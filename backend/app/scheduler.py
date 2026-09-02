"""进程内 DAG 调度器 + WebSocket 广播（对照需求 §4 / §7）。

依赖驱动：pending --父全部终态--> ready --调度--> running --成功/失败--> done/failed。
并发上限 CONCURRENCY=5（per-process，原型阶段）。
"""
import asyncio
import os
import random
import time
from typing import Any

from .db import SessionLocal
from .models import Node, Session, ContextEntry, ContextBlock, Approval
from .dag import compute_initial_status, is_terminal, running_relatives
from .executor import (
    stream_chat,
    stream_final,
    stream_chat_with_tools,
    build_messages,
    summarize_text,
    generate_plan,
    ModelNotConfigured,
)
from .tools import openai_tools, TOOL_BY_NAME, needs_approval
from .pricing import compute_cost
from .tool_executor import execute
from .model_config import ModelConfig, ModelProvider
from .event_log import EventLog, clear_events

CONCURRENCY = 5
TICK_SECONDS = 1.0

# 简单的进程内 pub/sub：sessionId -> set[asyncio.Queue]
_subscribers: dict[str, set[asyncio.Queue]] = {}

# 真实执行的异步任务：nodeId -> asyncio.Task
_real_exec_tasks: dict[str, asyncio.Task] = {}

# 取消请求集合：cancel 接口写入，_run_real 协作式检查后退出
_cancel_requested: set[str] = set()


def request_cancel(node_id: str) -> None:
    _cancel_requested.add(node_id)


def _model_configured(db) -> bool:
    return any(p.apiKey and p.baseUrl for p in db.query(ModelProvider).all())


def _pick_provider(db, model: str | None) -> ModelProvider | None:
    """按模型名匹配已启用的服务商；未指定时取第一个已启用的。"""
    providers = [p for p in db.query(ModelProvider).all() if p.apiKey and p.baseUrl]
    if not providers:
        return None
    if model:
        for p in providers:
            if model in (p.models or []):
                return p
    return providers[0]


def subscribe(session_id: str) -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.setdefault(session_id, set()).add(q)
    return q


def unsubscribe(session_id: str, q: asyncio.Queue) -> None:
    _subscribers.get(session_id, set()).discard(q)


async def publish(session_id: str, event: dict[str, Any]) -> None:
    for q in list(_subscribers.get(session_id, set())):
        try:
            q.put_nowait(event)
        except Exception:
            pass


def _approval_id() -> str:
    import uuid
    return "ap-" + uuid.uuid4().hex[:12]


def _brief_args(args: dict) -> str:
    """把工具参数压缩成一行便于展示（"每步具体在干嘛"）。"""
    parts = []
    for k, v in (args or {}).items():
        sv = str(v).replace("\n", " ")
        if len(sv) > 60:
            sv = sv[:60] + "…"
        parts.append(f"{k}={sv}")
    return " ".join(parts)


def _now_dt():
    from datetime import datetime
    return datetime.now()


def _gen_step_id() -> str:
    import uuid
    return "step-" + uuid.uuid4().hex[:10]


def _summary_from_json(s: str) -> str:
    """从 LLM 返回的结构化摘要 JSON 里提取纯文本摘要（统一走 jsonutil 严格解析）。"""
    if not s or not s.strip():
        return ""
    try:
        from .jsonutil import parse_json_object, expect_str, expect_list
        obj = parse_json_object(s)
        summary = expect_str(obj, "summary", "")
        if not summary:
            parts = []
            kps = expect_list(obj, "key_points", [])
            if kps:
                parts.append("；".join(str(k) for k in kps))
            conc = expect_str(obj, "conclusion", "")
            if conc:
                parts.append(f"结论：{conc}")
            summary = "。".join(parts)
        return (summary or "").strip()
    except Exception:
        return s[:500]


async def _rolling_memory(provider, model: str, old_memory: dict | None, user_text: str, final_text: str) -> dict | None:
    """用 LLM 把本轮对话折叠进滚动记忆，返回新 memory 字典。"""
    import json as _json
    old = old_memory or {}
    old_summary = old.get("summary", "")
    old_facts = old.get("key_facts", []) or []
    prompt = (
        "请把本卡片的历史记忆和本轮进展折叠成新的滚动记忆（供未来对话引用，控制长度）。"
        "严格返回 JSON，不要多余文字：\n"
        '{"summary": "一段不超过150字的历史摘要", "key_facts": ["事实1", "事实2", "事实3"], '
        '"conclusion": "最近结论"}\n\n'
        f"历史摘要：{old_summary[:800]}\n"
        f"历史关键事实：{'; '.join(str(f) for f in old_facts)[:800]}\n"
        f"本轮用户要求：{user_text[:800]}\n"
        f"本轮产出：{final_text[:2000]}\n"
    )
    msgs = [
        {"role": "system", "content": "你是记忆压缩助手，产出简洁、信息无损的滚动记忆。"},
        {"role": "user", "content": prompt},
    ]
    try:
        from .executor import _chat_once_text
        from .jsonutil import parse_json_object, expect_str, expect_list
        content = await _chat_once_text(provider.baseUrl.rstrip("/"), provider.pick_api_key(), model, msgs)
        obj = parse_json_object(content or "")  # 剥围栏 + 严格解析
        summary = expect_str(obj, "summary", "") or old_summary or final_text[:150]
        key_facts = [str(f) for f in expect_list(obj, "key_facts", []) if str(f).strip()]
        conclusion = expect_str(obj, "conclusion", "")
        out = _prune_memory(
            summary=summary,
            key_facts=key_facts[:8],
            conclusion=conclusion,
            archive=old.get("archive") or [],
        )
        out.update(_retain_goal_fields(old))
        return out
    except Exception:
        # 回退：合并旧摘要与本轮产出简单拼接
        out = _prune_memory(
            summary=f"{old_summary}; {final_text[:120]}".strip("; "),
            key_facts=old_facts,
            conclusion=final_text[:200],
            archive=old.get("archive") or [],
        )
        out.update(_retain_goal_fields(old))
        return out


def _retain_goal_fields(old: dict) -> dict:
    """从旧 memory 保留 goal 自动延续相关的字段（否则 _rolling_memory 会覆盖丢目标）。"""
    keep = {}
    for k in ("goal", "goal_round", "goal_max_rounds"):
        if old.get(k):
            keep[k] = old[k]
    return keep


def _prune_memory(
    summary: str,
    key_facts: list,
    conclusion: str,
    archive: list,
) -> dict:
    """memory 剪枝 + 层级摘要压缩（对齐业界「递归/分层记忆」）。

    规则（防止记忆无限膨胀）：
    1. key_facts 超过上限：把最旧的若干条 roll-up 进 summary，再删除它们（不再无限累积）。
    2. summary 超过长度上限：把当前 summary 下沉为「更底层 archive 条目」（分层摘要），
       让 summary 保持是「近期要点」，历史则压缩进 archive（数量也限流）。
    3. 时间戳：archive 条目带 when 标签，便于区分新旧。
    """
    import time as _time

    facts = [str(f) for f in (key_facts or []) if str(f).strip()]
    MAX_FACTS = 8
    MAX_SUMMARY_CHARS = 220
    MAX_ARCHIVE = 12

    arch = list(archive or [])
    now = _time.strftime("%m-%d %H:%M")

    # 规则1：key_facts 溢出 → 最旧条 roll-up 进 summary
    if len(facts) > MAX_FACTS:
        overflow = facts[: len(facts) - MAX_FACTS]
        facts = facts[-MAX_FACTS:]
        if overflow:
            summary = (summary + "；" + "；".join(overflow)).strip("；")[:MAX_SUMMARY_CHARS + 200]

    # 规则2：summary 过长 → 下沉到 archive（分层），重开近期 summary
    if len(summary) > MAX_SUMMARY_CHARS:
        if summary:
            arch.append({"layer": len(arch) + 1, "when": now, "summary": summary.strip()})
            if len(arch) > MAX_ARCHIVE:
                # 只保留最近 MAX_ARCHIVE 层（更早的已退化，不再累积）
                arch = arch[-MAX_ARCHIVE:]
        summary = (conclusion or "").strip() or summary[:MAX_SUMMARY_CHARS]

    return {
        "summary": summary[:MAX_SUMMARY_CHARS + 400],
        "key_facts": facts,
        "conclusion": conclusion,
        "archive": arch,
    }


async def _request_approval(db, node: Node, tool: str, args: dict) -> Approval:
    """创建审批请求并推送前端。"""
    ap = Approval(
        id=_approval_id(),
        sessionId=node.sessionId,
        nodeId=node.nodeId,
        tool=tool,
        args=args,
        status="pending",
    )
    db.add(ap)
    db.commit()
    await publish(
        node.sessionId,
        {
            "type": "approval",
            "approvalId": ap.id,
            "nodeId": node.nodeId,
            "tool": tool,
            "args": args,
        },
    )
    return ap


