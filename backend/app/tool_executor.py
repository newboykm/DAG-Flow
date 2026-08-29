"""工具执行器：在 workspace 内实际执行工具。

- 文件/命令工具限制在 workspace 目录内（路径规范化防越界）。
- web_search/web_fetch 用 httpx 请求公开接口。
"""
from __future__ import annotations

import os
import re
import subprocess
import glob as globlib

import httpx


def _read_parent_output(ctx: dict | None, args: dict) -> str:
    """读取上游节点内容块的全文（按标题 + 可选块号）。"""
    if not ctx:
        return "无法读取上游产出：缺少会话上下文"
    sid = ctx.get("sessionId")
    parent_ids = ctx.get("parentIds") or []
    title = (args.get("parent_title") or "").strip()
    block_no = args.get("block_no")
    if not sid or not title:
        return "参数不完整：需要 parent_title"

    from .models import ContextBlock
    from .db import SessionLocal

    db = SessionLocal()
    try:
        q = (
            db.query(ContextBlock)
            .filter(
                ContextBlock.sessionId == sid,
                ContextBlock.nodeId.in_(parent_ids),
                ContextBlock.title == title,
            )
            .order_by(ContextBlock.seq.asc())
        )
        if block_no:
            blocks = [b for b in q.all() if b.seq == block_no]
        else:
            blocks = q.all()
        if not blocks:
            return f"未找到上游「{title}」的产出内容块"
        parts = []
        for b in blocks:
            parts.append(f"--- 「{b.title}」#{b.seq} ---\n{b.fulltext}")
        return "\n\n".join(parts)
    finally:
        db.close()


def _search_parent_memory(ctx: dict | None, args: dict) -> str:
    """语义检索上游父节点的历史产出（RAG）。"""
    if not ctx:
        return "无法检索上游记忆：缺少会话上下文"
    sid = ctx.get("sessionId")
    parent_ids = ctx.get("parentIds") or []
    query = (args.get("query") or "").strip()
    top_k = args.get("top_k") or 4
    if not sid or not query:
        return "参数不完整：需要 query"

    from . import memory
    try:
        hits = memory.search(sid, query, parent_ids, top_k=top_k)
    except Exception as e:  # noqa: BLE001
        return f"检索失败：{e}"
    if not hits:
        return "没有检索到相关内容"
    parts = [f"—— 语义检索命中 {len(hits)} 条 ——"]
    for h in hits:
        parts.append(f"[{h.get('title')} #{h.get('seq')}] {h.get('text', '')[:1200]}")
    return "\n\n".join(parts)


def _safe_path(workspace: str, p: str) -> str:
    """把用户传的路径规范化并限制在 workspace 内。"""
    if not os.path.isabs(p):
        p = os.path.join(workspace, p)
    p = os.path.normpath(p)
    ws = os.path.normpath(workspace)
    if p != ws and not p.startswith(ws + os.sep):
        raise PermissionError(f"路径越界，不允许访问工作目录外：{p}")
    return p


def _resolve(workspace: str, p: str) -> str:
    p = _safe_path(workspace, p)
    return os.path.abspath(p)


