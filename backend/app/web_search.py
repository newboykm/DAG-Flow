"""DeepSeek native 联网搜索（对齐 dsh `web-search-deepseek` provider）。

调用 DeepSeek 的 Anthropic-compatible Messages API（POST {base}/anthropic/v1/messages），
带上 native `web_search_20250305` server tool。DeepSeek 服务端执行真实联网搜索，
返回结构化 `web_search_tool_result`（标题/URL/引用）+ 由 DeepSeek 生成的综合答案 `text`。

这是"高质量联网搜索"的核心：一次性拿到综合答案 + 权威来源，
而不是像免费引擎那样丢一堆杂乱结果让 agent 自己拼。
"""
from __future__ import annotations

import os
from typing import Any

DEEPSEEK_SEARCH_BASE = "https://api.deepseek.com/anthropic/v1"
# 默认用 deepseek-reasoner：思考增强，答案更严谨；可换 deepseek-chat 更快。
SEARCH_MODEL = "deepseek-reasoner"
MAX_TOKENS = 4096
MAX_USES = 5


def _credential() -> str | None:
    """DeepSeek API key：优先环境变量，其次 .env。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        return key.strip()
    # 向上查找含 DEEPSEEK_API_KEY 的 .env（与 start-dsh.sh 一致）
    cur = os.getcwd()
    for _ in range(6):
        envf = os.path.join(cur, ".env")
        if os.path.exists(envf):
            for line in open(envf, encoding="utf-8"):
                line = line.strip()
                if line.startswith("DEEPSEEK_API_KEY="):
                    v = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if v:
                        return v
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    return None


def native_search(queries: list[str], api_key: str | None = None, model: str = SEARCH_MODEL, max_results: int = 10) -> dict:
    """对 queries（1-4 个）各执行一次 DeepSeek native 搜索，合并结果。

    返回：
    {
      "ok": bool,
      "summary": str | None,          # DeepSeek 生成的综合答案（多查询取第一个非空）
      "sources": [ {url,title,snippet,published_at} ],  # 去重来源（受 max_results 限制）
      "per_query": [ {query, summary, sources} ],       # 各查询详细
      "error": str | None,
    }
    """
    key = api_key or _credential()
    if not key:
        return {"ok": False, "summary": None, "sources": [], "per_query": [], "error": "DeepSeek API key 未配置（DEEPSEEK_API_KEY）"}

    qs = [q for q in (queries or []) if isinstance(q, str) and q.strip()][:4]
    if not qs:
        return {"ok": False, "summary": None, "sources": [], "per_query": [], "error": "queries 必须包含至少一个非空查询"}

    import asyncio

    async def _one(query: str) -> dict:
        return await _native_search_one(key, model, query)

    # 先判断是否已在事件循环内（scheduler 的异步上下文），避免创建未消费的协程。
    try:
        asyncio.get_running_loop()
        in_loop = True
    except RuntimeError:
        in_loop = False

    if not in_loop:
        # 顶层/脚本调用：asyncio.run 一次性并发执行
        async def _run_all() -> list[dict]:
            return await asyncio.gather(*[_one(q) for q in qs])
        try:
            results = asyncio.run(_run_all())
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "summary": None, "sources": [], "per_query": [], "error": f"搜索失败：{e}"}
    else:
        # 已在事件循环内：用线程池隔离各查询的 asyncio 事件循环
        try:
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(qs))) as ex:
                futs = [ex.submit(lambda q=qq: asyncio.run(_one(q))) for qq in qs]
                results = [f.result(timeout=150) for f in futs]
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "summary": None, "sources": [], "per_query": [], "error": f"搜索失败：{e}"}

    per_query: list[dict] = []
    seen: set[str] = set()
    merged_sources: list[dict] = []
    fallback_summary: str | None = None
    for qi, res in zip(qs, results):
        per_query.append({"query": qi, "summary": res.get("summary"), "sources": res.get("sources", []), "error": res.get("error")})
        if fallback_summary is None and res.get("summary"):
            fallback_summary = res["summary"]
        for s in res.get("sources", []):
            u = s.get("url")
            if u and u not in seen:
                seen.add(u)
                merged_sources.append(s)

    if not max_results or max_results < 1:
        max_results = 10
    return {"ok": True, "summary": fallback_summary, "sources": merged_sources[:max_results], "per_query": per_query, "error": None}


async def _native_search_one(api_key: str, model: str, query: str) -> dict:
    import httpx
    endpoint = f"{DEEPSEEK_SEARCH_BASE}/messages"
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": f"请联网搜索并综合回答：{query}"}],
        "tools": [{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_USES}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=15.0)) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            if resp.status_code >= 400:
                detail = resp.text[:300]
                if resp.status_code == 401:
                    return {"summary": None, "sources": [], "error": f"DeepSeek key 无效（401）"}
                return {"summary": None, "sources": [], "error": f"DeepSeek 搜索 {resp.status_code}: {detail}"}
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        return {"summary": None, "sources": [], "error": f"网络错误：{e}"}

    return _parse_messages_response(data, query)


def _parse_messages_response(data: dict, query: str) -> dict:
    """从 Anthropic Messages 响应解析综合答案 + 结构化来源。"""
    content = data.get("content") or []
    summary_parts: list[str] = []
    sources: list[dict] = []
    seen: set[str] = set()

    for block in content:
        btype = block.get("type")
        if btype == "text":
            text = block.get("text", "")
            if text:
                summary_parts.append(text)
        elif btype == "web_search_tool_result":
            for item in block.get("content") or []:
                if item.get("type") != "web_search_result":
                    continue
                url = item.get("url", "")
                title = item.get("title", "")
                if url and url not in seen:
                    seen.add(url)
                    src: dict = {"url": url, "title": title}
                    pu = item.get("published_at") or item.get("publishedAt") or item.get("page_age")
                    if pu:
                        src["published_at"] = pu
                    # DeepSeek 在引用块里给 snippet，这里尽力取
                    src["snippet"] = item.get("snippet", "") or item.get("cited_text", "")
                    sources.append(src)

    summary = "\n\n".join(p for p in summary_parts if p).strip() or None
    return {"summary": summary, "sources": sources, "query": query, "error": None}


def format_search_result(result: dict) -> str:
    """把 native 搜索结果格式化为模型可见文本（dsh formatSearchOutput 风格）。"""
    parts: list[str] = []
    if result.get("summary"):
        parts.append(result["summary"])
    else:
        parts.append("No results found.")

    srcs = result.get("sources") or []
    if srcs:
        lines = []
        for s in srcs:
            label = s.get("title") or s.get("url")
            meta = []
            if s.get("snippet"):
                meta.append(s["snippet"])
            if s.get("published_at"):
                meta.append(f"({s['published_at']})")
            suffix = f" — {' '.join(meta)}" if meta else ""
            lines.append(f"- [{label}]({s['url']}){suffix}")
        parts.append("Sources:\n" + "\n".join(lines))

    # 多查询时提示来源覆盖度
    err = result.get("error")
    if err:
        parts.append(f"[搜索部分失败] {err}")
    parts.append("Cite the relevant URLs above as markdown links in your answer.")
    return "\n\n".join(parts)


# ---- HTML → Markdown（对齐 dsh web_fetch 的 turndown 语义，用 bs4 轻量实现）----

def html_to_markdown(html: str, max_chars: int = 12000) -> str:
    """把 HTML 正文转换为 Markdown，尽可能保留结构（标题/链接/粗斜体/列表/代码块）。

    行为对齐 dsh `web_fetch` 的 turndown 转换：
    - 丢弃 script/style/noscript；
    - 标题-> #/##/###，链接-> [text](url)，粗/斜-> **/ *，列表-> - / 1.，代码-> ```；
    - 超长按字符截断。
    输入若不是 HTML（如纯文本/JSON），原样返回（截断）。
    """
    if not html or not html.strip():
        return ""

    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        # 解析库不可用时退化为简单去标签
        import re as _re
        t = _re.sub(r"<script[\s\S]*?</script>", " ", html)
        t = _re.sub(r"<style[\s\S]*?</style>", " ", t)
        t = _re.sub(r"<[^>]+>", " ", t)
        return _collapse(_re.sub(r"\s+", " ", t)).strip()[:max_chars]

    for tag in soup(["script", "style", "noscript", "iframe", "svg"]):
        tag.decompose()

    lines: list[str] = []

    def walk(node, depth: int = 0) -> None:
        if depth > 12:
            return
        for el in node.children:
            name = getattr(el, "name", None)
            if name is None:
                text = str(el)
                if text.strip():
                    lines.append(text.strip())
                continue
            if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                lvl = int(name[1])
                txt = _inline(el).strip()
                if txt:
                    lines.append(f"{'#' * lvl} {txt}")
            elif name == "p":
                txt = _inline(el).strip()
                if txt:
                    lines.append(txt)
            elif name in ("ul", "ol"):
                items = []
                for li in el.find_all("li", recursive=False):
                    t = _inline(li).strip()
                    if t:
                        items.append(f"- {t}")
                if items:
                    lines.extend(items)
            elif name in ("pre", "code"):
                code = el.get_text().strip()
                if code:
                    lines.append("```\n" + code[:1500] + "\n```")
            elif name == "table":
                rows = []
                for tr in el.find_all("tr"):
                    cells = [_line(_inline(td)) for td in tr.find_all(["td", "th"])]
                    if cells:
                        rows.append("| " + " | ".join(cells) + " |")
                if rows:
                    sep = "|" + "|".join(["---"] * (rows[0].count("|") - 1)) + "|"
                    lines.append(rows[0]); lines.append(sep); lines.extend(rows[1:])
            elif name in ("br",):
                lines.append("")
            elif name in ("div", "section", "article", "main", "body"):
                walk(el, depth + 1)

    walk(soup.find("body") or soup)

    joined = "\n".join(l for l in lines if l is not None and str(l).strip())
    joined = _collapse(joined)
    if len(joined) > max_chars:
        joined = joined[:max_chars] + "\n…（内容过长已截断）"
    return joined.strip()


def _inline(el) -> str:
    """把一个元素内联渲染为 Markdown（处理 a/strong/em/img）。"""
    import re as _re
    out: list[str] = []
    for node in el.children:
        name = getattr(node, "name", None)
        if name is None:
            out.append(str(node))
            continue
        if name == "a":
            href = node.get("href")
            txt = _inline(node).strip()
            if href and txt:
                out.append(f"[{txt}]({href})")
            elif txt:
                out.append(txt)
        elif name in ("strong", "b"):
            txt = _inline(node).strip()
            if txt:
                out.append(f"**{txt}**")
        elif name in ("em", "i"):
            txt = _inline(node).strip()
            if txt:
                out.append(f"*{txt}*")
        elif name == "br":
            out.append(" ")
        elif name == "img":
            alt = node.get("alt", "")
            if alt:
                out.append(f"[图片: {alt}]")
        else:
            out.append(_inline(node))
    return _collapse("".join(out))


def _line(s: str) -> str:
    return str(s).replace("\n", " ").strip()


def _collapse(s: str) -> str:
    import re as _re
    return _re.sub(r"[ \t]+", " ", s)