async def _wait_approval(db, approval_id: str) -> str:
    """轮询审批结果，最多等 5 分钟，返回 approved/rejected。"""
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        # expire 强制刷新，避免 SQLAlchemy identity map 缓存旧 status
        db.expire_all()
        ap = db.get(Approval, approval_id)
        if ap and ap.status in ("approved", "rejected"):
            return ap.status
        await asyncio.sleep(1)
    return "rejected"


def _runtime_seconds(node: Node) -> float:
    return 2.5 + random.random() * 3.0


def _make_output(node: Node) -> dict:
    return {
        "type": "text",
        "summary": f"已完成「{node.title}」：结论已同步到共享上下文",
        "content": f"针对「{node.title}」的执行结果已就绪。",
        "artifacts": [],
    }


async def publish_output_to_context(db, node: Node) -> None:
    """节点完成时把 output 作为一个「内容块」追加发布（§4.2.2）。

    每个节点每轮执行追加一个不可变内容块（ContextBlock），按时间顺序构成该节点的完整历史。
    下游节点继承上游的所有内容块（摘要索引 + 按需取全文）。
    同时维护 ContextEntry（key=标题）作为兼容的“最新摘要”索引与冲突检测。
    """
    output = node.output or {}
    raw_summary = output.get("summary") or ""
    fulltext = output.get("content") or raw_summary or ""
    sess = db.query(Session).filter(Session.sessionId == node.sessionId).first()
    if not sess:
        return

    # LLM 生成结构化摘要（对齐主流 LLM 压缩记忆）；失败回退原文截断
    summary = raw_summary[:500] or ""
    provider = _pick_provider(db, (node.__dict__.get("model") or None))
    if provider and fulltext.strip():
        try:
            summary_json = await summarize_text(
                provider.baseUrl.rstrip("/"),
                provider.pick_api_key(),
                (node.__dict__.get("model") or None) or ((provider.models or [])[0] if provider.models else "default"),
                fulltext,
            )
            candidate = _summary_from_json(summary_json)
            if candidate:
                summary = candidate
        except Exception:
            summary = raw_summary[:500] or fulltext[:200]

    # 追加不可变内容块
    max_seq = (
        db.query(ContextBlock)
        .filter(ContextBlock.sessionId == node.sessionId, ContextBlock.nodeId == node.nodeId)
        .count()
    )
    block_seq = max_seq + 1
    db.add(
        ContextBlock(
            sessionId=node.sessionId,
            nodeId=node.nodeId,
            title=node.title,
            seq=block_seq,
            summary=summary,
            fulltext=fulltext,
        )
    )
    # 立即 flush，让下面的下游上下文刷新查询能看到这个新块
    db.flush()

    # 写入向量库（语义检索按需召回）
    try:
        from . import memory
        memory.add_block(node.sessionId, node.nodeId, block_seq, node.title, fulltext)
    except Exception:
        pass

    # 兼容旧的 ContextEntry（key=标题 → 最新摘要），冲突检测逻辑保留
    entry = (
        db.query(ContextEntry)
        .filter(ContextEntry.sessionId == node.sessionId, ContextEntry.key == node.title)
        .first()
    )
    base = (node.baseContext or {}).get(node.title)

    if entry is None:
        db.add(
            ContextEntry(
                sessionId=node.sessionId,
                key=node.title,
                value=summary,
                version=1,
                sourceNodeId=node.nodeId,
                status="active",
                candidates=[],
            )
        )
    else:
        cur = entry.version
        if base is None and entry.sourceNodeId and entry.sourceNodeId != node.nodeId:
            _mark_conflict(entry, node, summary)
        elif base != cur and base is not None:
            _mark_conflict(entry, node, summary)
        else:
            entry.value = summary
            entry.version += 1
            entry.sourceNodeId = node.nodeId
            entry.status = "active"
    sess.contextVersion = (sess.contextVersion or 0) + 1

    # 刷新所有下游节点的父上下文索引（不重跑模型，只让下游知道父节点新增内容）
    downstream = (
        db.query(Node)
        .filter(Node.sessionId == node.sessionId, Node.nodeId != node.nodeId)
        .all()
    )
    changed: list[Node] = []
    for dn in downstream:
        if node.nodeId not in (dn.parentIds or []):
            continue
        _refresh_parent_context(db, dn)
        changed.append(dn)

    # 推送下游节点上下文更新事件（前端据此刷新）
    for dn in changed:
        await publish(
            node.sessionId,
            {
                "type": "parent_context_updated",
                "nodeId": dn.nodeId,
                "parentNodeId": node.nodeId,
                "context": _parent_context_of(db, dn),
            },
        )


def _blocks_for(db, parent_node_id: str, session_id: str) -> list[dict]:
    blocks = (
        db.query(ContextBlock)
        .filter(ContextBlock.sessionId == session_id, ContextBlock.nodeId == parent_node_id)
        .order_by(ContextBlock.seq.asc())
        .all()
    )
    return [
        {"title": b.title, "seq": b.seq, "summary": b.summary, "fulltext": b.fulltext}
        for b in blocks
    ]


def _refresh_parent_context(db, downstream: Node) -> None:
    """根据下游的 parentIds 重新计算其 parentContext 索引。"""
    pc: dict[str, list[dict]] = {}
    for pid in downstream.parentIds or []:
        parent = db.get(Node, pid)
        blocks = _blocks_for(db, pid, downstream.sessionId)
        if blocks:
            pc[pid] = blocks
        elif parent and parent.output and parent.output.get("summary"):
            pc[pid] = [{"title": parent.title, "seq": 1, "summary": parent.output["summary"], "fulltext": parent.output.get("content", "")}]
    downstream.parentContext = pc


def _parent_context_of(db, downstream: Node) -> list[dict]:
    pc = downstream.parentContext or {}
    out: list[dict] = []
    for parent_node_id, blocks in pc.items():
        for b in blocks:
            out.append({"parentNodeId": parent_node_id, "parentTitle": b.get("title", ""), "seq": b.get("seq"), "summary": b.get("summary", "")})
    return out


def _mark_conflict(entry: ContextEntry, node: Node, new_value: str) -> None:
    entry.status = "conflict"
    existing = {c.get("sourceNodeId"): c for c in (entry.candidates or [])}
    if entry.sourceNodeId and entry.sourceNodeId not in existing:
        existing[entry.sourceNodeId] = {"value": entry.value, "sourceNodeId": entry.sourceNodeId}
    existing[node.nodeId] = {"value": new_value, "sourceNodeId": node.nodeId}
    entry.candidates = list(existing.values())
    entry.sourceNodeId = None  # 冲突态下无单一来源


def _goal_done_marked(text: str | None) -> bool:
    """判断最终文本是否声明了当前目标已完成（[GOAL_DONE]）。"""
    return bool(text and "[GOAL_DONE]" in text)


def _todo_summary(parsed: dict) -> str:
    """把 todo_write 的 todos 清单压缩成一行便于展示。"""
    todos = parsed.get("todos") or []
    parts = []
    for t in todos:
        c = str(t.get("content", ""))[:30]
        s = t.get("status", "pending")
        mark = {"pending": "⬜", "in_progress": "🔄", "completed": "✅"}.get(s, "⬜")
        parts.append(f"{mark}{c}")
    return " | ".join(parts) if parts else "（空）"


async def _ask_user_and_pause(db, node: Node, parsed: dict) -> None:
    """处理 ask_user：把问题写进消息流，节点置 paused 等待用户回复。

    用户在会话里回复(send_message)后，节点回到 running 重新执行，agent 拿到回答继续。
    对齐 dsh ask_user —— 让 agent 在关键决策前能真正询问用户。
    """
    questions = parsed.get("questions") or []
    if not questions:
        return
    # 组装问题文本：每个问题一行，带选项
    lines = []
    for i, q in enumerate(questions, 1):
        qtxt = str(q.get("question", "")).strip()
        if not qtxt:
            continue
        header = str(q.get("header", "")).strip()
        prefix = f"【{header}】 " if header else "[提问] "
        lines.append(f"{prefix}{qtxt}")
        opts = q.get("options") or []
        if opts:
            for idx, o in enumerate(opts, 1):
                lines.append(f"  {idx}. {o}")
    if not lines:
        return
    question_text = "\n".join(lines)

    # 追加到消息流（系统步骤消息，前端消息流可见）
    msgs = list(node.messages or [])
    msgs.append({"id": _gen_step_id(), "role": "system", "step": "用户提问", "detail": question_text[:800], "at": int(time.time() * 1000)})
    node.messages = msgs
    # 置为 paused，等待用户回复（send_message 会恢复到 running）
    node.status = "paused"
    db.commit()
    await publish(
        node.sessionId,
        {
            "type": "node_update",
            "nodeId": node.nodeId,
            "status": "paused",
            "messages": node.messages,
            "userQuestion": question_text[:800],
        },
    )



