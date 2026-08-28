"""真实执行器：OpenAI 兼容的流式 chat completions。

未配置模型时抛 ModelNotConfigured，由调度器回退到 mock。
"""
import httpx
from .model_config import ModelConfig


class ModelNotConfigured(Exception):
    pass


def load_config(db) -> tuple[str, str, str]:
    """返回 (base_url, api_key, model)；未配置抛 ModelNotConfigured。"""
    cfg = db.get(ModelConfig, "default") if hasattr(db, "get") else None
    if not cfg:
        cfg = getattr(db, "query", lambda *a, **k: None)(ModelConfig).first()
    if not cfg or not (cfg.apiKey and cfg.baseUrl and cfg.model):
        raise ModelNotConfigured()
    return cfg.baseUrl.rstrip("/"), cfg.apiKey, cfg.model


def build_messages(
    node_title: str,
    user_text: str,
    history: list[dict],
    parent_outputs: list[str],
    memory: dict | None = None,
    skills_text: str = "",
) -> list[dict]:
    """组装发给模型的消息：系统提示 + 卡片滚动记忆 + 父节点产出上下文 + 历史对话 + 当前输入。"""
    system = (
        "你是本卡片的专属执行 agent。遵循「先探测、后行动」的工作模式：\n"
        "【工作原则】\n"
        "1) 先用工具核实事实（列目录/读文件/检索），再下结论或动手；不确定时不要臆造，用工具确认。\n"
        "2) 需要信息时优先用只读工具（list_dir/read_file/search_files/grep_content/web_search/web_fetch/search_parent_memory/read_parent_output）；"
        "写文件、执行命令属于敏感操作，会先请求人工审批。\n"
        "3) 复杂任务先规划、分步执行；每步基于上一步结果推进，成功才继续；失败时分析原因、换策略，"
        "同一操作连续失败 2 次即停止并反馈，不要盲目重试。\n"
        "4) 每一步实施后自检：改动是否最小、是否验证过；能用工具验证就用工具验证。\n"
        "5) 上游任务只给摘要索引；需要细节时调用 read_parent_output（取完整内容）或 search_parent_memory（语义检索片段）。\n"
        "【联网搜索】\n"
        "6) web_search 会返回「标题 + 摘要 + URL」。摘要信息已足够回答时，直接基于摘要回答，不必再抓原文；"
        "只有摘要不足时才用 URL 调 web_fetch。抓取失败（动态渲染/反爬）就改用摘要或换来源回答，"
        "不要反复重抓同一页面。\n"
        "【输出格式】\n"
        "7) 最终回答用中文，结构化：先给结论，再给关键过程（含用到的工具/文件），最后给下一步建议（无则省略）。\n"
        "8) 不要编造工具结果或文件内容；把不确定处明确标为「待确认」。\n"
        "【安全】\n"
        "9) 不打印 API Key、密码等敏感信息；工具操作限制在工作目录内，路径越界会被拒绝。"
    )
    msgs: list[dict] = [{"role": "system", "content": system}]

    # 已加载的 skill 能力清单
    if skills_text.strip():
        msgs.append({"role": "system", "content": skills_text.strip()})

    # 卡片滚动记忆（历史对话/任务的压缩摘要，避免失忆）
    mem = memory or {}
    mem_parts = []
    if mem.get("summary"):
        mem_parts.append(f"本卡片历史摘要：{mem['summary']}")
    if mem.get("key_facts"):
        facts = "；".join(mem["key_facts"]) if isinstance(mem["key_facts"], list) else str(mem["key_facts"])
        mem_parts.append(f"关键结论：{facts}")
    if mem_parts:
        msgs.append({"role": "user", "content": "\n".join(mem_parts)})

    # 父节点产出上下文（摘要索引）
    ctx_parts = []
    if parent_outputs:
        ctx_parts.append("上游任务目录索引（按时间顺序）：")
        for i, p in enumerate(parent_outputs, 1):
            ctx_parts.append(f"[{i}] {p}")
        ctx_parts.append("（以上为摘要索引；如需某条全文调用 read_parent_output，如需按语义检索调用 search_parent_memory）")
    if node_title:
        ctx_parts.append(f"当前任务：{node_title}")
    if ctx_parts:
        msgs.append({"role": "user", "content": "\n".join(ctx_parts)})

    # 最近若干轮原始对话（保留连续性，但不无脑塞全部）
    for m in history[-12:]:
        role = m.get("role")
        text = m.get("text", "")
        if role in ("user", "assistant") and text:
            msgs.append({"role": role, "content": text[:2000]})

    msgs.append({"role": "user", "content": user_text})
    return msgs


async def stream_chat(base_url: str, api_key: str, model: str, messages: list[dict], temperature: float = 0.7):
    """流式调用 OpenAI 兼容 chat completions，逐段 yield 文本增量。"""
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                import json
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content")
                if content:
                    yield content


