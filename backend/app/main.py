"""FastAPI 入口：REST 接口 + WebSocket（对照需求 §6）。"""
import asyncio
import os
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, Depends, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session as OrmSession

from . import scheduler
from .db import Base, engine, get_db
from .models import Node, Session
from .schemas import (
    AddNodeRequest,
    CreateSessionRequest,
    GraphOut,
    ModelConfigIn,
    ModelConfigOut,
    NodeOut,
    SessionOut,
    UpdateNodeRequest,
    ResolveBlockedRequest,
    SendMessageRequest,
)
from .models import ContextEntry, ContextBlock, Approval, AppConfig, McpServer
from .model_config import ModelConfig, ModelProvider, PROVIDER_PRESETS, seed_providers, available_models
from .dag import compute_initial_status, would_create_cycle

# Windows 下 asyncio 子进程（exec_command）需要 Proactor 事件循环；
# 在事件循环创建前设置策略，避免 SelectorEventLoop 抛 NotImplementedError。
if os.name == "nt":
    try:
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    _migrate()
    task = asyncio.create_task(scheduler.scheduler_loop())
    yield
    task.cancel()


def _migrate():
    """轻量迁移：为已有 SQLite 表补充新增列。"""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "nodes" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("nodes")}
        if "parentContext" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE nodes ADD COLUMN parentContext JSON"))
        if "memory" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE nodes ADD COLUMN memory JSON"))
        if "plan" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE nodes ADD COLUMN plan JSON"))
    if "context_blocks" not in insp.get_table_names():
        Base.metadata.create_all(bind=engine)

    # 存量敏感字段迁移：把已明文存储的 API key 加密落盘（兼容幂等，加密失败会回落明文）
    try:
        from . import db as _db_ctx
        from .secure import is_encrypted, encrypt
        session = _db_ctx.SessionLocal()
        try:
            changed = False
            for p in session.query(ModelProvider).all():
                if p.apiKey and not is_encrypted(p._apiKey):
                    p._apiKey = encrypt(p.apiKey)  # property 读明文 -> 写密文
                    changed = True
            if changed:
                session.commit()
        finally:
            session.close()
    except Exception:
        pass  # 迁移失败不影响启动


