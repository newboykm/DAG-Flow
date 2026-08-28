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
from .dag import compute_initial_status, is_terminal
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


def _now_dt():
    from datetime import datetime
    return datetime.now()


def _gen_step_id() -> str:
    import uuid
    return "step-" + uuid.uuid4().hex[:10]


def _summary_from_json(s: str) -> str:
    """从 LLM 返回的结构化摘要 JSON 里提取纯文本摘要。"""
    import json as _json
    import re as _re
    if not s or not s.strip():
        return ""
    s = s.strip()
    # 去掉可能的代码块围栏
    s = _re.sub(r"^```(?:json)?\s*", "", s)
    s = _re.sub(r"\s*```$", "", s)
    try:
        obj = _json.loads(s)
        if isinstance(obj, dict):
            summary = obj.get("summary")
            if not summary:
                # 用 key_points + conclusion 拼出摘要
                parts = []
                if obj.get("key_points"):
                    parts.append("；".join(obj["key_points"]) if isinstance(obj["key_points"], list) else str(obj["key_points"]))
                if obj.get("conclusion"):
                    parts.append(f"结论：{obj['conclusion']}")
                summary = "。".join(parts)
            return (summary or "").strip()
    except Exception:
        pass
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
        content = await _chat_once_text(provider.baseUrl.rstrip("/"), provider.apiKey, model, msgs)
        content = (content or "").strip()
        if content.startswith("```"):
            import re as _re
            content = _re.sub(r"^```(?:json)?\s*", "", content)
            content = _re.sub(r"\s*```$", "", content)
        obj = _json.loads(content)
        summary = obj.get("summary") or old_summary or final_text[:150]
        key_facts = obj.get("key_facts") or []
        if not isinstance(key_facts, list):
            key_facts = [str(key_facts)]
        return {"summary": summary, "key_facts": key_facts[:5], "conclusion": obj.get("conclusion") or ""}
    except Exception:
        # 回退：合并旧摘要与本轮产出简单拼接
        return {
            "summary": f"{old_summary}; {final_text[:120]}".strip("; "),
            "key_facts": old_facts,
            "conclusion": final_text[:200],
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
                provider.apiKey,
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

        # 加载 skill 能力清单（内置默认 + 用户自定义目录）
        from .models import AppConfig
        from .skills import load_all_skills_prompt
        skill_row = db.query(AppConfig).filter(AppConfig.key == "skill_dir").first()
        skills_text = load_all_skills_prompt(skill_row.value if skill_row else None)

        messages = build_messages(node.title, user_text, history, parent_outputs, node.memory, skills_text)
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

        # 生成执行计划并推送前端（对齐主流 Planning agent）
        node.plan = await generate_plan(
            cfg_base, provider.apiKey, provider_model,
            user_text, parent_outputs, node.memory,
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

        # 确保存在一条 assistant 占位
        msgs = list(node.messages or [])
        if not msgs or msgs[-1].get("role") != "assistant":
            msgs.append({"id": f"m-{node_id}-a", "role": "assistant", "text": "", "streaming": True})
            node.messages = msgs
            db.commit()

        async def flush(text: str, streaming: bool):
            msgs = list(node.messages or [])
            for i in range(len(msgs) - 1, -1, -1):
                if msgs[i].get("role") == "assistant":
                    msgs[i] = {**msgs[i], "text": text, "streaming": streaming}
                    break
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
            plan = node.plan or {}
            steps = list(plan.get("steps") or [])
            if 0 <= idx < len(steps):
                steps[idx] = {**steps[idx], "status": status}
                plan["steps"] = steps
                node.plan = plan
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
        max_rounds = 6
        fail_counts: dict[str, int] = {}

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
            """工具失败后注入反思提示，让模型换策略重试（不超过 2 次）。"""
            fail_counts[tool_name] = fail_counts.get(tool_name, 0) + 1
            if fail_counts[tool_name] <= 2:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"工具 {tool_name} 的执行结果：{result[:500]}\n\n"
                            "请反思失败原因，换一种方法重试（不要重复完全相同调用）。"
                        ),
                    }
                )
                await flush_step("反思重试", tool_name)
        for _round in range(max_rounds):
            if cancelled():
                break
            # 标记当前计划步骤 running（若存在计划）
            cur_step = plan_index()
            if cur_step >= 0:
                await set_plan_step(cur_step, "running")

            round_text = ""
            tool_calls: list[dict] = []
            async for ev in stream_chat_with_tools(cfg_base, provider.apiKey, provider_model, messages, tools, temperature=0.2):
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

            if cancelled():
                break
            # 本轮完成（产生工具或文本）→ 当前计划步骤 done
            if cur_step >= 0:
                await set_plan_step(cur_step, "done")

            if tool_calls:
                # 把本轮 assistant 工具调用写入 messages，再逐个执行
                await flush_step("调用工具", f"{len(tool_calls)} 个工具")
                assistant_tool_msg = {"role": "assistant", "content": None, "tool_calls": tool_calls}
                messages.append(assistant_tool_msg)
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    raw_args = fn.get("arguments") or "{}"
                    import json as _json
                    try:
                        parsed = _json.loads(raw_args)
                    except Exception:
                        parsed = {}
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
                    if tool_def and needs_approval(name, parsed):
                        await flush_step("待审批", f"{name} {raw_args[:80]}")
                        ap = await _request_approval(db, node, name, parsed)
                        decision = await _wait_approval(db, ap.id)
                        if decision == "rejected":
                            result = f"用户拒绝了工具 {name} 的调用"
                            await flush_step("已拒绝", name)
                        else:
                            await flush_step("执行工具", name)
                            try:
                                result = await execute(name, parsed, workspace, tool_ctx)
                            except Exception as e:  # noqa: BLE001
                                result = f"工具执行出错：{e}"
                            await flush_step("工具结果", (result or "")[:120])
                    else:
                        await flush_step("执行工具", name)
                        try:
                            result = await execute(name, parsed, workspace, tool_ctx)
                        except Exception as e:  # noqa: BLE001
                            result = f"工具执行出错：{e}"
                        await flush_step("工具结果", (result or "")[:120])
                    messages.append({"role": "tool", "tool_call_id": tc.get("id", ""), "content": result})
                    if is_failure(result):
                        await push_reflection(name, result)
                continue  # 继续下一轮
            else:
                # 本轮没有工具调用：保留已有 acc（可能是空，说明模型直接给文本或结束）
                # 若 acc 为空且 round_text 也为空，说明模型没给内容也没工具，直接结束
                if not acc:
                    await flush_step("完成", "")
                break

        if cancelled() or node_id in _cancel_requested or (db.get(Node, node_id) or node).status == "cancelled":
            _cancel_requested.discard(node_id)
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

        # 计划收尾：把剩余步骤全部标记完成
        final_plan = node.plan or {}
        steps = list(final_plan.get("steps") or [])
        for i in range(len(steps)):
            if steps[i].get("status") != "done":
                steps[i] = {**steps[i], "status": "done"}
        final_plan["steps"] = steps
        node.plan = final_plan

        # 更新卡片滚动记忆（每次完成用 LLM 折叠，避免失忆 + 控制 token）
        try:
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

        node.status = "done"
        node.output = {
            "type": "text",
            "summary": final_text[:200] or f"已完成「{node.title}」",
            "content": final_text,
            "artifacts": [],
        }
        await publish_output_to_context(db, node)
        db.commit()
        await publish(
            node.sessionId,
            {
                "type": "node_update",
                "nodeId": node.nodeId,
                "status": "done",
                "progress": node.progress,
                "messages": node.messages,
            },
        )
    except Exception as e:  # noqa: BLE001
        # 执行失败：置 failed
        node = db.get(Node, node_id)
        if node and node.status == "running":
            node.status = "failed"
            node.failedReason = f"执行失败：{e}"
            db.commit()
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
        "你是主 agent 的聚焦子代理，负责一个明确的子任务。遵循「先探测、后行动」，"
        "分步执行、失败换策略，最终用中文返回一个精炼结论（含关键过程与文件/工具）。"
        "不要调用 run_subagent（禁止嵌套）。"
    )
    messages = [
        {"role": "system", "content": sys},
        {"role": "user", "content": f"子任务：{task}\n工作目录：{workspace}"},
    ]
    tools = openai_tools()
    acc = ""
    for _ in range(max(1, min(max_rounds, 6))):
        tool_calls: list[dict] = []
        async for ev in stream_chat_with_tools(provider.baseUrl.rstrip("/"), provider.apiKey, model, messages, tools, temperature=0.2):
            if ev["type"] == "delta":
                acc += ev["text"]
            elif ev["type"] == "done":
                tool_calls = ev["tool_calls"]
        if not tool_calls:
            break
        messages.append({"role": "assistant", "content": None, "tool_calls": tool_calls})
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