async def _handle_todo_write(db, node: Node, parsed: dict) -> None:
    """处理 todo_write：整份替换 node.plan.steps，持久化并推送前端。

    对齐 dsh todo_write 语义：每次全量替换、status 三元（pending/in_progress/completed），
    并做本地映射到已有 plan 结构（status 归一化为 running/done 以兼容前端 plan 渲染）。
    """
    todos = parsed.get("todos") or []
    steps = []
    for t in todos:
        content = str(t.get("content", "")).strip()
        if not content:
            continue
        raw_status = t.get("status", "pending")
        # dsh 的 completed/in_progress/pending -> 本地 plan 的 done/running/pending
        mapped = "done" if raw_status == "completed" else ("running" if raw_status == "in_progress" else "pending")
        steps.append({"label": content, "status": mapped})
    if not steps:
        return
    plan = dict(node.plan or {})
    plan["steps"] = steps
    node.plan = plan
    if hasattr(node, "messages"):
        node.messages = node.messages or []
    db.commit()
    await publish(
        node.sessionId,
        {
            "type": "node_update",
            "nodeId": node.nodeId,
            "status": node.status,
            "plan": node.plan,
            "messages": node.messages,
        },
    )


# 判定"复杂任务"的敏感词（含这些关键词的操作，计划需要先经用户审批）
_PLAN_APPROVAL_SENSITIVE = (
    "删除", "清空", "重置", "覆盖", "重写", "迁移", "重构", "安装", "卸载",
    "git push", "git commit", "删除文件", "打包", "发布", "部署", "改数据库", "迁移数据",
)


def _task_needs_plan_approval(plan: dict | None, user_text: str) -> bool:
    """复杂任务(true)才弹计划审批：步骤较多，或任务文本含敏感操作。"""
    steps = ((plan or {}).get("steps") or []) if isinstance(plan, dict) else []
    if len(steps) >= 3:
        return True
    low = (user_text or "").lower()
    return any(k in low for k in _PLAN_APPROVAL_SENSITIVE)


async def _maybe_plan_approval(db, node: Node, user_text: str, messages: list[dict], provider, provider_model: str) -> None:
    """混合 plan-mode：默认高自主执行；复杂任务先弹计划审批，批准才继续，拒绝则要求重规划。"""
    if not _task_needs_plan_approval(node.plan, user_text):
        return
    plan = node.plan or {}
    steps_text = "\n".join(f"- {s.get('label', '')}" for s in (plan.get("steps") or []))
    summary = f"任务计划（{len((plan.get('steps') or []))} 步）：\n{steps_text}"
    # 通过现有审批机制弹计划审批
    ap = await _request_approval(db, node, "plan", {"plan": summary[:2000]})
    decision = await _wait_approval(db, ap.id)
    if decision == "rejected":
        # 用户拒绝计划：把该审批收尾为终态，注入引导消息让模型重新规划
        await _settle_approval(db, ap.id)
        messages.append(
            {
                "role": "user",
                "content": (
                    "用户对当前计划不满意并拒绝了。请重新审视任务，制定一个更合理、更贴合用户意图的执行计划，"
                    "然后用 todo_write 更新计划步骤，再按新计划逐步执行。不要重复刚才不被认可的做法。"
                ),
            }
        )
        await publish(
            node.sessionId,
            {"type": "node_update", "nodeId": node.nodeId, "status": node.status,
             "plan": node.plan, "messages": node.messages, "planRejected": True},
        )
    else:
        await publish(
            node.sessionId,
            {"type": "node_update", "nodeId": node.nodeId, "status": node.status,
             "plan": node.plan, "messages": node.messages, "planApproved": True},
        )


async def _settle_approval(db, approval_id: str) -> None:
    """把审批收尾为终态（避免悬空 pending）。"""
    try:
        ap = db.get(Approval, approval_id)
        if ap and ap.status == "pending":
            ap.status = "rejected"
            db.commit()
    except Exception:
        pass


async def _publish_exec_event(db, node: Node) -> None:
    """把节点最新执行事件推给前端（WS），实现执行轨迹的实时可视化。

    前端收到 exec_event 后可增量渲染 agent 的 turn→step→工具→结果轨迹，
    对齐 dsh session event log 的实时反馈。
    """
    try:
        from . import event_log
        events = event_log.EventLog(node.sessionId, node.nodeId).latest_run_events()
        if not events:
            return
        last = events[-1]
        await publish(
            node.sessionId,
            {
                "type": "exec_event",
                "nodeId": node.nodeId,
                "event": last,
                "traceCount": len(events),
            },
        )
    except Exception:
        pass


async def _record_experiential(db, node: Node, workspace: str, provider=None, provider_model: str = "") -> dict:
    """项目经验自动回写（确定性、零污染、不阻塞）。

    从本次执行抽取"有把握"的经验写入项目记忆，避免下次重复遍历工具/代码：
    - skills_usage：本次实际调用了哪些工具（event_log 真实工具集）
    - entry_points : 本次产出里明确提到的项目文件路径（触碰过的关键位置）
    不猜坑/不猜结论，所以不会注入错误经验。
    """
    from . import experiential_mem as xmem
    added = {"skills_usage": 0, "entry_points": 0, "lessons": 0}
    try:
        from . import event_log
        events = event_log.EventLog(node.sessionId, node.nodeId).latest_run_events()
        tools = []
        for e in events:
            if e.get("kind") == "tool_call":
                t = e.get("tool")
                if t and t not in tools and not t.startswith("mcp__"):
                    tools.append(t)
        if tools:
            recorded = ", ".join(tools)
            xmem.record_line(workspace, "skills_usage",
                             f"任务「{str(node.title or '')[:40]}」用到的工具：{recorded}")
            added["skills_usage"] += 1

        # 触碰过的项目文件（从产出里出现的代码文件路径）
        html = (node.output or {}).get("content") or ""
        found = {}
        import re as _re
        if isinstance(html, str):
            for m in _re.finditer(r"[\w./\\-]+\.(py|ts|tsx|js|jsx|md|json|ya?ml|sh|sql|c|cpp|h|java|go|rs)\b", html):
                p = m.group(0)
                key = p.replace("\\", "/")
                if key and not key.startswith((".", "/", "http")) and "/" not in key:
                    key = key
                found.setdefault(key, None)
        real = [p for p in found if p and os.path.exists(os.path.join(workspace, p))]
        if real[:6]:
            xmem.record_line(workspace, "entry_points",
                             f"任务「{str(node.title or '')[:40]}」涉及文件：{', '.join(real[:6])}")
            added["entry_points"] += 1
    except Exception:
        pass

    # 过滤式收获沉淀：让模型反思并沉淀"可复用"经验（skill/入口/坑）；失败静默
    if provider is not None:
        try:
            from .experiential_mem import reflect_and_sink
            base = (provider.baseUrl or "").rstrip("/")
            key = provider.pick_api_key() if hasattr(provider, "pick_api_key") else ""
            n = await reflect_and_sink(
                workspace, str(node.title or ""),
                tools if "tools" in dir() else [], real if "real" in dir() else [],
                base, key or "", provider_model,
            )
            if n:
                added["lessons"] = max(added.get("lessons", 0), n)
        except Exception:
            pass
    return added