app = FastAPI(title="会话任务 DAG 卡片后端", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _node_out(n: Node) -> NodeOut:
    return NodeOut(
        nodeId=n.nodeId,
        sessionId=n.sessionId,
        parentIds=n.parentIds or [],
        title=n.title,
        status=n.status,
        kind=n.kind,
        input={"text": n.inputText},
        messages=n.messages or [],
        output=n.output,
        progress=n.progress,
        contextRef=n.contextRef,
        parentContext=_parent_context_payload(n),
        plan=n.plan,
        collapsed=n.collapsed,
        subtreeCollapsed=n.subtreeCollapsed or False,
        dragOffset=n.dragOffset,
        customSize=n.customSize,
        meta={
            "mode": n.mode,
            "retryOf": n.retryOf,
            "failedReason": n.failedReason,
            "skippedParents": n.skippedParents or [],
        },
        model=n.model,
        files=n.files or [],
        createdAt=n.createdAt.isoformat() if n.createdAt else "",
        updatedAt=n.updatedAt.isoformat() if n.updatedAt else "",
    )


def _parent_context_payload(n) -> list[dict]:
    """把节点的 parentContext 展平成列表，供前端展示「上游上下文」。"""
    pc = n.parentContext or {}
    out: list[dict] = []
    if isinstance(pc, dict):
        for parent_node_id, blocks in pc.items():
            for b in blocks or []:
                if isinstance(b, dict):
                    out.append(
                        {
                            "parentNodeId": parent_node_id,
                            "parentTitle": b.get("title", ""),
                            "seq": b.get("seq"),
                            "summary": b.get("summary", ""),
                        }
                    )
    return out


def _gen_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@app.get("/api/model-config", response_model=dict)
def get_model_config(db: OrmSession = Depends(get_db)):
    """返回所有服务商（含是否已填 key 可用）与可用模型列表。"""
    seed_providers(db)
    providers = []
    has_any = False
    for p in db.query(ModelProvider).order_by(ModelProvider.id).all():
        usable = bool(p.apiKey and p.baseUrl)
        if usable:
            has_any = True
        providers.append(
            {
                "provider": p.id,
                "label": p.label,
                "baseUrl": p.baseUrl,
                "apiKey": p.api_key_full,
                "models": p.models or [],
                "enabled": usable,
            }
        )
    return {"hasConfig": has_any, "providers": providers, "availableModels": available_models(db)}


@app.put("/api/model-config", response_model=dict)
def put_model_config(body: dict, db: OrmSession = Depends(get_db)):
    """按 provider 保存 apiKey（可一次保存多个服务商）。"""
    seed_providers(db)
    saves = body.get("providers") or []
    for item in saves:
        key = item.get("provider")
        p = db.get(ModelProvider, key)
        if not p:
            p = ModelProvider(id=key, label=key, baseUrl=item.get("baseUrl") or "", models=item.get("models") or [])
            db.add(p)
        if item.get("apiKey") is not None:
            p.apiKey = item["apiKey"]
        if item.get("baseUrl") is not None:
            p.baseUrl = item["baseUrl"]
        if item.get("models") is not None:
            p.models = item["models"]
        p.enabled = bool(p.apiKey and p.baseUrl)
    db.commit()
    return get_model_config(db)


@app.get("/api/trust-level")
def get_trust_level():
    """读取审核信任档：all(全部信任)/partial(部分信任)/none(全部不信任)。"""
    from .trust import get_trust_level, TRUST_LEVELS
    cur = get_trust_level()
    return {
        "level": cur,
        "levels": list(TRUST_LEVELS),
        "labels": {"all": "全部信任", "partial": "部分信任", "none": "全部不信任"},
        "desc": {
            "all": "敏感操作全部免审批，agent 全自主",
            "partial": "危险命令/运行代码需审批，普通写文件免审",
            "none": "所有写/编辑/命令/运行代码都要人工审批",
        },
    }


@app.put("/api/trust-level")
def put_trust_level(body: dict, db: OrmSession = Depends(get_db)):
    """设置审核信任档。"""
    from .trust import set_trust_level, TRUST_LEVELS
    level = set_trust_level(str(body.get("level", "")), db)
    return {"level": level, "levels": list(TRUST_LEVELS)}


@app.get("/api/model-presets")
def get_model_presets():
    return PROVIDER_PRESETS


@app.get("/api/model-available")
def get_model_available(db: OrmSession = Depends(get_db)):
    """供卡片下拉：当前可用的模型（已填 key 的服务商）。"""
    seed_providers(db)
    return {"availableModels": available_models(db)}


@app.post("/api/sessions", response_model=NodeOut)
def create_session(req: CreateSessionRequest, db: OrmSession = Depends(get_db)):
    sid = _gen_id("s")
    title = (req.title or "").strip() or None
    workspace = (req.workspace or "").strip() or None
    root = Node(
        nodeId=f"{sid}-root",
        sessionId=sid,
        kind="root",
        title=title or "会话起点",
        status="done",
        parentIds=[],
        inputText="",
    )
    sess = Session(sessionId=sid, title=title, workspace=workspace)
    db.add(sess)
    db.add(root)
    db.commit()
    db.refresh(root)
    return _node_out(root)


@app.get("/api/sessions", response_model=list[SessionOut])
def list_sessions(db: OrmSession = Depends(get_db)):
    sessions = db.query(Session).order_by(Session.updatedAt.desc()).all()
    return [
        SessionOut(
            sessionId=s.sessionId,
            title=s.title,
            workspace=s.workspace,
            createdAt=s.createdAt.isoformat() if s.createdAt else "",
            updatedAt=s.updatedAt.isoformat() if s.updatedAt else "",
        )
        for s in sessions
    ]


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str, db: OrmSession = Depends(get_db)):
    """删除会话及其全部节点/上下文/审批。"""
    sess = db.get(Session, sid)
    if not sess:
        raise HTTPException(status_code=404, detail="会话不存在")
    # 级联清理独立表
    db.query(Node).filter(Node.sessionId == sid).delete(synchronize_session=False)
    db.query(ContextEntry).filter(ContextEntry.sessionId == sid).delete(synchronize_session=False)
    db.query(ContextBlock).filter(ContextBlock.sessionId == sid).delete(synchronize_session=False)
    db.query(Approval).filter(Approval.sessionId == sid).delete(synchronize_session=False)
    db.delete(sess)
    db.commit()
    return {"deleted": sid}


