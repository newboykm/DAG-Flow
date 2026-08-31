"""节点事件日志：借鉴 dsh（DeepSeek Harness）的 durable session log 思想。

每个节点的一次执行（一个 turn）产生一个事件流，追加写入 JSONL 文件：

    turn_start -> step_start -> llm_delta* -> llm_done
                -> tool_call -> tool_result -> (reflection) -> step_start -> ... -> turn_end

- 每个事件带 seq + ts，落盘（追加 + flush），可回放、可审计。
- 中断 / 重试时，可从事件日志重建 LLM 上下文（rebuild_context），
  替代「只靠 Node.messages 数组」的上下文来源，避免长任务信息丢失。
- 事件日志是**增量增强层**：现有 Node.messages 语义与前端展示完全不变（前端兼容）。

事件 kind 约定：
    turn_start   {node_title, input}
    step_start   {round}
    llm_done     {text, reasoning, tool_calls, prompt_tokens, completion_tokens}
    tool_call    {tool, args, needs_approval}
    tool_result  {tool, ok, result_preview, elapsed_ms}
    reflection   {tool, reason}              # 失败反思注入
    turn_end     {status, final_text, usage}
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Iterable

# 事件日志根目录（相对 backend/ 包）：backend/.event_logs/<sessionId>/<nodeId>.jsonl
_EVENT_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".event_logs")

# 进程内锁（同一节点不会并发执行，这里只是防御）
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: str) -> threading.Lock:
    with _LOCKS_GUARD:
        lock = _LOCKS.get(path)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[path] = lock
        return lock


def _now_ts() -> float:
    return time.time()


def event_log_path(session_id: str, node_id: str) -> str:
    """事件日志文件路径（目录自动创建）。"""
    d = os.path.join(_EVENT_LOG_DIR, session_id)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"{node_id}.jsonl")


class EventLog:
    """单个节点的事件日志（追加写 + 回放读）。"""

    def __init__(self, session_id: str, node_id: str):
        self.session_id = session_id
        self.node_id = node_id
        self.path = event_log_path(session_id, node_id)
        self._seq = self._count_existing()
        self._run = self._count_runs()
        self._lock = _lock_for(self.path)

    # ---- 写入 ----

    def _count_runs(self) -> int:
        """统计既有 turn_start（run）数量，作为下一个 run 起始序号。"""
        n = 0
        for e in self.read_all():
            if e.get("kind") == "turn_start":
                n += 1
        return n

    def _count_existing(self) -> int:
        if not os.path.exists(self.path):
            return 0
        n = 0
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for _ in f:
                    n += 1
        except Exception:
            return 0
        return n

    def append(self, kind: str, **fields: Any) -> int:
        """追加一条事件，返回其 seq。落盘（append + flush）。

        事件带 run 字段：run 由 turn_start 开启（自增），同一次执行内共享同一 run。
        """
        with self._lock:
            self._seq += 1
            if kind == "turn_start":
                self._run += 1
            rec = {
                "seq": self._seq,
                "ts": _now_ts(),
                "run": self._run,
                "kind": kind,
                **fields,
            }
            try:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    f.flush()
            except Exception:
                # 日志写入失败不能拖垮主流程：降级为内存缓冲，仅告警
                self._memory_only = getattr(self, "_memory_only", [])
                self._memory_only.append(rec)
            return self._seq

    # ---- 读取 / 回放 ----

    def read_all(self) -> list[dict]:
        return self.read_from(0)

    def read_from(self, seq: int) -> list[dict]:
        """从 seq+1 开始读取全部事件（seq<=0 表示从头）。"""
        out: list[dict] = []
        mem = getattr(self, "_memory_only", None)
        if mem:
            out.extend(m for m in mem if m["seq"] > seq)
        if not os.path.exists(self.path):
            return out
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("seq", 0) > seq:
                        out.append(rec)
        except Exception:
            pass
        out.sort(key=lambda r: r.get("seq", 0))
        return out

    def clear(self) -> None:
        """清空事件日志（节点整体重置时调用；单次执行重跑走 append+run，不复用此方法）。"""
        with self._lock:
            try:
                if os.path.exists(self.path):
                    os.remove(self.path)
            except Exception:
                pass
            self._seq = 0
            self._run = 0
            if hasattr(self, "_memory_only"):
                del self._memory_only

    def latest_run_events(self) -> list[dict]:
        """仅返回最近一次 run（turn_start 之后全部）的事件；无任何 run 返回空。"""
        evs = self.read_all()
        last_start = None
        for i, e in enumerate(evs):
            if e.get("kind") == "turn_start":
                last_start = i
        if last_start is None:
            return []
        return evs[last_start:]

    def runs(self) -> list[tuple[int, int, str | None]]:
        """列出每次执行（run）：(run, start_seq, last_status)。"""
        evs = self.read_all()
        pages: list[tuple[int, int, str | None]] = []
        for e in evs:
            if e.get("kind") == "turn_start":
                pages.append((e.get("run", 0), e.get("seq", 0), None))
            elif e.get("kind") == "turn_end" and pages:
                r, s, _ = pages[-1]
                pages[-1] = (r, s, e.get("status"))
        return pages

    # ---- 便捷入口 ----

    def turn_start(self, node_title: str, input: str) -> int:
        return self.append("turn_start", node_title=node_title, input=input[:2000])

    def step_start(self, round: int) -> int:
        return self.append("step_start", round=round)

    def llm_done(self, text: str, reasoning: str, tool_calls: list[dict], prompt_tokens: int = 0, completion_tokens: int = 0) -> int:
        return self.append(
            "llm_done",
            text=text[:6000],
            reasoning=reasoning[:4000],
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def tool_call(self, tool: str, args: dict, needs_approval: bool = False) -> int:
        return self.append("tool_call", tool=tool, args=args, needs_approval=needs_approval)

    def tool_result(self, tool: str, ok: bool, result_preview: str, elapsed_ms: int) -> int:
        return self.append("tool_result", tool=tool, ok=ok, result_preview=result_preview[:2000], elapsed_ms=elapsed_ms)

    def reflection(self, tool: str, reason: str = "") -> int:
        return self.append("reflection", tool=tool, reason=reason[:500])

    def turn_end(self, status: str, final_text: str = "", usage: dict | None = None) -> int:
        return self.append("turn_end", status=status, final_text=final_text[:6000], usage=usage or {})


# ---- 从事件重建上下文（LLM messages）----

def rebuild_context(
    events: Iterable[dict],
    keep_recent_steps: int = 4,
    max_old_chars: int = 6000,
    compress: bool = True,
    base_url: str = "",
    api_key: str = "",
    model: str = "",
) -> list[dict]:
    """从事件日志重建 LLM messages（对齐现有 messages 结构，前端/模型语义一致）。

    - 最近 keep_recent_steps 个 step 保留原文（含工具调用/结果/反思）。
    - 更早的 step 折叠成一条 system 摘要（LLM 压缩；模型不可用时回退为截断拼接），
      避免长任务因滚动截断丢信息（dsh compaction 的单层版）。
    - 返回 [{role, content/text, tool_calls, tool_call_id, reasoning_content}]，
      可直接喂给 build_messages / stream_chat_with_tools。

    注意：本函数不抛异常；任何失败都回退到「尽量保留原文」。
    """
    evs = [e for e in events if isinstance(e, dict)]
    if not evs:
        return []

    # 1) 按 step 切分：每个 step = llm_done(+tool_call/tool_result/reflection 组)
    steps: list[list[dict]] = []
    cur: list[dict] = []
    for e in evs:
        if e.get("kind") == "step_start" and cur:
            steps.append(cur)
            cur = [e]
        else:
            cur.append(e)
    if cur:
        steps.append(cur)

    old_steps = steps[:-keep_recent_steps] if len(steps) > keep_recent_steps else []
    recent_steps = steps[-keep_recent_steps:] if len(steps) > keep_recent_steps else steps

    def _event_text(step: list[dict]) -> list[str]:
        lines: list[str] = []
        for e in step:
            k = e.get("kind")
            if k == "llm_done":
                t = (e.get("text") or "").strip()
                if t:
                    lines.append(f"助手：{t}")
                tcs = e.get("tool_calls") or []
                if tcs:
                    for tc in tcs:
                        fn = (tc.get("function") or {})
                        lines.append(f"调用工具 {fn.get('name','')}({(fn.get('arguments') or '')[:300]})")
            elif k == "tool_result":
                lines.append(f"工具 {e.get('tool','')} 返回：{(e.get('result_preview') or '')[:400]}")
            elif k == "reflection":
                lines.append(f"反思：{(e.get('reason') or '')[:300]}")
        return lines

    messages: list[dict] = []

    # 2) 老步骤折叠成摘要（LLM 压缩，dsh compaction 风格）
    if old_steps:
        old_lines: list[str] = []
        for s in old_steps:
            old_lines.extend(_event_text(s))
        old_text = "\n".join(old_lines)[:max_old_chars]
        if compress and base_url and api_key and model and old_text.strip():
            summary = _llm_summarize(base_url, api_key, model, old_text)
            if summary:
                messages.append({"role": "system", "text": f"【更早执行摘要】{summary[:1200]}"})
            else:
                messages.append({"role": "system", "text": f"【更早执行（截断）】{old_text[-1600:]}"})
        else:
            messages.append({"role": "system", "text": f"【更早执行（截断）】{old_text[-1600:]}"})

    # 3) 最近步骤原文重建
    for step in recent_steps:
        for e in step:
            k = e.get("kind")
            if k == "turn_start":
                messages.append({"role": "user", "text": (e.get("input") or "")})
            elif k == "llm_done":
                tcs = e.get("tool_calls") or []
                if tcs:
                    m = {"role": "assistant", "content": None, "tool_calls": tcs}
                    rc = e.get("reasoning")
                    if rc:
                        m["reasoning_content"] = rc
                    messages.append(m)
                else:
                    t = (e.get("text") or "").strip()
                    m: dict = {"role": "assistant", "text": t} if t else {"role": "assistant", "text": ""}
                    rc = e.get("reasoning")
                    if rc:
                        m["reasoning_content"] = rc
                    messages.append(m)
            elif k == "tool_result":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": _tool_call_id_of(step, e.get("tool", "")),
                        "content": e.get("result_preview") or "（无输出）",
                    }
                )
            elif k == "reflection":
                messages.append({"role": "user", "content": f"工具 {e.get('tool','')} 的执行结果不理想：{e.get('reason','')}。请反思失败原因，换一种方法重试（不要重复完全相同调用）。"})
    return messages


def _tool_call_id_of(step: list[dict], tool_name: str) -> str:
    """在 step 内找 tool_name 对应的 tool_call id（用于重建 tool 消息）。"""
    for e in step:
        if e.get("kind") == "llm_done":
            for tc in e.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name") == tool_name and tc.get("id"):
                    return tc["id"]
    return f"call-{abs(hash((tool_name, len(step)))) % 100000}"


def _llm_summarize(base_url: str, api_key: str, model: str, text: str) -> str:
    """LLM 压缩旧事件为摘要（单次低成本调用；失败返回空串）。"""
    import asyncio

    async def _run() -> str:
        import httpx
        prompt = (
            "下面是某任务卡片更早的多轮执行记录。请把关键信息压缩成一段不超过 300 字的中文摘要。"
            "保留：目标、已确认的事实、关键结论、已完成的操作。丢弃细节与寒暄。\n"
            "只输出摘要正文，不要其他文字：\n\n" + text[:4000]
        )
        msgs = [
            {"role": "system", "content": "你是上下文压缩器，产出信息无损的紧凑摘要。"},
            {"role": "user", "content": prompt},
        ]
        payload = {"model": model, "messages": msgs, "stream": False, "temperature": 0.2}
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
            return (data.get("choices", [{}])[0].get("message", {}).get("content", "") or "").strip()
        except Exception:
            return ""

    try:
        # 若已在事件循环内（scheduler 等 async 上下文），放进线程池执行，
        # 避免 asyncio.run() 在 running loop 中抛 RuntimeError。
        try:
            asyncio.get_running_loop()
            in_loop = True
        except RuntimeError:
            in_loop = False
        if in_loop:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
                fut = ex.submit(lambda: asyncio.run(_run()))
                return fut.result(timeout=60) or ""
        return asyncio.run(_run())
    except Exception:
        return ""


# ---- 模块级便捷函数 ----

def append_event(session_id: str, node_id: str, kind: str, **fields: Any) -> int:
    """模块级便捷写入：不关心 EventLog 实例时使用。"""
    return EventLog(session_id, node_id).append(kind, **fields)


def read_events(session_id: str, node_id: str) -> list[dict]:
    return EventLog(session_id, node_id).read_all()


def clear_events(session_id: str, node_id: str) -> None:
    EventLog(session_id, node_id).clear()


def last_status(events: list[dict]) -> str | None:
    """取事件流最后一次 turn_end 的 status（'done'/'failed'/'cancelled'/...）。"""
    for e in reversed(events):
        if e.get("kind") == "turn_end":
            return e.get("status")
    return None