async def _run_real(node_id: str) -> None:
    """用真实模型流式执行一个节点：逐步写入 messages/progress，完成后 done + 发布上下文。"""
    db = SessionLocal()
    try:
        node = db.get(Node, node_id)
        if not node or node.status != "running":
            return
        provider = _pick_provider(db, (node.__dict__.get("model") or None))
        if not provider:
            return
        sess = db.get(Session, node.sessionId)
        workspace = (sess.workspace if sess and sess.workspace else None) or os.getcwd()

        # 本次执行的事件日志（借鉴 dsh durable session log）：追加写，turn_start 开启新 run。
        # 供回放、审计、retry 恢复（上一轮事件被后续 re-run 覆盖，turn_start 自增 run 区分）。
        _elog = EventLog(node.sessionId, node.nodeId)
        _prior_events = _elog.read_all()
        _elog.turn_start(node.title or "", node.inputText or "")

        # 父节点产出作为上下文：读取每个父节点的「内容块」历史（按时间追加，块级摘要索引）
        parent_outputs = []
        for pid in node.parentIds or []:
            p = db.get(Node, pid)
            if not p:
                continue
            blocks = (
                db.query(ContextBlock)
                .filter(ContextBlock.sessionId == node.sessionId, ContextBlock.nodeId == pid)
                .order_by(ContextBlock.seq.asc())
                .all()
            )
            if blocks:
                for b in blocks:
                    parent_outputs.append(f"[{p.title} #{b.seq}] {b.summary}")
            elif p.output and p.output.get("summary"):
                # 兼容旧数据（尚无内容块）：退回最新摘要
                parent_outputs.append(f"[{p.title}] {p.output['summary']}")

        history = list(node.messages or [])[:-1]  # 去掉刚追加的空 assistant
        # 取最后一条 user 消息作为请求
        user_text = node.inputText or ""
        if history:
            last_user = next((m for m in reversed(history) if m.get("role") == "user"), None)
            if last_user:
                user_text = last_user.get("text", "")

        cfg_base = provider.baseUrl.rstrip("/")
        provider_model = (node.__dict__.get("model") or None) or ((provider.models or [])[0] if provider.models else "default")

        # 上下文实时压缩（token 压力驱动，对齐 dsh compaction）：长对话超预算时
        # 把更早历史折叠成 <compacted-summary> 摘要，保留最近 tail。
        try:
            from .executor import compact_history
            history = await compact_history(
                cfg_base, provider.pick_api_key(), provider_model, history,
                keep_recent=6, max_old_chars=6000,
                budget_tokens=24000,  # 历史部分 token 预算（其余留给系统提示/工具/当前轮）
            )
        except Exception:
            pass  # 压缩失败不影响主流程

        # 加载 skill 能力清单（内置默认 + 用户自定义目录）
        from .models import AppConfig
        from .skills import load_all_skills_prompt
        skill_row = db.query(AppConfig).filter(AppConfig.key == "skill_dir").first()
        skills_text = load_all_skills_prompt(skill_row.value if skill_row else None)

        # 开工主动加载项目上下文（README/约定 + 结构），让 agent 动手前先理解项目
        project_context = ""
        try:
            from .project_scan import get_project_context
            project_context = get_project_context(workspace)
        except Exception:
            project_context = ""

        messages = build_messages(node.title, user_text, history, parent_outputs, node.memory, skills_text, project_context, workspace)

        # 若为重试/继续（存在上一轮事件日志且非空），从上一轮事件重建"前情摘要"，
        # 注入上下文，让 agent 基于上次进展和失败点继续，而不是盲目重来。
        # 从事件日志重建最近一次 run 的 LLM 消息（原文 + 工具结果），供模型看到上次实际做过什么。
        try:
            prior_runs = _elog.runs()
            if len(prior_runs) >= 2 and _prior_events:
                # runs() 给出每次执行的 (run, start_seq, status)；取上一条已结束 run 的事件
                prev_run = prior_runs[-2]
                if prev_run and prev_run[0] > 0:
                    prev_events = [e for e in _prior_events if e.get("run", 0) == prev_run[0]]
                    prev_status = prev_run[2] or "unknown"
                    if prev_events:
                        rebuild = []
                        try:
                            from .event_log import rebuild_context as _rebuild
                            rebuild = _rebuild(prev_events, keep_recent_steps=6, compress=False)
                        except Exception:
                            rebuild = []
                        prev_text = "\n".join(
                            str(m.get("text") or m.get("content") or "")
                            for m in rebuild[-8:]
                            if m.get("text") or m.get("content")
                        )[:2500]
                        # 去掉头部 system/用户任务重复，聚焦上次执行情况
                        messages.append(
                            {
                                "role": "system",
                                "content": (
                                    f"【上一次执行（状态：{prev_status}）的进展与动作，供继续参考，不要重复已完成部分】\n"
                                    f"{prev_text or '（无有效可参考内容）'}"
                                ),
                            }
                        )
        except Exception:
            pass  # 事件日志恢复失败不影响主流程

        # 动态上下文注入（对齐 dsh runtime context）：当前时间/工作目录/git 状态/平台
        try:
            from .context import dynamic_context
            dyn = dynamic_context(workspace)
            if dyn.strip():
                messages.append({"role": "system", "content": f"【运行时上下文】\n{dyn}"})
        except Exception:
            pass

        tools = openai_tools()
        # 合并已启用的 MCP server 工具（第三方工具生态）
        mcp_tools_by_name: dict[str, str] = {}  # 工具名 -> server 名
        try:
            from .mcp_manager import list_mcp_tools
            mcp_all = await list_mcp_tools()
            for mt in mcp_all:
                if mt.get("name") == "__error__" or not mt.get("name"):
                    continue
                mcp_tools_by_name[mt["name"]] = mt["server"]
                tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"mcp__{mt['server']}__{mt['name']}",
                            "description": mt.get("description", ""),
                            "parameters": mt.get("input_schema") or {"type": "object", "properties": {}},
                        },
                    }
                )
        except Exception:
            pass
        tool_ctx = {"sessionId": node.sessionId, "parentIds": node.parentIds or []}

        # ---- 计划审批前置：流式生成计划时"尽早"弹审批，不必等整段计划收尾 ----
        _plan_appr_done = False       # 审批是否已 request 过（防重复弹）
        _abort_by_approval = False    # 用户拒绝→中断本轮执行

        async def _early_plan_approval(steps_part: list[str]) -> None:
            nonlocal _plan_appr_done, _abort_by_approval
            if _plan_appr_done:
                return
            # 用已解析出的步骤预判是否需计划审批（复用判据：步骤≥3 或 user_text 含敏感词）
            pseudo = {"steps": [{"label": s, "status": "pending"} for s in steps_part]}
            if not _task_needs_plan_approval(pseudo, user_text) or not steps_part:
                return
            _plan_appr_done = True
            steps_text = "\n".join(f"- {s}" for s in steps_part)
            summary = f"任务计划（{len(steps_part)} 步）：\n{steps_text}"
            ap = await _request_approval(db, node, "plan", {"plan": summary[:2000]})
            decision = await _wait_approval(db, ap.id)
            if decision == "rejected":
                await _settle_approval(db, ap.id)
                _abort_by_approval = True
                await publish(
                    node.sessionId,
                    {"type": "node_update", "nodeId": node.nodeId, "status": "failed",
                     "plan": {"goal": "", "steps": [{"label": s, "status": "pending"} for s in steps_part]},
                     "messages": node.messages, "planRejected": True},
                )
            else:
                await publish(
                    node.sessionId,
                    {"type": "node_update", "nodeId": node.nodeId, "planApproved": True},
                )

        # 生成执行计划并推送前端（对齐主流 Planning agent）
        node.plan = await generate_plan(
            cfg_base, provider.pick_api_key(), provider_model,
            user_text, parent_outputs, node.memory,
            on_steps=_early_plan_approval,
        )
        db.commit()
        await publish(
            node.sessionId,
            {
                "type": "node_update",
                "nodeId": node.nodeId,
                "status": node.status,
                "plan": node.plan,
                "messages": node.messages,
            },
        )
        if _abort_by_approval:
            # 用户拒绝计划并给了原因 ->停止执行该节点
            from .models import Session as _Sess
            _sn = db.get(_Sess, node.sessionId)
            try:
                node.status = "failed"
                node.output = {"type": "text", "summary": "用户拒绝执行该任务计划", "content": "任务已被用户拒绝,未执行。", "artifacts": []}
                db.commit()
                await publish(node.sessionId, {"type": "node_update", "nodeId": node.nodeId, "status": "failed",
                                               "output": node.output})
                return
            except Exception:
                pass
        # 未在流式阶段早弹、但最终计划满足判据(如非敏感但≥3步)的任务：仍按原机制确认一次
        if not _plan_appr_done:
            await _maybe_plan_approval(db, node, user_text, messages, provider, provider_model)

        # 确保存在一条 assistant 占位
        msgs = list(node.messages or [])
        if not msgs or msgs[-1].get("role") != "assistant":
            msgs.append({"id": f"m-{node_id}-a", "role": "assistant", "text": "", "streaming": True})
            node.messages = msgs
            db.commit()

        # 记录本节点已同步到会话 usage 的 token，避免重复累加（用于实时费用/余额）
        _usage_synced = {"p": 0, "c": 0}

        async def flush(text: str, streaming: bool):
            msgs = list(node.messages or [])
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "assistant":
                    msgs[i] = {**msgs[i], "text": text, "streaming": streaming}
                    break
            node.messages = msgs
            # token 实时估算（prompt 按 messages 序列化长度，completion 按已生成字符）
            try:
                prompt_chars = sum(len(str(m2.get("content") or m2.get("text") or "")) for m2 in messages)
                prev = node.progress or {}
                node.progress = {
                    **prev,
                    "promptTokens": max(int(prompt_chars / 3), prev.get("promptTokens") or 0),
                    "completionTokens": max(int(len(acc) / 3), prev.get("completionTokens") or 0),
                    "tokens": int((prompt_chars + len(acc)) / 3),
                    "elapsedMs": int((time.monotonic() - prev.get("startedAt", time.monotonic())) * 1000),
                }
                # 把本节点实时 token 同步到会话 usage（增量累计，供实时费用/余额）
                try:
                    cur_p = int(node.progress.get("promptTokens") or 0)
                    cur_c = int(node.progress.get("completionTokens") or 0)
                    dp = cur_p - _usage_synced["p"]
                    dc = cur_c - _usage_synced["c"]
                    if (dp > 0 or dc > 0) and sess:
                        _usage_synced["p"] = cur_p
                        _usage_synced["c"] = cur_c
                        usage = dict(sess.usage or {})
                        np = usage.get("promptTokens", 0) + dp
                        nc = usage.get("completionTokens", 0) + dc
                        usage["promptTokens"] = np
                        usage["completionTokens"] = nc
                        from .pricing import compute_cost
                        usage["cost"] = round(compute_cost(provider_model, np, nc), 6)
                        sess.usage = usage
                        sess.updatedAt = _now_dt()
                        db.commit()
                        await publish(
                            node.sessionId,
                            {
                                "type": "usage",
                                "promptTokens": np,
                                "completionTokens": nc,
                                "cost": usage["cost"],
                                "budget": sess.budget,
                            },
                        )
                except Exception:
                    pass
            except Exception:
                pass
            db.commit()
            await publish(
                node.sessionId,
                {
                    "type": "node_update",
                    "nodeId": node.nodeId,
                    "status": node.status,
                    "progress": node.progress,
                    "messages": node.messages,
                },
            )

        def cancelled() -> bool:
            # 协作式取消：检查 DB 状态（有人 cancel）或取消集合
            db.expire_all()
            cur = db.get(Node, node_id)
            if not cur:
                return True
            if node_id in _cancel_requested or cur.status == "cancelled":
                return True
            return False

        async def flush_step(label: str, detail: str = ""):
            """在聊天流里追加一条系统步骤消息（任务流）。"""
            msgs = list(node.messages or [])
            # 移除还在流式的 assistant 占位，换成步骤消息（保持顺序）
            msgs.append(
                {
                    "id": _gen_step_id(),
                    "role": "system",
                    "step": label,
                    "detail": detail,
                    "at": int(time.time() * 1000),
                }
            )
            node.messages = msgs
            db.commit()
            await publish(
                node.sessionId,
                {
                    "type": "node_update",
                    "nodeId": node.nodeId,
                    "status": node.status,
                    "progress": node.progress,
                    "messages": node.messages,
                },
            )

        async def set_plan_step(idx: int, status: str):
            """标记计划步骤状态并推送。"""
            import copy
            plan = copy.deepcopy(node.plan or {})  # 深拷贝为新对象，确保 SQLAlchemy 检测 JSON 变更并落库
            steps = list(plan.get("steps") or [])
            if 0 <= idx < len(steps):
                steps[idx] = {**steps[idx], "status": status}
                plan["steps"] = steps
                node.plan = plan
                # 计划完成度：已完成步骤 / 总步骤
                done = sum(1 for s in steps if s.get("status") == "done")
                node.progress = {
                    **(node.progress or {}),
                    "stepDone": done,
                    "stepTotal": len(steps),
                }
                db.commit()
                await publish(
                    node.sessionId,
                    {
                        "type": "node_update",
                        "nodeId": node.nodeId,
                        "status": node.status,
                        "plan": node.plan,
                        "progress": node.progress,
                        "messages": node.messages,
                    },
                )

        def plan_index() -> int:
            """下一个待执行步骤下标（无计划或全部完成返回 -1）。"""
            steps = (node.plan or {}).get("steps") or []
            for i, s in enumerate(steps):
                if s.get("status") != "done":
                    return i
            return -1

        # Agent 循环：流式；每轮累积 content，若出现 tool_calls 则执行工具并回传
        acc = ""
        total_prompt = 0
        total_completion = 0
        max_rounds = 15  # 收敛优先：多数任务 15 轮内足够，避免无谓 LLM 往返
        fail_counts: dict[str, int] = {}
        rounding_stall = 0  # 连续"无效轮"计数，≥2 提前收尾防空转
        pending_tool_ids = 0

        def is_failure(result: str) -> bool:
            s = (result or "").strip()
            return bool(s) and (
                s.startswith("工具执行出错")
                or s.startswith("执行失败")
                or s.startswith("用户拒绝了工具")
                or "不存在" in s
                or "失败" in s[:20]
            )

        async def push_reflection(tool_name: str, result: str):
            """工具失败后注入反思提示，让模型换策略重试（同一工具累计不超过 2 次）。

            超过阈值时不再死磕：引导模型评估「是否值得继续 / 是否可回滚已做改动」，
            若不宜继续则停止该路线，并把不确定部分明确标为「待确认」，回到主流程推进。
            对齐 dsh 的"对不确定处明确标注 / 自我验证"行为。
            """
            fail_counts[tool_name] = fail_counts.get(tool_name, 0) + 1
            if fail_counts[tool_name] <= 2:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"工具 {tool_name} 的执行结果：{result[:500]}\n\n"
                            "【失败反思】请先分析失败原因（可能是参数错误、路径不存在、权限、外部依赖、思路本身不对等），"
                            "然后换一种【不同的】方法重试——不要重复完全相同的调用。若换了策略仍失败，"
                            "下一轮就停止该路线的纠缠，转入收尾。"
                        ),
                    }
                )
                await flush_step("反思重试", tool_name)
            else:
                # 该工具已持续失败：引导模型评估回滚/待确认，回到主流程
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"工具 {tool_name} 仍无法成功（已多次尝试）。请停止在这一点上继续纠缠：\n"
                            "1) 评估刚才失败的改动是否已对工作区造成副作用；若有必要，评估是否回滚/清理已做的改动；\n"
                            "2) 把无法完成或不确定的部分**明确标为「待确认」**，说明卡点和已知信息；\n"
                            "3) 基于已完成的部分，继续推进剩余任务或给出结论，不要无限重试同一个失败点。"
                        ),
                    }
                )
                await flush_step("放弃该路线", tool_name)
        for _round in range(max_rounds):
            if cancelled():
                break
            # 标记当前计划步骤 running（若存在计划）
            cur_step = plan_index()
            if cur_step >= 0:
                await set_plan_step(cur_step, "running")
            # 事件日志：本轮开始
            _elog.step_start(_round)

            round_text = ""
            tool_calls: list[dict] = []
            last_reasoning = ""
            deferred_reflections: list[tuple[str, str]] = []
            async for ev in stream_chat_with_tools(cfg_base, provider.pick_api_key(), provider_model, messages, tools, temperature=0.2):
                if cancelled():
                    break
                if ev["type"] == "delta":
                    round_text += ev["text"]
                    acc += ev["text"]
                    # 逐 token 流式刷新
                    all_text = acc
                    await flush(all_text, True)
                elif ev["type"] == "done":
                    tool_calls = ev["tool_calls"]
                    last_reasoning = ev.get("reasoning_content", "")

            if cancelled():
                break
            # 事件日志：本轮模型产出（文本 + 工具调用 + token）
            _elog.llm_done(round_text, last_reasoning, tool_calls)
            await _publish_exec_event(db, node)
            # 本轮完成（产生工具或文本）→ 当前计划步骤 done
            if cur_step >= 0:
                await set_plan_step(cur_step, "done")

            if tool_calls:
                # 把本轮 assistant 工具调用写入 messages，再逐个执行
                await flush_step("调用工具", f"{len(tool_calls)} 个工具")
                assistant_tool_msg = {"role": "assistant", "content": None, "tool_calls": tool_calls}
                # deepseek thinking 模式要求把 reasoning_content 原样回传，否则下一轮 400
                if last_reasoning:
                    assistant_tool_msg["reasoning_content"] = last_reasoning
                messages.append(assistant_tool_msg)

                # ---- 并行执行：把本轮"可并行安全"的独立工具先并发跑，最后统一收集 ----
                # 判定：只读/独立、无审批、非状态类（读写文件/子代理/审批/MCP/特殊工具不并行）
                _PARALLEL_SAFE = {
                    "read", "read_file", "list_dir", "search_files", "grep_content",
                    "web_search", "web_fetch", "memory_search", "read_parent_output",
                    "search_parent_memory", "skill", "get_goal", "remember",
                }
                _parallel_results: dict[str, str] = {}
                _subagent_tasks: dict[str, dict] = {}  # tool_call_id -> parsed(run_subagent)
                _batch: list[tuple[str, str, dict]] = []  # (tool_call_id, name, parsed)
                _subagent_run_ids: list[str] = []  # 本轮需要并行执行的 run_subagent 的 tool_call_id
                for tc in tool_calls:
                    _fn = tc.get("function", {})
                    _n = _fn.get("name", "")
                    _raw = _fn.get("arguments") or "{}"
                    try:
                        _parsed = _json.loads(_raw)
                    except Exception:
                        _parsed = {}
                    if _n == "run_subagent":
                        # 多个独立子任务：父节点(主agent)等待、子代理之间并行（父与子不同时执行其他任务）
                        _subagent_run_ids.append(tc.get("id", ""))
                        _subagent_tasks.setdefault(tc.get("id", ""), _parsed)
                        continue
                    if _n not in _PARALLEL_SAFE:
                        continue
                    # 只并行真正不需要审批的（当前这些只读工具恒不审批）
                    if needs_approval(_n, _parsed):
                        continue
                    _batch.append((tc.get("id", ""), _n, _parsed))
                # 子代理并行：同轮多个 run_subagent 用独立 DB session 并发执行（父等待全部完成后收集）
                if _subagent_run_ids:
                    await flush_step("并行子代理", f"{len(_subagent_run_ids)} 个独立子任务")
                    from .db import SessionLocal as _SubSessionLocal

                    async def _run_subparallel(tid: str):
                        _sp = _subagent_tasks.get(tid, {})
                        _sdb = _SubSessionLocal()
                        try:
                            _r = await _run_subagent(
                                _sdb, node, provider, provider_model, workspace,
                                _sp.get("task", ""), tool_ctx, int(_sp.get("max_rounds") or 4),
                            )
                            return tid, _r
                        except Exception as _e:  # noqa: BLE001
                            return tid, f"子代理执行出错：{_e}"
                        finally:
                            _sdb.close()

                    _sub_results = await asyncio.gather(*[_run_subparallel(t) for t in _subagent_run_ids])
                    for _tid, _tres in _sub_results:
                        _parallel_results[_tid] = _tres
                        _elog.tool_call("run_subagent", {}, False)
                        _elog.tool_result("run_subagent", not is_failure(_tres), (_tres or "")[:2000], 0)
                    await flush_step("子代理结果", f"收集 {len(_sub_results)} 个子任务结果")
                    await _publish_exec_event(db, node)
                if _batch:
                    await flush_step("并行执行", f"{len(_batch)} 个独立工具")
                    async def _run_one(item):
                        _id, _n, _p = item
                        try:
                            return _id, _n, await execute(_n, _p, workspace, tool_ctx)
                        except Exception as _e:
                            return _id, _n, f"工具执行出错：{_e}"
                    _par_results = await asyncio.gather(*[_run_one(t) for t in _batch])
                    for _tid, _tn, _tres in _par_results:
                        _parallel_results[_tid] = _tres
                        _elog.tool_call(_tn, {}, False)
                        _elog.tool_result(_tn, not is_failure(_tres), (_tres or "")[:2000], 0)
                    await flush_step("并行结果", f"收集 {len(_par_results)} 个结果")
                    await _publish_exec_event(db, node)

                for tc in tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    raw_args = fn.get("arguments") or "{}"
                    import json as _json
                    try:
                        parsed = _json.loads(raw_args)
                    except Exception:
                        parsed = {}
                    if name == "todo_write":
                        # 待办清单：整份替换，映射到 node.plan.steps 持久化并推送前端（对齐 dsh todo_write）
                        await _handle_todo_write(db, node, parsed)
                        result = "待办清单已更新。"
                        messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": f"待办清单已更新：{_todo_summary(parsed)}"})
                        continue
                    if tc.get("id", "") in _parallel_results:
                        # 该工具已在并行批次执行，直接使用已收集的结果
                        _pres = _parallel_results.get(tc.get("id", ""), "")
                        messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": _pres})
                        if is_failure(_pres):
                            deferred_reflections.append((name, _pres))
                        continue
                    if name == "create_goal":
                        # 设置长期目标（对齐 dsh goal）：存进 node.memory.goal，agent 后续轮次持续记住
                        objective = str(parsed.get("objective", "")).strip()
                        if objective:
                            mem = dict(node.memory or {})
                            mem["goal"] = objective
                            mem["goal_round"] = 0  # goal 自动延续：从第 0 轮开始
                            if not mem.get("goal_max_rounds"):
                                mem["goal_max_rounds"] = 4  # 默认最多推进 4 轮
                            node.memory = mem
                            node.messages = node.messages or []
                            db.commit()
                            await publish(node.sessionId, {"type": "node_update", "nodeId": node.nodeId,
                                                            "status": node.status, "memory": node.memory, "messages": node.messages})
                        messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                         "content": f"已设定持续目标：{objective[:120]}"})
                        continue
                    if name == "get_goal":
                        g = (node.memory or {}).get("goal") if node.memory else None
                        result = f"当前持续目标：{g}" if g else "当前没有设定持续目标。"
                        messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": result})
                        continue
                    if name == "schedule_create":
                        # 创建定时提醒：存到 node.memory.schedules，到点由 tick 注入为新的用户消息触发下一轮
                        prompt = str(parsed.get("prompt", "")).strip()
                        after = int((parsed.get("after_seconds") or 0) or 0)
                        every = int((parsed.get("every_seconds") or 0) or 0)
                        if not prompt:
                            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": "schedule_create 需要非空 prompt"})
                            continue
                        if after <= 0 and every <= 0:
                            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": "schedule_create 需要 after_seconds 或 every_seconds"})
                            continue
                        import uuid as _uu
                        sid = "sched-" + _uu.uuid4().hex[:8]
                        now = time.time()
                        every = every if every >= 300 else 0  # 周期最少 300s
                        mem = dict(node.memory or {})
                        sched = list(mem.get("schedules") or [])
                        sched.append({
                            "id": sid,
                            "prompt": prompt[:500],
                            "due_ts": now + (after or every),
                            "every": every,
                            "last_ts": None,
                        })
                        mem["schedules"] = sched
                        node.memory = mem
                        node.messages = node.messages or []
                        db.commit()
                        await publish(node.sessionId, {"type": "node_update", "nodeId": node.nodeId,
                                                        "status": node.status, "memory": node.memory, "messages": node.messages})
                        messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                         "content": f"已创建定时提醒 {sid}（after={after}s, every={every}s）：{prompt[:80]}"})
                        continue
                    if name == "ask_user":
                        # 向用户提问（对齐 dsh ask_user）：把问题写进消息流，节点置 paused 等待用户回复；
                        # 用户在会话里回复(send_message)后节点回到 running 继续执行。
                        await _ask_user_and_pause(db, node, parsed)
                        return
                    if name == "run_subagent":
                        await flush_step("子代理", parsed.get("task", "")[:80])
                        try:
                            result = await _run_subagent(
                                db, node, provider, provider_model, workspace,
                                parsed.get("task", ""), tool_ctx, int(parsed.get("max_rounds") or 4),
                            )
                        except Exception as e:  # noqa: BLE001
                            result = f"子代理执行出错：{e}"
                        await flush_step("子代理结果", (result or "")[:120])
                        messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": result})
                        continue
                    if name.startswith("mcp__"):
                        # MCP 工具：mcp__{server}__{tool}
                        parts = name.split("__", 2)
                        if len(parts) == 3:
                            server_name, tool_name = parts[1], parts[2]
                            await flush_step("MCP工具", f"{server_name}.{tool_name}")
                            try:
                                from .mcp_manager import call_mcp_tool
                                result = await call_mcp_tool(server_name, tool_name, parsed)
                            except Exception as e:  # noqa: BLE001
                                result = f"MCP 工具执行出错：{e}"
                            await flush_step("MCP结果", (result or "")[:120])
                            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": result})
                            continue
                    tool_def = TOOL_BY_NAME.get(name)
                    # 事件日志：工具调用（含是否需审批）
                    _elog.tool_call(name, parsed, bool(tool_def and needs_approval(name, parsed)))
                    if tool_def and needs_approval(name, parsed):
                        await flush_step("待审批", f"{name} {raw_args[:80]}")
                        ap = await _request_approval(db, node, name, parsed)
                        decision = await _wait_approval(db, ap.id)
                        if decision == "rejected":
                            result = f"用户拒绝了工具 {name} 的调用"
                            await flush_step("已拒绝", name)
                        else:
                            await flush_step("执行工具", f"{name}({_brief_args(parsed)})")
                            try:
                                result = await execute(name, parsed, workspace, tool_ctx)
                            except Exception as e:  # noqa: BLE001
                                result = f"工具执行出错：{e}"
                            await flush_step("工具结果", (result or "")[:120])
                    else:
                        await flush_step("执行工具", f"{name}({_brief_args(parsed)})")
                        try:
                            result = await execute(name, parsed, workspace, tool_ctx)
                        except Exception as e:  # noqa: BLE001
                            result = f"工具执行出错：{e}"
                        await flush_step("工具结果", (result or "")[:120])
                    # 事件日志：工具结果
                    _elog.tool_result(name, not is_failure(result), (result or "")[:2000], 0)
                    await _publish_exec_event(db, node)
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": result})
                    if is_failure(result):
                        # 先收集反思，不在 tool_calls 中间插入 user 消息，
                        # 等本轮所有 tool 结果都 append 到位后再统一注入（否则 deepseek 400）
                        deferred_reflections.append((name, result))
                # 兜底：确保每个 tool_call_id 都有对应 tool 消息（缺了补空结果），
                # 避免 deepseek 报「insufficient tool messages following tool_calls」
                tc_ids = {tc.get("id", "") for tc in tool_calls if tc.get("id")}
                existing_ids = {
                    m.get("tool_call_id")
                    for m in messages
                    if m.get("role") == "tool" and m.get("tool_call_id")
                }
                for missing in tc_ids - existing_ids:
                    messages.append({"role": "tool", "tool_call_id": missing, "content": "（该工具未返回结果）"})
                # 本轮所有 tool 消息已就位，此时再注入反思 user 消息，不破坏 tool_calls→tool 的相邻顺序
                for rname, rresult in deferred_reflections:
                    # 事件日志：失败反思
                    _elog.reflection(rname, (rresult or "")[:500])
                    await push_reflection(rname, rresult)
                continue  # 继续下一轮
            else:
                # 本轮没有工具调用：保留已有 acc（可能是空，说明模型直接给文本或结束）
                # 若 acc 为空且 round_text 也为空，说明模型没给内容也没工具，直接结束
                if not acc:
                    await flush_step("完成", "")
                break

        if cancelled() or node_id in _cancel_requested or (db.get(Node, node_id) or node).status == "cancelled":
            _cancel_requested.discard(node_id)
            print(f"[cancel] 执行循环检测到取消，节点 {node_id} 正在收尾", flush=True)
            cur = db.get(Node, node_id)
            if cur:
                cur.status = "cancelled"
                cur.messages = node.messages
                # 结束流式占位
                msgs = list(cur.messages or [])
                for i in range(len(msgs) - 1, -1, -1):
                    if msgs[i].get("role") == "assistant":
                        msgs[i] = {**msgs[i], "streaming": False}
                        break
                cur.messages = msgs
                db.commit()
                # 事件日志：turn 结束（取消）
                _elog.turn_end("cancelled", "", None)
                await publish(
                    node.sessionId,
                    {
                        "type": "node_update",
                        "nodeId": node_id,
                        "status": "cancelled",
                        "messages": cur.messages,
                    },
                )
            return

        final_text = acc or "（本次执行无文本输出）"
        await flush(final_text, False)
        # 把最终一轮的 reasoning_content 写进最后一条 assistant 消息，供下一轮回传
        if last_reasoning:
            msgs = list(node.messages or [])
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "assistant":
                    msgs[i] = {**msgs[i], "reasoning_content": last_reasoning}
                    break
            node.messages = msgs

        # 计划收尾：把剩余步骤全部标记完成
        import copy
        final_plan = copy.deepcopy(node.plan or {})  # 新对象，确保 SQLAlchemy 检测变更并持久化 plan
        steps = list(final_plan.get("steps") or [])
        for i in range(len(steps)):
            if steps[i].get("status") != "done":
                steps[i] = {**steps[i], "status": "done"}
        final_plan["steps"] = steps
        node.plan = final_plan

        # 更新卡片滚动记忆：仅当确有较多内容时用 LLM 折叠（省一次额外请求，提速短任务）
        # 短任务/输出少时跳过压缩，直接在失败/无文本时也无损。
        try:
            _should_roll = int(total_completion or 0) > 800 or ((node.messages or []).__len__() > 20)
            if _should_roll:
                new_memory = await _rolling_memory(provider, provider_model, node.memory, user_text, final_text)
                if new_memory:
                    node.memory = new_memory
        except Exception:
            pass

        # 累计用量与费用
        cost = compute_cost(provider_model, total_prompt, total_completion)
        if sess:
            usage = dict(sess.usage or {})
            usage["promptTokens"] = usage.get("promptTokens", 0) + total_prompt
            usage["completionTokens"] = usage.get("completionTokens", 0) + total_completion
            usage["cost"] = round(usage.get("cost", 0.0) + cost, 6)
            sess.usage = usage
            sess.updatedAt = _now_dt()

        # ---- goal 自动延续（对齐 dsh goal-round-driver）：目标未完成则自动进入下一轮 ----
        _mem = dict(node.memory or {})
        _goal = _mem.get("goal")
        if _goal and not _goal_done_marked(final_text) and not cancelled():
            _round = int(_mem.get("goal_round") or 0) + 1
            _max = int(_mem.get("goal_max_rounds") or 4)
            if _round <= _max:
                # 新一轮继续推进；保留目标字段，避免 _rolling_memory 覆盖
                _mem["goal"] = _goal
                _mem["goal_round"] = _round
                node.memory = _mem
                msgs = list(node.messages or [])
                msgs.append({"id": _gen_step_id(), "role": "user",
                             "text": f"[目标延续 第{_round}轮] 目标尚未达成。请基于本轮进展继续推进目标「{str(_goal)[:120]}」，不要重复已完成部分；若本轮彻底达成目标，请以 [GOAL_DONE] 结束。"})
                node.messages = msgs
                node.status = "running"
                db.commit()
                await publish(node.sessionId, {"type": "node_update", "nodeId": node.nodeId,
                                               "status": "running", "memory": node.memory, "messages": node.messages, "goalContinuation": _round})
                _real_exec_tasks.pop(node_id, None)
                return

        # 成功收尾时：把本次的经验（工具用法/涉及文件）自动回写到项目级记忆，供下次任务按需加载
        try:
            await _record_experiential(db, node, workspace)
        except Exception:
            pass

        node.status = "done"
        node.output = {
            "type": "text",
            "summary": final_text[:200] or f"已完成「{node.title}」",
            "content": final_text,
            "artifacts": [],
        }
        await publish_output_to_context(db, node)
        db.commit()
        # 事件日志：turn 结束（成功）
        _elog.turn_end("done", final_text, None)
        await publish(
            node.sessionId,
            {
                "type": "node_update",
                "nodeId": node.nodeId,
                "status": "done",
                "progress": node.progress,
                "plan": node.plan,
                "messages": node.messages,
                "output": node.output,
            },
        )
    except Exception as e:  # noqa: BLE001
        # 执行失败：置 failed
        node = db.get(Node, node_id)
        if node and node.status == "running":
            node.status = "failed"
            node.failedReason = f"执行失败：{e}"
            db.commit()
            # 事件日志：turn 结束（失败）
            _elog.turn_end("failed", "", None)
            await publish(
                node.sessionId,
                {"type": "node_update", "nodeId": node_id, "status": "failed",
                 "progress": node.progress, "messages": node.messages},
            )
    finally:
        db.close()
        _real_exec_tasks.pop(node_id, None)


