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
    project_context: str = "",
) -> list[dict]:
    """组装发给模型的消息：系统提示 + 项目上下文 + 卡片滚动记忆 + 父节点产出 + 历史 + 当前输入。"""
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
        "【自主推进】\n"
        "9) 你是高自主执行 agent：一次任务尽量自己拆解、连续调用工具推进到最后并完成，"
        "不要每步都停下来等确认；除非遇到需要人工决策/敏感操作/或实在无法继续，否则自主完成闭环。\n"
        "10) 任务结束时明确说「完成」并总结做了什么；不要输出「还需你确认下一步」这类半途而废的结束语。\n"
        "【自我验证】\n"
        "11) 写完文件、改完或执行完关键操作后，主动运行验证（读回文件/执行命令/编译/测试）确认结果无误再收尾；"
        "验证不通过就继续修正，不要直接下结论。\n"
        "【安全】\n"
        "12) 不打印 API Key、密码等敏感信息；工具操作限制在工作目录内，路径越界会被拒绝。"
    )
    msgs: list[dict] = [{"role": "system", "content": system}]

    # 已加载的 skill 能力清单
    if skills_text.strip():
        msgs.append({"role": "system", "content": skills_text.strip()})

    # 开工主动加载的项目背景（README/约定 + 结构）——让 agent 动手前先理解项目
    if project_context.strip():
        msgs.append(
            {
                "role": "system",
                "content": f"【项目背景，动手前列它们先浏览，按需用工具确认】\n{project_context.strip()[:3500]}",
            }
        )

    # 卡片滚动记忆（历史对话/任务的压缩摘要，避免失忆）
    mem = memory or {}
    mem_parts = []
    if mem.get("summary"):
        mem_parts.append(f"本卡片历史摘要：{mem['summary']}")
    if mem.get("key_facts"):
        facts = "；".join(mem["key_facts"]) if isinstance(mem["key_facts"], list) else str(mem["key_facts"])
        mem_parts.append(f"关键结论：{facts}")
    arch = mem.get("archive") or []
    if arch:
        # 分层历史摘要：仅取最近几层（更早的早已收敛），避免体积膨胀
        for a in arch[-3:]:
            mem_parts.append(f"[历史{str(a.get('layer', ''))}] {str(a.get('summary', ''))[:300]}")
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
    """一次非流式调用，返回 message.content（供摘要/压缩等内部用途）。

    内置失败重试：对限流(429)/5xx/网络错误做指数退避重试（最多 3 次），
    提高真实调用稳定性（对齐主流「限流自动重试」）。
    """
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": messages, "stream": False, "temperature": 0.2}
    import asyncio as _aio
    last: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=10.0)) as client:
                resp = await client.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
        except httpx.HTTPStatusError as he:
            # 只有限流/服务端错误才重试；4xx(如 401/400 参数错) 直接抛
            status = he.response.status_code if he.response is not None else 0
            if status not in (408, 429, 500, 502, 503, 504):
                raise
            last = he
        except httpx.TransportError as te:
            last = te
        if attempt < 2:
            await _aio.sleep(1.0 * (2 ** attempt))
    raise last if last else RuntimeError("LLM 调用失败")


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
        from .jsonutil import parse_json_object, expect_str, expect_list
        obj = parse_json_object(content or "")
        goal = expect_str(obj, "goal", "")
        steps = expect_list(obj, "steps", [])
        # 归一化：steps 支持 str 或 {"label":..} 两种形式
        norm: list[str] = []
        for s in steps:
            if isinstance(s, dict):
                lab = expect_str(s, "label", "")
                if str(lab).strip():
                    norm.append(str(lab).strip())
            else:
                sv = str(s).strip()
                if sv:
                    norm.append(sv)
        norm = norm[:8]
        return {
            "goal": goal or task[:80],
            "steps": [{"label": s, "status": "pending"} for s in norm],
        }
    except ValueError:
        # 模型未返回合法计划 JSON：回退单步计划（不静默成功，明确降级）
        return {"goal": task[:80], "steps": [{"label": "执行任务并给出结论", "status": "pending"}]}
    except Exception:
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


# ---- 上下文实时压缩（长对话防丢信息） ----

def _est_tokens(chars: int) -> int:
    """粗略 token 估算：中文≈0.6 token/字，ASCII≈1 token/4 字符。"""
    cjk = sum(1 for ch in str(chars) if "\u4e00" <= ch <= "\u9fff")
    rest = max(0, int(chars) - cjk)
    return int(cjk * 0.6 + rest / 4)


def _msg_chars(m: dict) -> int:
    return len(m.get("text", "") or "")


async def compact_history(
    base_url: str,
    api_key: str,
    model: str,
    history: list[dict],
    keep_recent: int = 6,
    max_old_chars: int = 6000,
) -> list[dict]:
    """长对话实时压缩：把「最近 keep_recent 条」之外的更早历史，用 LLM 折叠成一条摘要消息。

    目的：替代原来的「滚动截断」（只留最后 12 条、每条截 2000 字），做到
    信息有摘要、不因长度机械丢弃。模型不可用/失败时退化为简单拼接，不阻塞调用。
    """
    if not history:
        return history

    # 老历史 = 去掉最近 keep_recent 条后剩余
    old = history[:-keep_recent] if len(history) > keep_recent else []
    recent = history[-keep_recent:] if len(history) > keep_recent else history

    # 压缩目标是「超量且非空」的老历史；若老历史本身很短，直接透传（最近条已含全部）
    old_chars = sum(_msg_chars(m) for m in old)
    if not old or old_chars <= max_old_chars:
        return history

    # 逐条折叠：把老历史的 user/assistant 对话压成要点
    folded_lines: list[str] = []
    for m in old:
        role = m.get("role", "")
        text = (m.get("text", "") or "").strip()
        if not text:
            continue
        prefix = "用户" if role == "user" else ("助手" if role == "assistant" else role)
        folded_lines.append(f"{prefix}：{text[:600]}")

    if not folded_lines:
        return history

    prompt = (
        "下面是某任务卡片更早的一段多轮对话。请把它的关键信息压缩成一段不超过 200 字的中文摘要。"
        "保留：用户的需求、已确认的事实、已做的决定、关键结论。丢弃寒暄和无用往返。\n"
        "只输出摘要正文，不要其他文字：\n\n"
        + "\n".join(folded_lines[-40:])
    )
    msgs = [
        {"role": "system", "content": "你是上下文压缩器，产出信息无损的紧凑摘要。"},
        {"role": "user", "content": prompt},
    ]
    summary = ""
    try:
        content = await _chat_once_text(base_url, api_key, model, msgs)
        summary = (content or "").strip()
    except Exception:
        summary = ""

    # 生成压缩后的历史：摘要块 + 最近原文
    compacted: list[dict] = []
    if summary:
        compacted.append({"role": "system", "text": f"【更早对话摘要】{summary[:800]}"})
    else:
        # LLM 失败回退：把老历史合成一条（尽量小）
        fallback = "；".join(l for l in folded_lines)[-1600:]
        compacted.append({"role": "system", "text": f"【更早对话（截断）】{fallback}"})
    compacted.extend(recent)
    return compacted
