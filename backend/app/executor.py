"""真实执行器：OpenAI 兼容的流式 chat completions。

未配置模型时抛 ModelNotConfigured，由调度器回退到 mock。
"""
import httpx
from .model_config import ModelConfig


class ModelNotConfigured(Exception):
    pass


class _RetryableHTTP(Exception):
    """限流(429)/5xx 等可重试的 HTTP 状态（流式请求建立阶段）。"""

    def __init__(self, status: int):
        super().__init__(f"retryable llm http status {status}")
        self.status = status


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
    workspace: str = "",
) -> list[dict]:
    """组装发给模型的消息：分层 system 提示（身份/角色/工具指引/技能/项目上下文）+ 记忆 + 父产出 + 历史 + 输入。

    分层遵循 dsh（DeepSeek Harness）的 system-prompt section 思想——职责分离、明确可读：
      - 身份（你是谁）
      - 角色 persona（怎么工作：先探测后行动/自主推进/安全）
      - 工具指引（用什么工具、什么时候用、关键用法）
      - 技能清单 + 项目上下文 + 记忆 + 上游产出 + 历史
    """
    # ---- 身份层 ----
    identity = "你是本卡片的专属执行 agent（一个自主的多工具 AI 智能体）。"

    # ---- 角色 persona 层（决策驱动工作原则）----
    persona = (
        "【决策三问：收到任务后先判断】\n"
        "① 用户意图明确吗？不明确 → 只问 1 个关键问题（附 2-3 个选项 + 默认建议），不要连环抛问。\n"
        "② 信息能自己查吗？（read/grep/search/web）能 → 直接并行探索，不提问。\n"
        "③ 操作可逆吗？不可逆（生产/push/扣款/删数据）→ 必须确认；可逆 → 直接执行，事后简述。\n"
        "\n【把握度分级（决定提问还是自行决定）】\n"
        "- 变量命名/代码风格/内部实现：≥60% 自行决定，不问。\n"
        "- 修改逻辑/重/新增文件：≥80% 才做；涉及对外接口/数据结构/数据存储等影响用户的设计 → 提问并给默认方案。\n"
        "- 生产、不可逆操作：≥95% 且必须用户明确确认。\n"
        "- 读/查/搜：零风险，大胆并行做，用发现指导决策。\n"
        "\n【规划期 vs 执行期】\n"
        "- 规划期（还没动代码）：意图、方案、对外接口必须清楚 → 不清楚就提问收敛。\n"
        "- 执行期（已在改代码）：内部细节自己用工具查，只在关键决策点（选型/对外接口）暂停提问。\n"
        "\n【并行探索】：一次把相关文件 grep 全，再并行 read（工具可并行，不要串行一个等一个）。\n"
        "\n【快速失败（防死循环）】\n"
        "- 同一 (工具, 参数) 失败后禁止重试，强制换一种思路。\n"
        "- 换 2 种思路仍失败 → 主动上报，给出 2 个备选方案请用户指示。\n"
        "- 单任务最多 12 步；超出则输出当前最佳结果 + 未完成清单。\n"
        "\n【执行后透明】：每次写入/修改后，都简述做了什么、为什么。\n"
        "\n【自主推进】你是高自主执行 agent：一次任务尽量自己拆解、连续用工具推进到最后并完成，"
        "除非遇到真需要用户决策/不可逆敏感操作/或卡死无法继续，否则自主闭环；结束时明确说「完成」并总结。\n"
        "\n【输出】最终回答用中文，先给结论，再给关键过程与用到的文件/工具，最后给下一步建议（无则省略）；"
        "不编造工具结果，不确定处标「待确认」；关键操作后自检验证（读回/编译/测试）再收尾。\n"
        "\n【安全】不打印密钥等敏感信息，操作限工作目录内；写文件/执行命令/运行代码等敏感操作会走审批，别硬闯。\n"
        "\n【长对话】轮次较多时定期总结已确认要点，避免重复提问；发现矛盾主动指出；信息够了就主动推进不再等确认。"
    )

    # ---- 工具指引层（dsh 每个工具自带 guidance 的思想）----
    tool_guidance = (
        "【工具使用指引】\n"
        "· 阅读文件一律用 read（带行号、可 offset/limit 分页），不要用 cat。\n"
        "· 修改已存在文件的局部内容用 edit（精确替换 old_string；默认要求唯一，多次匹配需更精确或 replace_all=true）。"
        "edit 前必须先 read 过该文件，否则会被拒绝。\n"
        "· 联网搜索用 web_search：一次可传 queries=[多个不同角度关键词]（1-4 个），系统会并行搜索、合并去重，"
        "DeepSeek 会返回高质量综合答案 + 权威来源。对重要/易错/需多方求证的信息，务必提供多个不同关键词以获得交叉验证；"
        "答案中要引用来源 URL。需要抓取某页正文时用 web_fetch（自动转为 Markdown）。\n"
        "· 上游任务只给摘要索引；需要细节时调用 read_parent_output（取完整内容）或 search_parent_memory（语义检索片段）。\n"
        "· 子任务可派生子代理完成（run_subagent），复杂任务建议拆成多个子任务。"
    )

    # ---- 组装分层 system 消息 ----
    msgs: list[dict] = [
        {"role": "system", "content": identity},
        {"role": "system", "content": persona},
        {"role": "system", "content": tool_guidance},
    ]

    # 项目经验记忆注入（技能/工具用法/入口/注意）：让 agent 一上来就知道咋做事，避免重复遍历
    if workspace:
        try:
            from .experiential_mem import load_project_knowledge
            _pk = load_project_knowledge(workspace)
            if _pk.strip():
                msgs.append({"role": "system", "content": _pk[:3000]})
        except Exception:
            pass

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
    # 长期目标（goal）：让 agent 在多轮/多步执行中始终记得主攻方向（对齐 dsh goal）
    if mem.get("goal"):
        goal_round = mem.get("goal_round") or 0
        mem_parts.append(
            f"【持续目标】{str(mem['goal'])[:400]}\n"
            f"（第 {goal_round} 轮推进中。目标尚未完成前，本轮继续朝它推进；"
            f"若本轮已彻底完成该目标，请在最终回复末尾单独一行写 [GOAL_DONE]。）"
        )
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
            msg = {"role": role, "content": text[:2000]}
            # deepseek thinking 模式：assistant 的历史 reasoning_content 需原样回传
            rc = m.get("reasoning_content")
            if role == "assistant" and rc:
                msg["reasoning_content"] = rc[:4000]
            msgs.append(msg)

    msgs.append({"role": "user", "content": user_text})
    return msgs