async def stream_chat_with_tools(
    base_url: str, api_key: str, model: str, messages: list[dict], tools: list[dict] | None,
    temperature: float = 0.2,
):
    """流式调用并同时产出两类事件：
    yield {"type":"delta","text":...}  每段文本增量
    yield {"type":"done","tool_calls":[...]}  本轮结束时若模型要调工具
    yield {"type":"done","tool_calls":[]}     本轮结束无工具
    """
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: dict = {"model": model, "messages": messages, "stream": True, "temperature": temperature}
    if tools:
        payload["tools"] = tools
    import json as _json
    tool_calls_acc: dict[int, dict] = {}
    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
        async with client.stream("POST", url, json=payload, headers=headers) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = _json.loads(data)
                except Exception:
                    continue
                delta = chunk.get("choices", [{}])[0].get("delta", {})
                content = delta.get("content")
                if content:
                    yield {"type": "delta", "text": content}
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    acc = tool_calls_acc.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if tc.get("id"):
                        acc["id"] = tc["id"]
                    if tc.get("function", {}).get("name"):
                        acc["function"]["name"] = tc["function"]["name"]
                    if tc.get("function", {}).get("arguments"):
                        acc["function"]["arguments"] += tc["function"]["arguments"]
    ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())]
    yield {"type": "done", "tool_calls": ordered}


async def stream_final(
    base_url: str, api_key: str, model: str, messages: list[dict]
):
    """最终回答的流式输出（无工具）。"""
    async for delta in stream_chat(base_url, api_key, model, messages):
        yield delta


async def _chat_once_text(base_url: str, api_key: str, model: str, messages: list[dict]) -> str:
    """一次非流式调用，返回 message.content（供摘要/压缩等内部用途）。"""
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "stream": False, "temperature": 0.2}
    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    return data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""


async def generate_plan(base_url: str, api_key: str, model: str, task: str, parent_index: list[str], memory: dict | None) -> dict:
    """LLM 规划：把任务拆分为可执行步骤（对齐主流 Planning agent）。"""
    import json as _json
    mem = memory or {}
    prompt = (
        "你是任务规划器。请把下面的任务拆解成 2~6 个具体可执行步骤（步骤要能对应到工具调用，"
        "如读文件/列目录/联网搜索/写文件等），严格返回 JSON，不要多余文字：\n"
        '{"goal": "一句话目标", "steps": ["步骤1", "步骤2", ...]}\n\n'
        f"任务：{task[:1500]}\n"
        f"已继承的上游索引：{'; '.join(parent_index)[:1000]}\n"
        f"历史记忆摘要：{mem.get('summary', '')[:500]}\n"
    )
    msgs = [
        {"role": "system", "content": "你是任务规划助手，产出简洁、可执行的计划 JSON。"},
        {"role": "user", "content": prompt},
    ]
    try:
        content = await _chat_once_text(base_url, api_key, model, msgs)
        content = (content or "").strip()
        import re as _re
        content = _re.sub(r"^```(?:json)?\s*", "", content)
        content = _re.sub(r"\s*```$", "", content)
        obj = _json.loads(content)
        steps = obj.get("steps") or []
        if not isinstance(steps, list):
            steps = [str(steps)]
        steps = [str(s).strip() for s in steps if str(s).strip()][:8]
        return {
            "goal": str(obj.get("goal") or task[:80]),
            "steps": [{"label": s, "status": "pending"} for s in steps],
        }
    except Exception:
        # 回退：单步计划
        return {"goal": task[:80], "steps": [{"label": "执行任务并给出结论", "status": "pending"}]}


async def summarize_text(base_url: str, api_key: str, model: str, text: str) -> str:
    """LLM 生成结构化摘要（对齐主流「LLM 压缩记忆」：单次低成本调用）。

    返回 JSON 字符串：{"summary": "...", "key_points": [...], "conclusion": "..."}
    失败时返回原文截断。
    """
    if not text.strip():
        return ""
    prompt = (
        "请把以下任务产出压缩成结构化摘要（用中文）。严格返回 JSON，不要多余文字：\n"
        '{"summary": "一句话摘要", "key_points": ["要点1", "要点2", "要点3"], "conclusion": "最终结论"}\n\n'
        f"产出内容：\n{text[:4000]}"
    )
    msgs = [
        {"role": "system", "content": "你是内容压缩助手，产出简洁、信息无损的结构化摘要。"},
        {"role": "user", "content": prompt},
    ]
    try:
        url = f"{base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {"model": model, "messages": msgs, "stream": False, "temperature": 0.2}
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        content = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        return content.strip()
    except Exception:
        return f'{{"summary": "{text[:200]}"}}'