@app.get("/api/sessions/{sid}/graph", response_model=GraphOut)
def get_graph(sid: str, db: OrmSession = Depends(get_db)):
    nodes = db.query(Node).filter(Node.sessionId == sid).all()
    if not nodes:
        raise HTTPException(status_code=404, detail="会话不存在")
    node_outs = [_node_out(n) for n in nodes]
    edges = [
        {"id": f"{p}->{n.nodeId}", "source": p, "target": n.nodeId}
        for n in nodes
        for p in (n.parentIds or [])
    ]
    return GraphOut(sessionId=sid, nodes=node_outs, edges=edges)


@app.get("/api/sessions/{sid}/context/{key}/fulltext")
def get_context_fulltext(sid: str, key: str, db: OrmSession = Depends(get_db)):
    """按上下文 key 取对应节点的产出全文（按需检索，避免一次性塞入全量上下文）。"""
    from urllib.parse import unquote
    key = unquote(key)
    entry = (
        db.query(ContextEntry)
        .filter(ContextEntry.sessionId == sid, ContextEntry.key == key)
        .first()
    )
    if not entry:
        raise HTTPException(status_code=404, detail="上下文条目不存在")
    source_node_id = entry.sourceNodeId
    node = db.get(Node, source_node_id) if source_node_id else None
    content = ""
    if node and node.output:
        content = node.output.get("content") or node.output.get("summary") or ""
    return {
        "key": key,
        "summary": entry.value,
        "fulltext": content,
        "sourceNodeId": source_node_id,
    }


@app.get("/api/sessions/{sid}/context")
def get_context(sid: str, db: OrmSession = Depends(get_db)):
    """返回会话共享上下文：版本号 + 键值条目列表（§4.2.3）。"""
    sess = db.query(Session).filter(Session.sessionId == sid).first()
    if not sess:
        raise HTTPException(status_code=404, detail="会话不存在")
    entries = (
        db.query(ContextEntry)
        .filter(ContextEntry.sessionId == sid)
        .order_by(ContextEntry.key)
        .all()
    )
    return {
        "sessionId": sid,
        "contextVersion": sess.contextVersion or 0,
        "entries": [
            {
                "key": e.key,
                "value": e.value,
                "version": e.version,
                "sourceNodeId": e.sourceNodeId,
                "status": e.status,
                "candidates": e.candidates or [],
            }
            for e in entries
        ],
    }


@app.post("/api/sessions/{sid}/context/{key}/resolve")
def resolve_context(sid: str, key: str, body: dict, db: OrmSession = Depends(get_db)):
    """裁决冲突（§4.2.2）：action = keep_first / keep_second / merge + mergeValue。"""
    from urllib.parse import unquote
    key = unquote(key)
    entry = (
        db.query(ContextEntry)
        .filter(ContextEntry.sessionId == sid, ContextEntry.key == key)
        .first()
    )
    if not entry:
        raise HTTPException(status_code=404, detail="上下文条目不存在")
    if entry.status != "conflict":
        raise HTTPException(status_code=400, detail="该条目不在冲突态")

    candidates = entry.candidates or []
    action = body.get("action")
    if action == "keep_first":
        pick = candidates[0] if len(candidates) > 0 else None
    elif action == "keep_second":
        pick = candidates[1] if len(candidates) > 1 else None
    elif action == "merge":
        merged = (body.get("mergeValue") or "").strip()
        if not merged:
            raise HTTPException(status_code=400, detail="合并值不能为空")
        pick = {"value": merged, "sourceNodeId": "user-merge"}
    else:
        raise HTTPException(status_code=400, detail="未知裁决动作")

    if pick is None:
        raise HTTPException(status_code=400, detail="候选不足")

    entry.value = pick["value"]
    entry.sourceNodeId = pick.get("sourceNodeId")
    entry.status = "active"
    entry.candidates = []
    entry.version += 1
    db.commit()
    db.refresh(entry)
    return {
        "key": entry.key,
        "value": entry.value,
        "version": entry.version,
        "status": entry.status,
        "sourceNodeId": entry.sourceNodeId,
    }