async def stream_chat(base_url: str, api_key: str, model: str, messages: list[dict], temperature: float = 0.7):
    """流式调用 OpenAI 兼容 chat completions，逐段 yield 文本增量。

    对齐 dsh-llm-retry：请求建立阶段(限流/5xx/连接错误)退避重试；开始产出后不重试。
    """
    import asyncio as _aio
    import random as _rnd
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

    async def _one_attempt(started: bool):
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    if (not started) and resp.status_code in (408, 429, 500, 502, 503, 504):
                        raise _RetryableHTTP(resp.status_code)
                    resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = __import__("json").loads(data)
                    except Exception:
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content

    attempts = 0
    started = False
    while True:
        try:
            async for ev in _one_attempt(started):
                started = True
                yield ev
            break
        except _RetryableHTTP:
            attempts += 1
            if attempts > 3:
                raise
            delay = min(15.0, 1.0 * (2 ** (attempts - 1)) * (0.5 + _rnd.random()))
            await _aio.sleep(delay)
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TransportError) as _te:
            attempts += 1
            if attempts > 3 or started:
                raise
            delay = min(15.0, 1.0 * (2 ** (attempts - 1)) * (0.5 + _rnd.random()))
            await _aio.sleep(delay)


async def stream_chat_with_tools(
    base_url: str, api_key: str, model: str, messages: list[dict], tools: list[dict] | None,
    temperature: float = 0.2,
):
    """流式调用并同时产出两类事件：
    yield {"type":"delta","text":...}  每段文本增量
    yield {"type":"done","tool_calls":[...]}  本轮结束时若模型要调工具
    yield {"type":"done","tool_calls":[]}     本轮结束无工具

    对齐 dsh-llm-retry：请求建立阶段(限流 429/5xx/连接错误)用指数退避+抖动自动重试。
    一旦已开始产出正文，则不再重试（避免重复 token）。
    """
    import asyncio as _aio
    import random as _rnd
    url = f"{base_url}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload: dict = {"model": model, "messages": messages, "stream": True, "temperature": temperature}
    if tools:
        payload["tools"] = tools
        # deepseek 等兼容端点有时需要显式 tool_choice，避免 400
        payload["tool_choice"] = "auto"
    import json as _json
    tool_calls_acc: dict[int, dict] = {}
    reasoning_acc = ""

    async def _one_attempt(started: bool):
        """发起一次请求并 yield；HTTP 429/5xx 可重试时抛 _Retryable，否则原样传播。"""
        nonlocal tool_calls_acc, reasoning_acc
        if not tools:
            # 无工具时也保持同一入口（由调用方决定是否传 tools）
            pass
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            async with client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    # 可重试：限流/5xx 且尚未产出内容
                    if (not started) and resp.status_code in (408, 429, 500, 502, 503, 504):
                        raise _RetryableHTTP(resp.status_code)
                    body = await resp.aread()
                    # 打印出供应商返回的具体错误，便于定位 400 原因
                    print(
                        "[LLM", resp.status_code, "]",
                        body.decode("utf-8", errors="replace")[:1500],
                        flush=True,
                    )
                    # 打印最后 5 条消息的结构，便于定位不足的 tool 消息
                    try:
                        for _m in messages[-5:]:
                            _role = _m.get("role")
                            _info = {
                                "role": _role,
                                "has_tool_calls": bool(_m.get("tool_calls")),
                                "tool_call_ids": [t.get("id") for t in (_m.get("tool_calls") or [])] if _m.get("tool_calls") else None,
                                "tool_call_id": _m.get("tool_call_id"),
                                "has_reasoning": "reasoning_content" in _m,
                            }
                            print("[LLM msg]", _json.dumps(_info, ensure_ascii=False), flush=True)
                    except Exception:
                        pass
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
                    if delta.get("reasoning_content"):
                        reasoning_acc += delta["reasoning_content"]
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

    # 重试循环：可重试错误退避，最多 3 次；一旦 yield 过真实内容即标记 started 不在重试
    tool_calls_acc = {}
    reasoning_acc = ""
    attempts = 0
    started = False
    while True:
        try:
            async for ev in _one_attempt(started):
                if ev["type"] == "delta":
                    started = True
                yield ev
            break
        except _RetryableHTTP:
            attempts += 1
            if attempts > 3:
                raise
            delay = min(15.0, 1.0 * (2 ** (attempts - 1)) * (0.5 + _rnd.random()))
            await _aio.sleep(delay)
        except (httpx.ConnectTimeout, httpx.ReadTimeout, httpx.TransportError) as _te:
            attempts += 1
            if attempts > 3 or started:
                raise
            delay = min(15.0, 1.0 * (2 ** (attempts - 1)) * (0.5 + _rnd.random()))
            await _aio.sleep(delay)

    ordered = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())]
    yield {"type": "done", "tool_calls": ordered, "reasoning_content": reasoning_acc}


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