async def _run_subagent(
    db, parent_node: Node, provider, model: str, workspace: str,
    task: str, tool_ctx: dict, max_rounds: int,
) -> str:
    """运行一个子代理（同模型、可读写工具、限轮、审批复用父卡片）。"""
    from .executor import stream_chat_with_tools

    sys = (
        "你是主 agent 派出的聚焦子代理，负责一个完全独立的子任务。你不共享主 agent 的对话，"
        "任务说明里已包含你需要的全部上下文。遵循「先探测、后行动」：\n"
        "1) 先用工具核实事实（read/search/web_search 等），再下结论或动手；不确定不要臆造。\n"
        "2) 复杂任务分步执行、失败分析原因换策略，不要盲目重复同一操作。\n"
        "3) 需要信息用只读工具，写文件/执行命令是敏感操作会请求审批。\n"
        "4) 不要调用 run_subagent（禁止嵌套子代理）。\n"
        "5) 完成时用中文返回一个【精炼结论】：先说结论，再给关键过程（含用到的文件/工具），"
        "不要输出中间思考的每一步——主 agent 只需要你的最终结果。"
    )
    messages = [
        {"role": "system", "content": sys},
        {"role": "system", "content": f"工作目录：{workspace}"},
        {"role": "user", "content": f"子任务：{task}"},
    ]
    # 动态上下文（时间/git 状态等）
    try:
        from .context import dynamic_context
        dyn = dynamic_context(workspace)
        if dyn.strip():
            messages.append({"role": "system", "content": f"【运行时上下文】\n{dyn}"})
    except Exception:
        pass
    tools = openai_tools()
    acc = ""
    for _ in range(max(1, min(max_rounds, 6))):
        tool_calls: list[dict] = []
        reasoning = ""
        async for ev in stream_chat_with_tools(provider.baseUrl.rstrip("/"), provider.pick_api_key(), model, messages, tools, temperature=0.2):
            if ev["type"] == "delta":
                acc += ev["text"]
            elif ev["type"] == "done":
                tool_calls = ev["tool_calls"]
                reasoning = ev.get("reasoning_content", "")
        if not tool_calls:
            break
        assistant_msg = {"role": "assistant", "content": None, "tool_calls": tool_calls}
        if reasoning:
            assistant_msg["reasoning_content"] = reasoning
        messages.append(assistant_msg)
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            raw = fn.get("arguments") or "{}"
            import json as _json
            try:
                parsed = _json.loads(raw)
            except Exception:
                parsed = {}
            if name == "run_subagent":
                result = "（禁止嵌套子代理）"
            elif needs_approval(name, parsed):
                ap = await _request_approval(db, parent_node, name, parsed)
                decision = await _wait_approval(db, ap.id)
                result = f"用户拒绝了工具 {name}" if decision == "rejected" else await execute(name, parsed, workspace, tool_ctx)
            else:
                try:
                    result = await execute(name, parsed, workspace, tool_ctx)
                except Exception as e:  # noqa: BLE001
                    result = f"工具执行出错：{e}"
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": result})
    return acc.strip() or "（子代理无文本产出）"