@app.post("/api/sessions/{sid}/nodes", response_model=NodeOut)
def add_node(sid: str, req: AddNodeRequest, db: OrmSession = Depends(get_db)):
    nodes = db.query(Node).filter(Node.sessionId == sid).all()
    if not nodes:
        raise HTTPException(status_code=404, detail="会话不存在")
    by_id = {n.nodeId: n for n in nodes}
    children = {n.nodeId: set() for n in nodes}
    for n in nodes:
        for p in n.parentIds or []:
            children.setdefault(p, set()).add(n.nodeId)

    # 追加规则（§3.4）：优先使用显式传入的 parentIds；否则按模式从 anchor 反推
    if req.parentIds is not None:
        parent_ids = [p for p in req.parentIds if p]
        if req.mode == "join" and len(parent_ids) < 2:
            raise HTTPException(status_code=400, detail="join 需要至少 2 个父节点")
    elif req.mode == "parallel":
        anchor = req.anchorNodeId
        if not anchor or anchor not in by_id or by_id[anchor].kind == "root":
            raise HTTPException(status_code=400, detail="根节点不可并行追加")
        anchor_parents = by_id[anchor].parentIds or []
        if not anchor_parents:
            raise HTTPException(status_code=400, detail="锚点无父节点，无法并行")
        parent_ids = [anchor_parents[0]]
    else:  # serial
        anchor = req.anchorNodeId
        if anchor and anchor in by_id:
            parent_ids = [anchor]
        else:
            root = next((n.nodeId for n in nodes if n.kind == "root"), "root")
            parent_ids = [root]

    # 环检测（§3.4）
    for p in parent_ids:
        if p not in by_id:
            raise HTTPException(status_code=400, detail=f"父节点不存在：{p}")
    source_id = _gen_id("n")
    if would_create_cycle(children, source_id, parent_ids):
        raise HTTPException(status_code=400, detail="新边会构成环，已拒绝")

    status = compute_initial_status({n.nodeId: n.status for n in nodes}, parent_ids)
    # 新建空卡片（无用户输入）先停在 pending 等待输入，不自动执行
    if not (req.input.get("text") or "").strip():
        status = "pending"
    node = Node(
        nodeId=source_id,
        sessionId=sid,
        kind="task",
        parentIds=parent_ids,
        title=(req.input.get("text") or "新任务"),
        inputText=req.input.get("text") or "",
        status=status,
        mode=req.mode,
    )
    db.add(node)
    db.commit()
    db.refresh(node)
    return _node_out(node)