async def generate_plan(base_url: str, api_key: str, model: str, task: str, parent_index: list[str], memory: dict | None,
                        on_steps=None) -> dict:
    """LLM 规划：把任务拆分为可执行步骤（对齐主流 Planning agent）。

    流式采集：等整段返回才生成会让"任务单审批"看起来晚到。这里改为流式攒内容，
    一旦能解析出足够步骤就通过 on_steps(norm) 提前通知（供调度器尽早弹计划审批），
    最终仍保证返回完整 plan。on_steps=None（调用方不关心）时行为基本等同原非流式版本。
    """
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
    from .jsonutil import parse_json_object, expect_str, expect_list
    import asyncio as _aio

    def _normalize(steps):
        norm: list[str] = []
        for s in steps or []:
            if isinstance(s, dict):
                lab = str(expect_str(s, "label", "") or "").strip()
                if lab:
                    norm.append(lab)
            else:
                sv = str(s).strip()
                if sv:
                    norm.append(sv)
        return norm[:8]

    _announced = False
    content = ""
    try:
        async for piece in stream_chat(base_url, api_key, model, msgs, temperature=0.4):
            if not piece:
                continue
            content += piece
            # 增量早报到：一旦攒出可解析且有足够步骤的计划，就提前通知（只一次），审批无需等整段收尾
            if on_steps is not None and not _announced:
                try:
                    obj = parse_json_object(content)
                    norm = _normalize(obj.get("steps"))
                    if len(norm) >= 2 and len(content) > 40:
                        _announced = True
                        await on_steps(norm)
                except Exception:
                    pass
        obj = parse_json_object(content or "")
        goal = expect_str(obj, "goal", "")
        steps = _normalize(obj.get("steps"))
        return {
            "goal": goal or task[:80],
            "steps": [{"label": s, "status": "pending"} for s in steps],
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
    budget_tokens: int = 0,
) -> list[dict]:
    """长对话实时压缩（对齐 dsh compaction-basic 的 token 压力驱动思想）。

    改进点（相对旧版）：
    - token 压力驱动：当整体(系统提示+历史)估算 token 超过 budget_tokens 时触发，
      旧版是固定字符阈值，新版按模型上下文预算自适应。
    - 保留最近 tail（keep_recent 条原文），更早历史用 LLM 压成摘要。
    - 摘要带 <compacted-summary> 标记（dsh 语义），下轮明确这是压缩过的历史。
    - 收敛校验：摘要若未比源 token 更小，则降级为更大力度压缩/截断，不回退到未压缩。
    模型不可用/失败时退化为截断拼接，不阻塞调用。
    """
    if not history:
        return history

    # ---- token 压力判断：整体估算超过预算才压缩 ----
    total_tokens = sum(_est_tokens(_msg_chars(m)) for m in history)
    if budget_tokens > 0 and total_tokens <= budget_tokens:
        return history  # 未超压，直接透传（对齐 dsh 压力未超标不压缩）

    # 老历史 = 去掉最近 keep_recent 条后剩余；最近 tail 保留原文
    old = history[:-keep_recent] if len(history) > keep_recent else []
    recent = history[-keep_recent:] if len(history) > keep_recent else history

    old_chars = sum(_msg_chars(m) for m in old)
    # 保守字符下限（与旧版一致）：老历史确实超量才压
    if not old or old_chars <= max_old_chars:
        return history

    def _fold(folded: list[str]) -> list[str]:
        """把老历史逐条折叠成要点行。"""
        lines: list[str] = []
        for m in old:
            role = m.get("role", "")
            text = (m.get("text", "") or "").strip()
            if not text:
                continue
            prefix = "用户" if role == "user" else ("助手" if role == "assistant" else role)
            lines.append(f"{prefix}：{text[:600]}")
        return lines

    async def _try_summarize(folded: list[str], max_summary_len: int) -> str:
        prompt = (
            f"下面是某任务卡片更早的一段多轮对话。请把它的关键信息压缩成一段不超过 {max_summary_len} 字的中文摘要。"
            "保留：用户的需求、已确认的事实、已做的决定、关键结论。丢弃寒暄和无用往返。\n"
            "只输出摘要正文，不要其他文字：\n\n"
            + "\n".join(folded[-40:])
        )
        msgs = [
            {"role": "system", "content": "你是上下文压缩器，产出信息无损的紧凑摘要。"},
            {"role": "user", "content": prompt},
        ]
        try:
            content = await _chat_once_text(base_url, api_key, model, msgs)
            return (content or "").strip()
        except Exception:
            return ""

    folded = _fold([])
    if not folded:
        return history

    # 首次摘要尝试
    summary = await _try_summarize(folded, 200)
    # 收敛校验：若摘要没有明显更小（token 相仿或更大），再压一次更狠的限制
    if summary:
        src_tok = sum(_est_tokens(l) for l in folded)
        sum_tok = _est_tokens(summary)
        if sum_tok > src_tok * 0.6:
            summary = await _try_summarize(folded, 100)

    # 生成压缩后的历史：<compacted-summary> 标记 + 最近原文（对齐 dsh 的 checkpoint framing）
    compacted: list[dict] = []
    if summary:
        compacted.append({"role": "system", "text": f"<compacted-summary>\n{summary[:1000]}\n</compacted-summary>"})
    else:
        # LLM 失败回退：把老历史合成一条（尽量小，保留尾部信息）
        fallback = "；".join(folded)[-1600:]
        compacted.append({"role": "system", "text": "<compacted-summary>\n" + fallback + "\n</compacted-summary>"})
    compacted.extend(recent)
    return compacted