async def execute(name: str, args: dict, workspace: str, ctx: dict | None = None) -> str:
    """执行一个工具，返回文本结果。抛异常时由调用方处理。

    ctx: 可选上下文 {sessionId, parentIds}，供 read_parent_output 使用。
    """
    if name == "read_parent_output":
        return _read_parent_output(ctx, args)
    if name == "search_parent_memory":
        return _search_parent_memory(ctx, args)
    if name == "remember":
        from . import memo
        mtype = args.get("type") or "project"
        scope = args.get("scope") or "project"
        return memo.remember_item(scope, args.get("name", "memory"), mtype, args.get("body", ""))
    if name == "memory_search":
        from . import memo
        hits = memo.search_memories(args.get("query", ""))
        if not hits:
            return "（暂无记忆）"
        lines = ["—— 记忆检索结果 ——"]
        for h in hits:
            lines.append(f"- [{h['scope']}/{h['name']}] ({h['type']}): {h['preview']}")
        return "\n".join(lines)
    if name == "forget":
        from . import memo
        return memo.forget_item(args.get("scope") or "project", args.get("name", ""))
    if name == "read_file":
        p = _resolve(workspace, args.get("path", ""))
        if not os.path.isfile(p):
            return f"文件不存在：{args.get('path')}"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        return _clip_text(content, 6000, "文件内容过长已截断（如有需要，可用 read_file 结合参数或提示用户）")

    if name == "write_file":
        p = _resolve(workspace, args.get("path", ""))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(args.get("content", ""))
        return f"已写入 {p}"

    if name == "list_dir":
        p = _resolve(workspace, args.get("path", "."))
        if not os.path.isdir(p):
            return f"目录不存在：{args.get('path', '.')}"
        entries = sorted(os.listdir(p))
        lines = []
        for e in entries:
            full = os.path.join(p, e)
            kind = "dir" if os.path.isdir(full) else "file"
            lines.append(f"{kind}\t{e}")
        body = "\n".join(lines) or "(空目录)"
        if len(lines) > 50:
            body = "\n".join(lines[:50]) + f"\n…（共 {len(lines)} 项，仅显示前 50）"
        return body

    if name == "exec_command":
        cmd = args.get("command", "")
        if not cmd.strip():
            return "命令为空"
        try:
            proc = await _run_command(cmd, workspace)
            out = (proc.stdout or "")[:2000]
            err = (proc.stderr or "")[:1000]
            return (f"exit={proc.returncode}\nSTDOUT:\n{out}\nSTDERR:\n{err}").strip() or "exit=0"
        except Exception as e:  # noqa: BLE001
            import traceback as _tb
            print("[exec_command] failed:", repr(e), flush=True)
            _tb.print_exc()
            return f"执行失败：{e}"

    if name == "run_python":
        code = args.get("code", "")
        if not code.strip():
            return "代码为空"
        import asyncio
        import sys as _sys
        try:
            proc = await asyncio.create_subprocess_exec(
                _sys.executable, "-c", code,
                cwd=_resolve(workspace, "."),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            return (
                f"exit={proc.returncode}\nSTDOUT:\n{(stdout.decode(errors='replace') or '')[:2000]}"
                f"\nSTDERR:\n{(stderr.decode(errors='replace') or '')[:1000]}"
            ).strip() or "exit=0"
        except Exception as e:  # noqa: BLE001
            return f"执行失败：{e}"

    if name == "read_image":
        p = _resolve(workspace, args.get("path", ""))
        if not os.path.exists(p):
            return f"文件不存在：{args.get('path')}"
        import mimetypes
        size = os.path.getsize(p)
        mime, _ = mimetypes.guess_type(p)
        if os.path.isdir(p):
            return f"这是目录，非文件：{args.get('path')}"
        # 尽量读出可读文本概要；二进制返回元信息
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                head = f.read(800)
            if any(not ch.isprintable() and ch not in "\r\n\t" for ch in head[:200]):
                return f"{os.path.basename(p)}：二进制/图片文件，大小 {size} B，MIME {mime or '未知'}（本实现不提供视觉识别）"
            return f"{os.path.basename(p)}：大小 {size} B，MIME {mime or 'text/plain'}。内容概览：\n{head[:600]}"
        except Exception as e:  # noqa: BLE001
            return f"读取失败：{e}"

    if name == "search_files":
        pattern = args.get("pattern", "*")
        base = _resolve(workspace, ".")
        matches = globlib.glob(os.path.join(base, "**", pattern), recursive=True)
        rels = [os.path.relpath(m, workspace) for m in matches if os.path.isfile(m)]
        return "\n".join(rels[:200]) or "(没有匹配)"

    if name == "grep_content":
        pattern = args.get("pattern", "")
        if not pattern:
            return "正则表达式为空"
        glob_pat = args.get("glob") or "*"
        base = _resolve(workspace, ".")
        results = []
        for file in globlib.glob(os.path.join(base, "**", glob_pat), recursive=True):
            if not os.path.isfile(file):
                continue
            try:
                with open(file, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        if re.search(pattern, line):
                            results.append(f"{os.path.relpath(file, workspace)}:{i}: {line.strip()[:200]}")
            except Exception:
                continue
            if len(results) >= 200:
                break
        return "\n".join(results) or "(没有匹配)"

    if name == "web_search":
        query = args.get("query", "")
        return _web_search(query)

    if name == "web_fetch":
        url = args.get("url", "")
        return await _web_fetch(url)

    return f"未知工具：{name}"


def _clip_text(text: str, limit: int = 6000, hint: str = "…（内容过长已截断）") -> str:
    """截断工具返回文本，附带提示，避免全文塞入上下文。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n" + hint


async def _run_command(cmd: str, workspace: str):
    """异步执行命令。Windows 下：若命令为类 Unix 风格且本机有 Git-bash 的 sh，则用 sh -c（支持 wc/tail/ls/grep 等）；否则用 cmd /c。"""
    import asyncio
    import shutil
    if os.name == "nt":
        unixtools = ("ls ", "wc ", "tail ", "head ", "grep ", "find ", "cat ",
                      "chmod", "pwd", "mkdir ", "rm ", "touch ", "diff ", "sort ",
                      "cut ", "awk ", "sed ", "tee ", "history", "cp ", "mv ",
                      ";  ", "source ", "./")
        lower = " " + (cmd or "").lower() + " "
        uses_unix = any(u in lower for u in unixtools)
        sh = shutil.which("sh")
        if uses_unix and sh:
            shell = [sh, "-c", cmd]
        else:
            shell = ["cmd", "/c", cmd]
    else:
        shell = ["sh", "-c", cmd]
    proc = await asyncio.create_subprocess_exec(
        *shell,
        cwd=workspace,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    return type("R", (), {"returncode": proc.returncode, "stdout": stdout.decode(errors="replace"), "stderr": stderr.decode(errors="replace")})


def _web_search(query: str) -> str:
    """联网搜索：优先 Tavily（若有 key），失败回退 Bing，再回退 DuckDuckGo。"""
    api_key = _get_tavily_key()
    if api_key:
        r = _tavily_search(query, api_key)
        if r and r != "没有搜索结果":
            return r
    result = _bing_search(query)
    if result and result != "没有搜索结果":
        return result
    return _ddg_search(query)


def _get_tavily_key() -> str:
    """从 app_config 读取 Tavily API key。"""
    try:
        from .models import AppConfig
        from .db import SessionLocal
        from .secure import decrypt
        db = SessionLocal()
        try:
            row = db.query(AppConfig).filter(AppConfig.key == "tavily_api_key").first()
            return decrypt(row.value) if (row and row.value) else ""
        finally:
            db.close()
    except Exception:
        return ""


def _tavily_search(query: str, api_key: str) -> str:
    """Tavily 搜索（专为 LLM 设计，answer 字段可直接回答 + results 带 url/content）。"""
    import httpx
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
                "include_answer": True,
                "include_raw_content": False,
            },
            timeout=20,
        )
        if resp.status_code != 200:
            return f"Tavily 搜索失败（HTTP {resp.status_code}）"
        data = resp.json()
        parts = []
        answer = (data.get("answer") or "").strip()
        if answer:
            parts.append(f"直接答案：{answer}")
        results = data.get("results") or []
        for r in results:
            title = (r.get("title") or "").strip()
            url = r.get("url") or ""
            content = (r.get("content") or "").strip()[:300]
            if title or content:
                parts.append(f"{title}\n{content}\nURL: {url}")
        return "\n\n".join(parts) if parts else "没有搜索结果"
    except Exception as e:  # noqa: BLE001
        return f"Tavily 搜索失败：{e}"


def _strip_tags(html: str) -> str:
    import re as _re
    html = _re.sub(r"<script[\s\S]*?</script>", " ", html)
    html = _re.sub(r"<style[\s\S]*?</style>", " ", html)
    html = _re.sub(r"<[^>]+>", " ", html)
    html = _re.sub(r"&amp;", "&", html)
    html = _re.sub(r"&lt;", "<", html)
    html = _re.sub(r"&gt;", ">", html)
    html = _re.sub(r"&quot;", '"', html)
    html = _re.sub(r"&#\d+;", " ", html)
    return _re.sub(r"\s+", " ", html).strip()


def _bing_search(query: str) -> str:
    import urllib.parse
    import httpx
    import re as _re
    url = "https://cn.bing.com/search?q=" + urllib.parse.quote(query)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    try:
        html = httpx.get(url, headers=headers, timeout=20, follow_redirects=True).text
    except Exception as e:  # noqa: BLE001
        return f"联网搜索失败：{e}"
    # 每个结果块：<li class="b_algo"> ... <h2><a href>标题</a></h2> ... <p>摘要</p>
    items = []
    blocks = _re.findall(r'<li class="b_algo"[\s\S]*?(?=<li class="b_algo"|</ol>|</ul>)', html)
    for b in blocks:
        a_m = _re.search(r'<h2[^>]*>[\s\S]*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b)
        if not a_m:
            continue
        href = a_m.group(1)
        title = _strip_tags(a_m.group(2))
        cap_m = _re.search(r'<p[^>]*>(.*?)</p>', b)
        cap = _strip_tags(cap_m.group(1)) if cap_m else ""
        if title:
            items.append(f"{title}\n{cap}\nURL: {href}")
        if len(items) >= 5:
            break
    return "\n\n".join(items) if items else "没有搜索结果"


def _ddg_search(query: str) -> str:
    # DuckDuckGo HTML 端点，返回摘要（无需 key）
    import urllib.request
    import urllib.parse
    import re as _re
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
    except Exception as e:  # noqa: BLE001
        return f"联网搜索失败：{e}"
    results = _re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', html)
    if not results:
        results = _re.findall(r'class="result__snippet"[^>]*>(.*?)</', html)
    snippets = []
    for r in results:
        text = _re.sub(r"<[^>]+>", "", r).strip()
        if text:
            snippets.append(text)
            if len(snippets) >= 5:
                break
    return "\n".join(snippets) if snippets else "没有搜索结果"


async def _web_fetch(url: str) -> str:
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=15.0),
            follow_redirects=True,
        ) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            text = resp.text[:8000]
            # 粗去标签
            text = re.sub(r"<script[\s\S]*?</script>", "", text)
            text = re.sub(r"<style[\s\S]*?</style>", "", text)
            text = re.sub(r"<[^>]+>", " ", text)
            text = re.sub(r"\s+", " ", text)
            body = text.strip()
            if not body or len(body) < 80:
                # 可能是 JS 动态渲染页（SPA）或反爬页，没有有效正文
                return (
                    "该页面可能为 JS 动态渲染或存在反爬，抓取不到有效正文。"
                    "请优先基于 web_search 返回的摘要与已有知识回答；"
                    "确需该页内容时，尝试其他来源的链接，不要反复重抓同一页面。"
                )
            return body
    except httpx.HTTPStatusError as e:
        return (
            f"网页请求失败（HTTP {e.response.status_code}）。"
            "请改用 web_search 的摘要或其他来源回答，不要反复重试同一 URL。"
        )
    except httpx.ConnectTimeout:
        return (
            f"网页连接超时，无法访问：{url}（目标站点可能不可达）。"
            "请改用 web_search 摘要或其他来源回答。"
        )
    except httpx.TimeoutException:
        return f"网页请求超时：{url}。请改用 web_search 摘要或其他来源回答。"
    except Exception as e:  # noqa: BLE001
        return f"网页抓取失败：{e}。请改用 web_search 摘要或其他来源回答。"