async def tick() -> None:
    """每 TICK_SECONDS 推进一次状态机。

    原型演示用 time.monotonic() 判断运行时长，不引入额外表。
    """
    db = SessionLocal()
    try:
        nodes = db.query(Node).all()
        by_id = {n.nodeId: n for n in nodes}
        changed_ids: set[str] = set()

        running_count = sum(1 for n in nodes if n.status == "running")
        real_dispatch: list[str] = []

        now = time.monotonic()

        # ready -> running（并发上限）
        for n in nodes:
            if n.status == "ready" and running_count < CONCURRENCY:
                # 父子互斥：若父链或子链上有正在运行的节点，则暂不启动（保持 ready 等待）
                try:
                    status_of = {x.nodeId: x.status for x in nodes}
                    parents_of = {x.nodeId: set(x.parentIds or []) for x in nodes}
                    recur = running_relatives({n.nodeId}, status_of, parents_of)
                except Exception:
                    recur = []
                # 注意：n 自身还没 running；这里需要把"此刻已在 running 的亲人"去掉自身干扰
                if recur:
                    # 有父/子在跑 → 不调度，等其完成
                    continue
                n.status = "running"
                # 记录节点读上下文基线：启动时每个 key 的版本快照（§4.2.1 读模型）
                n.baseContext = {
                    e.key: e.version
                    for e in db.query(ContextEntry).filter(ContextEntry.sessionId == n.sessionId).all()
                }
                if _model_configured(db):
                    # 真实执行：交给异步流式任务，本 tick 只标记 running
                    n.progress = {
                        **(n.progress or {}),
                        "real": True,
                        "startedAt": now,
                        "tokens": 0,
                        "logs": ["开始执行（真实模型）…"],
                    }
                    running_count += 1
                    changed_ids.add(n.nodeId)
                    if n.nodeId not in _real_exec_tasks:
                        real_dispatch.append(n.nodeId)
                else:
                    # mock 执行（未配置模型时的回退）
                    n.progress = {
                        **(n.progress or {}),
                        "startedAt": now,
                        "expectedMs": int(_runtime_seconds(n) * 1000),
                        "tokens": 0,
                        "logs": [f"开始执行「{n.title}」…"],
                    }
                    running_count += 1
                    changed_ids.add(n.nodeId)

        # running -> done / failed
        for n in nodes:
            # 真实执行的节点：若任务仍在则跳过；若任务已丢失（如重启后孤儿），重新派发
            if n.status == "running" and (n.progress or {}).get("real"):
                if n.nodeId not in _real_exec_tasks:
                    real_dispatch.append(n.nodeId)
                continue
            if n.status == "running" and n.progress and "startedAt" in n.progress:
                started = n.progress["startedAt"]
                expected_ms = n.progress.get("expectedMs", 3000)
                elapsed = now - started
                # 累加 token（模拟）
                n.progress = {
                    **n.progress,
                    "tokens": int(elapsed * 80),
                    "elapsedMs": int(elapsed * 1000),
                }
                # 流式填充最后一条 assistant 消息
                msgs = list(n.messages or [])
                if msgs and msgs[-1].get("role") == "assistant" and msgs[-1].get("streaming"):
                    portion = min(1.0, elapsed * 1000 / expected_ms)
                    msgs[-1]["text"] = _partial_reply(n.title, portion)
                    n.messages = msgs
                    changed_ids.add(n.nodeId)

                if elapsed * 1000 >= expected_ms:
                    if n.mode == "fail-demo" or random.random() < 0.05:
                        n.status = "failed"
                        n.failedReason = "执行超时（10 分钟）"
                    else:
                        n.status = "done"
                        n.output = _make_output(n)
                        # 发布产出到共享上下文（§4.2.2：节点产出进入会话上下文，版本+1）
                        await publish_output_to_context(db, n)
                    # 完成时补全最后一条 assistant 消息
                    if msgs and msgs[-1].get("role") == "assistant":
                        msgs[-1]["text"] = _final_reply(n.title)
                        msgs[-1]["streaming"] = False
                        n.messages = msgs
                    changed_ids.add(n.nodeId)

        # pending -> ready / blocked（仅当节点已有用户输入才就绪；空卡片等待输入，不自动执行）
        for n in nodes:
            if n.status == "pending":
                has_user_input = any(m.get("role") == "user" for m in (n.messages or []))
                if not has_user_input:
                    continue  # 空卡片：等待用户输入，不自动执行
                pids = n.parentIds or []
                sts = [by_id[p].status for p in pids if p in by_id]
                if sts and all(is_terminal(s) for s in sts):
                    n.status = "ready" if all(s == "done" for s in sts) else "blocked"
                    changed_ids.add(n.nodeId)

        # ---- 定时提醒（schedule）触发：到期注入新的用户消息，触发下一个执行轮 ----
        now_ts = time.time()
        for n in nodes:
            mem = n.memory or {}
            scheds = mem.get("schedules") or []
            if not scheds:
                continue
            pending = [s for s in scheds if (s.get("due_ts") or now_ts + 1) <= now_ts]
            if not pending:
                continue
            # 触发：把每个到期提醒的 prompt 作为新的 user 消息注入
            msgs = list(n.messages or [])
            newly_running = False
            for s in pending:
                pmt = str(s.get("prompt", "")).strip() or "定时任务触发"
                msgs.append({"id": _gen_step_id(), "role": "user", "text": f"[定时提醒] {pmt}", "at": int(now_ts * 1000)})
                # 周期任务重置下一轮；一次性任务移除
                if s.get("every") and s["every"] >= 300:
                    s["due_ts"] = now_ts + s["every"]
                    s["last_ts"] = now_ts
                else:
                    scheds.remove(s)
            n.messages = msgs
            n.memory = mem
            # 若非执行中，置 running 让它执行这一轮
            if n.status not in ("running", "paused"):
                n.status = "running"
                newly_running = True
            if newly_running and n.nodeId not in _real_exec_tasks and _model_configured(db):
                real_dispatch.append(n.nodeId)
            changed_ids.add(n.nodeId)

        if changed_ids:
            db.commit()
            # 广播每个已变更节点的状态 + progress + messages 增量
            for nid in changed_ids:
                n = by_id.get(nid)
                if not n:
                    continue
                await publish(
                    n.sessionId,
                    {
                        "type": "node_update",
                        "nodeId": nid,
                        "status": n.status,
                        "progress": n.progress,
                        "messages": n.messages,
                    },
                )
        # commit 之后再派发真实执行任务（保证 _run_real 能读到 running 状态）
        for nid in real_dispatch:
            if nid not in _real_exec_tasks:
                _real_exec_tasks[nid] = asyncio.create_task(_run_real(nid))
    finally:
        db.close()


async def scheduler_loop() -> None:
    """后台常驻调度循环。"""
    while True:
        try:
            await tick()
        except Exception:
            pass
        await asyncio.sleep(TICK_SECONDS)


_REPLY_BASE = (
    "明白了。我已根据你的输入和当前卡片上下文完成这次回复，并同步到共享上下文。\n\n"
)


def _final_reply(title: str) -> str:
    return f"{_REPLY_BASE}针对「{title}」的产出已就绪，如需结合本卡片内容可在此继续追问。"


def _partial_reply(title: str, portion: float) -> str:
    full = _final_reply(title)
    length = max(1, int(len(full) * min(1.0, portion)))
    return full[:length]