@app.get("/api/nodes/{nid}", response_model=NodeOut)
def get_node(nid: str, db: OrmSession = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(status_code=404, detail="节点不存在")
    return _node_out(n)


@app.get("/api/nodes/{nid}/events")
def get_node_events(nid: str, db: OrmSession = Depends(get_db)):
    """返回某个节点本次+历史的执行事件流（对齐 dsh 的 session event log 可视化）。

    让前端能实时渲染 agent 执行的细粒度过程（turn→step→工具→chunk 的轨迹），
    而不是只看到整批 messages 结果。
    """
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(status_code=404, detail="节点不存在")
    from . import event_log
    events = event_log.read_events(n.sessionId, nid)
    return {"nodeId": nid, "count": len(events), "events": events}


@app.get("/api/nodes/{nid}/concurrency")
def check_node_concurrency(nid: str, db: OrmSession = Depends(get_db)):
    """检测父子互斥：该节点的父链/子链上是否有正在运行的节点。

    用于"父子不能同时执行"：若命中，前端应提醒用户（如父节点正执行中，子节点需等待）。
    """
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(status_code=404, detail="节点不存在")
    nodes = db.query(Node).filter(Node.sessionId == n.sessionId).all()
    status_of = {x.nodeId: x.status for x in nodes}
    parents = {x.nodeId: set(x.parentIds or []) for x in nodes}
    from .dag import running_relatives
    running = running_relatives({nid}, status_of, parents)
    titles = {x.nodeId: x.title for x in nodes}
    return {
        "nodeId": nid,
        "blocked": len(running) > 0,
        "runningRelatives": running,
        "runningTitles": [titles.get(r, r) for r in running],
        "message": ("有相关节点正在执行，父子不可同时执行。" if running else "无冲突，可执行。"),
    }


@app.patch("/api/nodes/{nid}", response_model=NodeOut)
def update_node(nid: str, req: UpdateNodeRequest, db: OrmSession = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(status_code=404, detail="节点不存在")
    if req.title is not None:
        n.title = req.title
    if req.input is not None:
        n.inputText = req.input.get("text") or n.inputText
    if req.collapsed is not None:
        n.collapsed = req.collapsed
    if req.subtreeCollapsed is not None:
        n.subtreeCollapsed = req.subtreeCollapsed
    if req.dragOffset is not None:
        n.dragOffset = req.dragOffset
    if req.customSize is not None:
        n.customSize = req.customSize
    if req.model is not None:
        n.model = req.model
    db.commit()
    db.refresh(n)
    return _node_out(n)


@app.post("/api/nodes/{nid}/cancel", response_model=NodeOut)
def cancel_node(nid: str, db: OrmSession = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(status_code=404, detail="节点不存在")
    if n.status not in ("running", "ready", "pending"):
        raise HTTPException(status_code=400, detail="当前状态不可取消")
    n.status = "cancelled"
    db.commit()
    db.refresh(n)
    # 通知正在执行的 _run_real 协作式停止
    from . import scheduler as _sched
    _sched.request_cancel(nid)
    # 若真实执行任务仍在跑，直接中断其 asyncio task（避免等待当前流式完成）
    try:
        task = _sched._real_exec_tasks.get(nid)
        if task and not task.done():
            task.cancel()
    except Exception:
        pass
    print(f"[cancel] 节点 {nid} 已置 cancelled 并请求取消", flush=True)
    return _node_out(n)


@app.post("/api/nodes/{nid}/retry", response_model=NodeOut)
def retry_node(nid: str, db: OrmSession = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(status_code=404, detail="节点不存在")
    # 原地重试：保留对话消息与输出，仅把状态重置为 ready/pending，由调度器重新执行当前节点
    parents = {x.nodeId: x.status for x in db.query(Node).filter(Node.sessionId == n.sessionId).all()}
    status = compute_initial_status(parents, n.parentIds or [])
    # 清掉上一轮运行痕迹，但保留历史 messages 与历史 output
    n.status = status
    n.failedReason = None
    n.progress = None
    db.commit()
    db.refresh(n)
    return _node_out(n)


@app.post("/api/nodes/{nid}/resolve-blocked", response_model=NodeOut)
def resolve_blocked(nid: str, req: ResolveBlockedRequest, db: OrmSession = Depends(get_db)):
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(status_code=404, detail="节点不存在")
    if n.status != "blocked":
        raise HTTPException(status_code=400, detail="节点不在 blocked 状态")
    if req.action == "cancel":
        n.status = "cancelled"
    else:
        n.status = "ready"
        # 记录被跳过的父节点
        parent_nodes = db.query(Node).filter(Node.nodeId.in_(n.parentIds or [])).all()
        n.skippedParents = [
            p.nodeId for p in parent_nodes if p.status in ("failed", "cancelled")
        ]
    db.commit()
    db.refresh(n)
    return _node_out(n)


@app.post("/api/nodes/{nid}/pause", response_model=NodeOut)
def pause_node(nid: str, db: OrmSession = Depends(get_db)):
    """暂停运行中节点（§7 状态机 running → paused）。"""
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(status_code=404, detail="节点不存在")
    if n.status != "running":
        raise HTTPException(status_code=400, detail="当前状态不可暂停")
    n.status = "paused"
    db.commit()
    db.refresh(n)
    return _node_out(n)


@app.post("/api/nodes/{nid}/resume", response_model=NodeOut)
def resume_node(nid: str, db: OrmSession = Depends(get_db)):
    """恢复暂停节点（§7 状态机 paused → running）。"""
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(status_code=404, detail="节点不存在")
    if n.status != "paused":
        raise HTTPException(status_code=400, detail="当前状态不可恢复")
    n.status = "running"
    db.commit()
    db.refresh(n)
    return _node_out(n)


@app.delete("/api/nodes/{nid}", response_model=NodeOut)
def delete_node(nid: str, db: OrmSession = Depends(get_db)):
    """删除卡片：仅允许「无用户输入（无 user 消息）」且「无下游依赖」的节点（§删除规则）。"""
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(status_code=404, detail="节点不存在")
    if n.kind == "root":
        raise HTTPException(status_code=400, detail="根节点不可删除")

    siblings = db.query(Node).filter(Node.sessionId == n.sessionId, Node.nodeId != nid).all()
    has_downstream = any(nid in (x.parentIds or []) for x in siblings)
    if has_downstream:
        raise HTTPException(status_code=400, detail="卡片有下游依赖，暂不允许删除")

    result = _node_out(n)
    # 级联删除该节点发布的共享上下文条目，以及与其相关的审批
    db.query(ContextEntry).filter(ContextEntry.sourceNodeId == nid).delete()
    db.query(Approval).filter(Approval.nodeId == nid).delete()
    db.delete(n)
    db.commit()
    return result


@app.post("/api/nodes/{nid}/messages", response_model=NodeOut)
def send_message(nid: str, req: SendMessageRequest, db: OrmSession = Depends(get_db)):
    """卡片内多轮对话：追加一条用户消息；若节点可执行则进入 ready/running。

    - 首问自动生成标题（占位「新任务」时取首问前 8 字）。
    - 追加用户消息 + 一条空的 assistant 占位（流式由后端调度器填充）。
    """
    n = db.get(Node, nid)
    if not n or n.kind == "root":
        raise HTTPException(status_code=404, detail="节点不存在")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="消息不能为空")

    now_ms = int(__import__("time").time() * 1000)
    messages = list(n.messages or [])
    first_turn = len(messages) == 0 or n.title == "新任务"

    # 首问自动标题
    if first_turn:
        n.title = text[:8] + ("…" if len(text) > 8 else "")
        # 会话无标题时，用首问作为会话标题
        sess = db.query(Session).filter(Session.sessionId == n.sessionId).first()
        if sess and not sess.title:
            sess.title = text[:20] + ("…" if len(text) > 20 else "")

    messages.append({"id": _gen_id("m"), "role": "user", "text": text, "at": now_ms})

    # ---- 父子互斥：若本节点的父链或子链上有正在运行的节点，则提醒并阻止同时执行 ----
    try:
        nodes_all = db.query(Node).filter(Node.sessionId == n.sessionId).all()
        status_of = {x.nodeId: x.status for x in nodes_all}
        parents_of = {x.nodeId: set(x.parentIds or []) for x in nodes_all}
        from .dag import running_relatives
        running = running_relatives({nid}, status_of, parents_of)
        if running:
            titles = {x.nodeId: x.title for x in nodes_all}
            names = [titles.get(r, r) for r in running]
            raise HTTPException(
                status_code=409,
                detail=f"父节点或子节点正在执行中（{'、'.join(names)}），父子不可同时执行。请等它完成后重试。",
            )
    except HTTPException:
        raise
    except Exception:
        pass  # 校验失败不阻断

    # 执行时机：pending 节点若来源全部 done 则就绪；否则等待
    if n.status in ("pending", "done", "failed", "cancelled", "blocked"):
        nodes = db.query(Node).filter(Node.sessionId == n.sessionId).all()
        parent_nodes = {p.nodeId: p for p in nodes if p.nodeId in (n.parentIds or [])}
        if n.status == "pending":
            if parent_nodes and all(p.status == "done" for p in parent_nodes.values()):
                n.status = "ready"
        else:
            # 已完成/失败/取消的卡片继续追问：重新进入 running
            n.status = "running"
        messages.append(
            {"id": _gen_id("m"), "role": "assistant", "text": "", "streaming": True, "at": now_ms}
        )

    n.messages = messages
    n.inputText = text
    db.commit()
    db.refresh(n)
    return _node_out(n)


@app.post("/api/sessions/{sid}/budget")
def set_session_budget(sid: str, body: dict, db: OrmSession = Depends(get_db)):
    sess = db.get(Session, sid)
    if not sess:
        raise HTTPException(status_code=404, detail="会话不存在")
    sess.budget = body.get("budget")
    db.commit()
    return {"sessionId": sid, "budget": sess.budget}


@app.get("/api/browse")
def browse_dir(path: str = ""):
    """后端驱动的目录浏览（前端「浏览」选择工作目录用），返回绝对路径列表。"""
    import os
    import string
    base = (path or "").strip()
    entries: list[dict] = []
    if not base:
        # 无路径：列出盘符
        if os.name == "nt":
            import ctypes
            drives = []
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if bitmask & (1 << i):
                    drives.append(f"{string.ascii_uppercase[i]}:\\")
            return {"path": "", "entries": [{"name": d, "isDir": True, "path": d} for d in drives]}
        return {"path": "", "entries": [{"name": "/", "isDir": True, "path": "/"}]}
    if not os.path.isdir(base):
        return {"path": base, "entries": [], "error": "目录不存在"}
    for name in sorted(os.listdir(base)):
        full = os.path.join(base, name)
        if os.path.isdir(full):
            entries.append({"name": name, "isDir": True, "path": os.path.abspath(full)})
    parent = os.path.dirname(os.path.abspath(base))
    return {
        "path": os.path.abspath(base),
        "parent": parent,
        "entries": entries,
    }


@app.get("/api/sessions/{sid}/approvals/pending")
def pending_approvals(sid: str, db: OrmSession = Depends(get_db)):
    """返回该会话当前的待审批记录（前端拉取兜底，避免错过 WS 事件；按卡片分组展示）。"""
    rows = (
        db.query(Approval)
        .filter(Approval.sessionId == sid, Approval.status == "pending")
        .order_by(Approval.createdAt.asc())
        .all()
    )
    return [{"approvalId": a.id, "nodeId": a.nodeId, "tool": a.tool, "args": a.args} for a in rows]


@app.get("/api/skills")
def get_skills(db: OrmSession = Depends(get_db)):
    """返回 skill 目录配置 + 扫描到的 skill 列表（内置 + 自定义）。"""
    from . import skills as skills_mod
    row = db.query(AppConfig).filter(AppConfig.key == "skill_dir").first()
    skill_dir = (row.value if row else None) or ""
    return {
        "skillDir": skill_dir,
        "skills": skills_mod.all_skills(skill_dir or None),
    }


@app.put("/api/skills")
def put_skills(body: dict, db: OrmSession = Depends(get_db)):
    """设置 skill 目录路径并返回扫描结果。"""
    from . import skills as skills_mod
    skill_dir = (body.get("skillDir") or "").strip()
    row = db.query(AppConfig).filter(AppConfig.key == "skill_dir").first()
    if row:
        row.value = skill_dir
    else:
        db.add(AppConfig(key="skill_dir", value=skill_dir))
    db.commit()
    return {"skillDir": skill_dir, "skills": skills_mod.all_skills(skill_dir or None)}


@app.get("/api/config/tavily")
def get_tavily_config(db: OrmSession = Depends(get_db)):
    """返回 Tavily 搜索 API key 配置（前端回显明文）。"""
    from .secure import decrypt
    row = db.query(AppConfig).filter(AppConfig.key == "tavily_api_key").first()
    return {"apiKey": decrypt(row.value) if row else ""}


@app.put("/api/config/tavily")
def put_tavily_config(body: dict, db: OrmSession = Depends(get_db)):
    """保存 Tavily 搜索 API key（敏感字段加密存储）。"""
    from .secure import encrypt
    api_key = (body.get("apiKey") or "").strip()
    stored = encrypt(api_key)
    row = db.query(AppConfig).filter(AppConfig.key == "tavily_api_key").first()
    if row:
        row.value = stored
    else:
        db.add(AppConfig(key="tavily_api_key", value=stored))
    db.commit()
    return {"apiKey": api_key}


@app.get("/api/mcp/servers")
def list_mcp_servers(db: OrmSession = Depends(get_db)):
    """返回已配置的 MCP server 列表。"""
    rows = db.query(McpServer).order_by(McpServer.id).all()
    return [
        {"id": r.id, "name": r.name, "command": r.command, "args": r.args or [], "enabled": r.enabled}
        for r in rows
    ]


@app.post("/api/mcp/servers")
def add_mcp_server(body: dict, db: OrmSession = Depends(get_db)):
    """新增一个 MCP server（stdio 模式）。"""
    name = (body.get("name") or "").strip()
    command = (body.get("command") or "").strip()
    if not name or not command:
        raise HTTPException(status_code=400, detail="name 和 command 必填")
    row = McpServer(
        name=name,
        command=command,
        args=body.get("args") or [],
        enabled=body.get("enabled", True),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "name": row.name, "command": row.command, "args": row.args, "enabled": row.enabled}


@app.delete("/api/mcp/servers/{sid}")
def delete_mcp_server(sid: int, db: OrmSession = Depends(get_db)):
    row = db.get(McpServer, sid)
    if not row:
        raise HTTPException(status_code=404, detail="MCP server 不存在")
    db.delete(row)
    db.commit()
    return {"deleted": sid}


@app.post("/api/mcp/servers/{sid}/toggle")
def toggle_mcp_server(sid: int, db: OrmSession = Depends(get_db)):
    row = db.get(McpServer, sid)
    if not row:
        raise HTTPException(status_code=404, detail="MCP server 不存在")
    row.enabled = not row.enabled
    db.commit()
    return {"id": row.id, "enabled": row.enabled}


@app.get("/api/mcp/tools")
async def list_mcp_tools_endpoint():
    """连接所有启用的 MCP server，返回其工具列表（含 schema）。"""
    from .mcp_manager import list_mcp_tools
    return {"tools": await list_mcp_tools()}


@app.get("/api/sessions/{sid}/usage")
def get_session_usage(sid: str, db: OrmSession = Depends(get_db)):
    sess = db.get(Session, sid)
    if not sess:
        raise HTTPException(status_code=404, detail="会话不存在")
    usage = sess.usage or {}
    cost = usage.get("cost", 0.0)
    budget = sess.budget
    balance = round(budget - cost, 4) if budget is not None else None
    return {
        "sessionId": sid,
        "promptTokens": usage.get("promptTokens", 0),
        "completionTokens": usage.get("completionTokens", 0),
        "cost": cost,
        "budget": budget,
        "balance": balance,
    }


@app.post("/api/nodes/{nid}/files")
async def upload_node_file(nid: str, file: UploadFile = File(...), db: OrmSession = Depends(get_db)):
    """上传文件到会话 workspace 并关联到卡片。"""
    n = db.get(Node, nid)
    if not n:
        raise HTTPException(status_code=404, detail="节点不存在")
    sess = db.get(Session, n.sessionId)
    workspace = (sess.workspace if sess and sess.workspace else None) or os.getcwd()
    os.makedirs(workspace, exist_ok=True)
    filename = os.path.basename(file.filename or "upload.bin")
    dest = os.path.join(workspace, filename)
    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)
    files = list(n.files or [])
    files.append({"path": dest, "name": filename, "size": len(content)})
    n.files = files
    db.commit()
    db.refresh(n)
    return _node_out(n)


@app.post("/api/ocr")
async def ocr_upload(file: UploadFile = File(...)):
    """接收图片并 OCR 识别，返回识别文本（用于多模态：图片 → 文本 → 模型）。"""
    from .ocr import ocr_image
    content = await file.read()
    if not content:
        return {"text": ""}
    # 尝试从文件名判断扩展名；用内存 bytes 直接 OCR
    text = ocr_image(content)
    return {"text": text}


@app.websocket("/ws/sessions/{sid}")
async def ws_session(ws: WebSocket, sid: str):
    await ws.accept()
    q = scheduler.subscribe(sid)
    try:
        while True:
            event = await q.get()
            await ws.send_json(event)
    except WebSocketDisconnect:
        scheduler.unsubscribe(sid, q)


@app.post("/api/approvals/{aid}/approve")
def approve_approval(aid: str, db: OrmSession = Depends(get_db)):
    ap = db.get(Approval, aid)
    if not ap:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    ap.status = "approved"
    db.commit()
    return {"approvalId": aid, "status": "approved"}


@app.post("/api/approvals/{aid}/reject")
def reject_approval(aid: str, db: OrmSession = Depends(get_db)):
    ap = db.get(Approval, aid)
    if not ap:
        raise HTTPException(status_code=404, detail="审批请求不存在")
    ap.status = "rejected"
    db.commit()
    return {"approvalId": aid, "status": "rejected"}
