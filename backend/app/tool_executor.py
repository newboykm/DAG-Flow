"""工具执行器：在 workspace 内实际执行工具。

- 文件/命令工具限制在 workspace 目录内（路径规范化防越界）。
- web_search/web_fetch 用 httpx 请求公开接口。
"""
from __future__ import annotations

import os
import re
import time
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


# ---- dsh 风格的 read/edit 工具（read-before-edit 一致性追踪）----
_READ_MAX_LINE_LENGTH = 2000
_READ_MAX_BYTES = 50 * 1024
_READ_DEFAULT_LIMIT = 2000
# 绝对路径 -> (size, mtime)，read 成功后记录，edit 前校验是否已读且未被外部改动。
_file_observations: dict[str, tuple[int, float]] = {}


def _mark_observed(path: str) -> None:
    try:
        _file_observations[path] = (os.path.getsize(path), os.path.getmtime(path))
    except Exception:
        pass


def _is_observed_current(path: str) -> bool:
    obs = _file_observations.get(path)
    if obs is None:
        return False
    try:
        return obs[0] == os.path.getsize(path) and abs(obs[1] - os.path.getmtime(path)) < 1e-6
    except Exception:
        return False


def _truncate_read_line(line: str) -> str:
    if len(line) <= _READ_MAX_LINE_LENGTH:
        return line
    return f"{line[:_READ_MAX_LINE_LENGTH]}... (line truncated to {_READ_MAX_LINE_LENGTH} chars)"


def _dsh_read(args: dict, workspace: str) -> str:
    """dsh `read`：带行号 + 分页 + 单行截断 + 字节上限。"""
    fp = args.get("file_path", "")
    p = _resolve(workspace, fp)
    if not os.path.isfile(p):
        return f"文件不存在：{fp}"
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except Exception as e:  # noqa: BLE001
        return f"读取失败：{e}"

    total = len(lines)
    offset = int(args.get("offset") or 1)
    limit = int(args.get("limit") or _READ_DEFAULT_LIMIT)
    if offset < 1:
        offset = 1
    if limit < 1:
        limit = 1
    if offset > total and not (total == 0 and offset == 1):
        return f"offset {offset} 超出范围：{fp}（共 {total} 行）"

    out_lines = []
    byte_acc = 0
    truncated_by_bytes = False
    shown = 0
    for i in range(offset - 1, total):
        if shown >= limit:
            break
        raw = lines[i].rstrip("\r\n")
        text = _truncate_read_line(raw)
        byte_acc += len(text.encode("utf-8", errors="replace"))
        if byte_acc > _READ_MAX_BYTES:
            truncated_by_bytes = True
            break
        out_lines.append(f"{i + 1}: {text}")
        shown += 1

    body = "\n".join(out_lines) if out_lines else "（无内容）"
    tail = ""
    if shown < total - (offset - 1):
        tail = f"\n…（仅显示 {shown} 行，共 {total} 行；可用 read offset={offset + shown} 继续）"
    elif truncated_by_bytes:
        tail = "\n…（达到字节上限截断）"
    _mark_observed(p)
    return f"=== {fp}（共 {total} 行，本次 {shown} 行）===\n{body}{tail}"


def _str_replace_editor(args: dict, workspace: str) -> str:
    """str_replace_editor（对齐 dsh tool-str-replace-editor）。

    命令：
    - view: 文件→带行号(cat -n)；目录→列非隐藏项(最多2层)
    - create: 创建文件(path 存在则拒绝)
    - str_replace: 精确字面替换，old_str 必须唯一（重复拒绝）
    - insert: 在 insert_line 之后插入 new_str
    状态跨调用（读-改一致 + 最近的 str_replace/insert 记录支持 undo）。
    """
    cmd = str(args.get("command", "")).strip()
    path_arg = str(args.get("path", "") or "").strip()
    if not cmd or not path_arg:
        return "str_replace_editor 需要 command 和 path"
    p = _resolve(workspace, path_arg)

    if cmd == "view":
        if os.path.isdir(p):
            return _sre_view_dir(p, workspace)
        if not os.path.isfile(p):
            return f"文件不存在：{path_arg}"
        start, end = 1, -1
        vr = args.get("view_range")
        if isinstance(vr, list) and len(vr) == 2 and isinstance(vr[0], int):
            start = max(1, vr[0])
            end = vr[1]
        return _sre_view_file(p, workspace, start, end)

    if cmd == "create":
        if os.path.exists(p):
            return f"create 失败：{path_arg} 已存在，不可重复创建。"
        content = str(args.get("file_text", "") or "")
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        _mark_observed(p)
        return f"文件已创建：{path_arg}（{len(content)} 字符）"

    if cmd == "str_replace":
        old_str = str(args.get("old_str", "") or "")
        new_str = str(args.get("new_str", "") or "")
        if not old_str:
            return "str_replace 需要 old_str"
        if old_str == new_str:
            return "old_str 与 new_str 必须不同"
        if not os.path.isfile(p):
            return f"文件不存在：{path_arg}"
        if not _is_observed_current(p):
            return f"请先 view 该文件（read)后再 str_replace，以确保基于当前内容替换。"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        count = content.count(old_str)
        if count == 0:
            return f"在 {path_arg} 中未找到 old_str（需精确匹配含空白）。"
        if count > 1:
            return f"old_str 在 {path_arg} 中出现 {count} 次，不唯一。请加更多上下文使其唯一。"
        after = content.replace(old_str, new_str, 1)
        _sre_remember(p, content)  # 记录 undo 快照
        with open(p, "w", encoding="utf-8") as f:
            f.write(after)
        _mark_observed(p)
        removed = len(old_str) - len(new_str)
        return f"文件 {path_arg} 已更新（移除 {max(0, removed)} 字符）。"

    if cmd == "insert":
        insert_line = int(args.get("insert_line") or 0)
        new_str = str(args.get("new_str", "") or "")
        if insert_line < 0:
            return "insert_line 需 ≥ 0"
        if not os.path.isfile(p):
            return f"文件不存在：{path_arg}"
        if not _is_observed_current(p):
            return f"请先 view 该文件后再 insert，以确保基于当前内容。"
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        idx = min(insert_line, len(lines))
        lines.insert(idx, new_str + ("\n" if not new_str.endswith("\n") else ""))
        _sre_remember(p, "".join(lines))
        with open(p, "w", encoding="utf-8") as f:
            f.writelines(lines)
        _mark_observed(p)
        return f"已在 {path_arg} 第 {insert_line} 行后插入。"

    return f"未知 command：{cmd}（允许 view/create/str_replace/insert）"


# str_replace_editor 的 undo 快照（相对路径 → 最近一次替换前的完整内容）
_sre_undo: dict[str, str] = {}


def _sre_remember(p: str, before: str) -> None:
    _sre_undo[p] = before


# 观测复用 _is_observed_current / _mark_observed（init 自托管，无需额外 import）


def _sre_view_file(p: str, workspace: str, start: int, end: int) -> str:
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    _mark_observed(os.path.abspath(p))
    total = len(lines)
    last = total if end == -1 else min(end, total)
    out = []
    for i in range(start - 1, last):
        if i < 0 or i >= total:
            break
        out.append(f"{i + 1}\t{lines[i].rstrip()}")
    body = "\n".join(out)
    clipped = last < total or start > 1
    suffix = "\n<response clipped>" if clipped and len(body) > 6000 else ""
    if len(body) > 6000:
        body = body[:6000] + "\n…"
    return f"File: {os.path.relpath(p, workspace)}\n{body}{suffix}"


def _sre_view_dir(p: str, workspace: str) -> str:
    entries = []
    import fnmatch as _fn
    for root, dirs, files in os.walk(p):
        depth = root[len(p):].count(os.sep)
        if depth >= 2:
            dirs[:] = []
            continue
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        files = [f for f in files if not f.startswith('.')]
        for f in sorted(files):
            entries.append(os.path.relpath(os.path.join(root, f), workspace))
        for d in sorted(dirs):
            entries.append(os.path.relpath(os.path.join(root, d), workspace) + "/")
        if depth >= 2:
            dirs[:] = []
    if not entries:
        return "（空/全隐藏目录）"
    rel = os.path.relpath(p, workspace)
    return f"Directory: {rel}\n" + "\n".join(entries[:200])


def _dsh_edit(args: dict, workspace: str) -> str:
    """dsh `edit`：精确字面替换 + read-before-edit 校验。"""
    fp = args.get("file_path", "")
    old = args.get("old_string", "")
    new = args.get("new_string", "")
    replace_all = bool(args.get("replace_all", False))
    p = _resolve(workspace, fp)

    if not old:
        return "old_string 必须为非空字符串"
    if old == new:
        return "old_string 与 new_string 必须不同"
    if not os.path.isfile(p):
        return f"文件不存在：{fp}"

    if not _is_observed_current(p):
        return (
            f"该文件在本次会话中尚未被 read，或读取后已被外部修改。"
            f"请先调用 read 读取 {fp}（可用 read offset/limit 分页），再执行 edit，以确保基于当前内容做精确替换。"
        )

    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:  # noqa: BLE001
        return f"读取失败：{e}"

    count = content.count(old)
    if count == 0:
        return (
            f"未在 {fp} 中找到匹配的 old_string。它必须与文件内容完全一致（含缩进/换行）。"
            f"请先用 read 查看实际内容，再提供精确匹配。"
        )
    if not replace_all and count > 1:
        return (
            f"old_string 在 {fp} 中出现 {count} 次。请提供更长/更精确的 old_string 使其唯一，"
            f"或设置 replace_all=true 替换全部。"
        )

    after = content.replace(old, new) if replace_all else content.replace(old, new, 1)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(after)
    except Exception as e:  # noqa: BLE001
        return f"写入失败：{e}"

    _mark_observed(p)
    return f"文件 {fp} 已更新成功。" if not replace_all else f"文件 {fp} 已更新成功，所有匹配均已替换。"


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
        query = str(args.get("query", "") or "").strip()
        if not query:
            return "请提供检索关键词（query）。"
        lines = ["—— 跨会话/历史检索结果 ——"]
        found = 0
        # 1) 关键词记忆条目
        from . import memo
        for h in memo.search_memories(query):
            lines.append(f"- [记忆 {h['scope']}/{h['name']}] ({h['type']}): {h['preview']}")
            found += 1
        # 2) 当前 session 的语义内容块（历史节点产出，RAG）
        try:
            from . import memory
            sid = (ctx or {}).get("sessionId") if ctx else None
            if sid:
                hits = memory.search(sid, query, [], top_k=int(args.get("top_k") or 4))
                for h in hits:
                    lines.append(f"- [内容块 {h.get('title','')} #{h.get('seq')}] {str(h.get('text',''))[:300]}")
                    found += 1
        except Exception:
            pass
        # 3) event_log 执行轨迹里命中的步骤（说明"之前做了什么/为什么"）
        try:
            from . import event_log
            sid = (ctx or {}).get("sessionId") if ctx else None
            if sid:
                import glob as _g
                import os as _os
                evdir = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))), ".event_logs", sid)
                ql = query.lower()
                ev_count = 0
                for evf in sorted(_g.glob(_os.path.join(evdir, "*.jsonl"))):
                    try:
                        for raw in open(evf, encoding="utf-8", errors="replace"):
                            if ql in raw.lower():
                                import json as _j
                                try:
                                    rec = _j.loads(raw)
                                except Exception:
                                    continue
                                k = rec.get("kind")
                                if k in ("tool_call", "tool_result", "llm_done", "reflection"):
                                    flags = "[执行]" if k == "tool_result" else ("[调用]" if k == "tool_call" else ("[回答]" if k == "llm_done" else "[反思]"))
                                    text = (rec.get("tool") or rec.get("text") or rec.get("result_preview") or "")[:200]
                                    lines.append(f"- {flags} {text}")
                                    ev_count += 1
                                    if ev_count >= 6:
                                        raise StopIteration
                    except StopIteration:
                        break
                    except Exception:
                        continue
                found += ev_count
        except Exception:
            pass
        if not found:
            return "没有检索到相关内容。"
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
        _mark_observed(os.path.abspath(p))
        return _clip_text(content, 6000, "文件内容过长已截断（如有需要，可用 read_file 结合参数或提示用户）")

    if name == "read":
        return _dsh_read(args, workspace)

    if name == "edit":
        return _dsh_edit(args, workspace)

    if name == "str_replace_editor":
        return _str_replace_editor(args, workspace)

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
        # run_in_background=true 时后台运行返回 job id（对齐 dsh tool-bash）
        if args.get("run_in_background"):
            jid = _start_bg_job(cmd, workspace)
            return f"已在后台启动任务 {jid}（用 job_output 读取结果，job_kill 停止，job_list 查看）"
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

    if name == "job_list":
        return _job_list_text()
    if name == "job_output":
        return _job_output_text(args.get("job_id", ""))
    if name == "job_kill":
        return _job_kill(args.get("job_id", ""))
    if name == "skill":
        # 按需加载 skill 完整指令（对齐 dsh tool-skill）
        return _tool_skill(args, workspace)

    if name == "session_event_search":
        return _session_event_search(str(args.get("query", "") or ""), args.get("session_id") or None, ctx)

    if name == "session_event_read":
        return _session_event_read(args.get("session_id") or None, args.get("node_id") or None, int(args.get("seq") or 0), ctx)

    if name == "web_search":
        # 支持 dsh 语义的 queries[]（1-4 个），也兼容旧的单 query。
        queries = args.get("queries") or []
        if not queries or not isinstance(queries, list) or not any(q for q in queries):
            if args.get("query"):
                queries = [args.get("query")]
            else:
                return "queries 必须包含至少一个非空查询（可最多 4 个不同角度的关键词）"
        queries = [str(q).strip() for q in queries if str(q).strip()][:4]
        return _web_search(queries)

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
    try:
        proc = await asyncio.create_subprocess_exec(
            *shell,
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        return type("R", (), {"returncode": proc.returncode, "stdout": stdout.decode(errors="replace"), "stderr": stderr.decode(errors="replace")})
    except NotImplementedError:
        # Windows 上 SelectorEventLoop 不支持子进程（_make_subprocess_transport 抛 NotImplementedError）。
        # 退回线程内 subprocess.run，与事件循环类型无关，保证 exec_command 在各种 loop 下都可用。
        def _run_sync():
            p = subprocess.run(
                shell,
                cwd=workspace,
                capture_output=True,
                timeout=120,
            )
            return p.returncode, p.stdout, p.stderr

        returncode, stdout, stderr = await asyncio.to_thread(_run_sync)
        return type("R", (), {"returncode": returncode, "stdout": stdout.decode(errors="replace"), "stderr": stderr.decode(errors="replace")})


# ---- bash 后台任务（job）管理，对齐 dsh tool-bash run_in_background + tool-jobs ----
import uuid as _uuid
import threading as _threading

_JOBS_MAX_OUTPUT = 200_000  # 后台任务输出保留上限（字符）


class _BgJob:
    __slots__ = ("id", "proc", "done", "status", "stdout", "stderr", "created_at")
    def __init__(self, proc):
        self.id = "job-" + _uuid.uuid4().hex[:8]
        self.proc = proc
        self.done = _threading.Event()
        self.status = "running"
        self.stdout = ""
        self.stderr = ""
        self.created_at = time.time()


_jobs: dict[str, _BgJob] = {}


def _resolve_shell_cmd(cmd: str):
    """返回执行命令的 shell 列表（Windows 兼容类 Unix + cmd）。"""
    import shutil
    if os.name == "nt":
        unixtools = ("ls ", "wc ", "tail ", "head ", "grep ", "find ", "cat ",
                      "chmod", "pwd", "mkdir ", "rm ", "touch ", "diff ", "sort ",
                      "cut ", "awk ", "sed ", "tee ", "history", "cp ", "mv ",
                      ";  ", "source ", "./")
        lower = " " + (cmd or "").lower() + " "
        uses_unix = any(u in lower for u in unixtools)
        sh = shutil.which("sh")
        return [sh, "-c", cmd] if (uses_unix and sh) else ["cmd", "/c", cmd]
    return ["sh", "-c", cmd]


def _start_bg_job(cmd: str, workspace: str) -> str:
    """后台启动命令，立即返回 job id。"""
    try:
        proc = subprocess.Popen(
            _resolve_shell_cmd(cmd),
            cwd=workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as e:  # noqa: BLE001
        return f"后台启动失败：{e}"
    job = _BgJob(proc)
    _jobs[job.id] = job

    def _reader():
        try:
            out, err = proc.communicate(timeout=1800)
        except Exception:
            out = ""
            err = ""
            try:
                proc.kill()
            except Exception:
                pass
        job.stdout = (job.stdout + out)[-_JOBS_MAX_OUTPUT:]
        job.stderr = (job.stderr + err)[-_JOBS_MAX_OUTPUT:]
        job.status = "done" if proc.returncode == 0 else "failed"
        job.done.set()

    _threading.Thread(target=_reader, daemon=True).start()
    return job.id


def _job_cmd_desc(proc) -> str:
    a = getattr(proc, "args", "")
    if isinstance(a, str):
        return a
    if isinstance(a, (list, tuple)):
        return " ".join(str(x) for x in a)
    return str(a)


def _job_list_text() -> str:
    lines = []
    for job in _jobs.values():
        st = "running" if not job.done.is_set() else job.status
        lines.append(f"- {job.id} [{st}] {_clip_text(_job_cmd_desc(job.proc), 80)}")
    if not lines:
        return "（没有后台任务）"
    return "\n".join(lines)


def _job_output_text(job_id: str) -> str:
    job = _jobs.get(job_id)
    if not job:
        return f"后台任务不存在：{job_id}"
    st = "running" if not job.done.is_set() else job.status
    out = (job.stdout or "").strip()
    err = (job.stderr or "").strip()
    parts = [f"[job {job_id}] status={st}"]
    if out:
        parts.append(f"STDOUT:\n{out}")
    if err:
        parts.append(f"STDERR:\n{err[:_JOBS_MAX_OUTPUT]}")
    return "\n\n".join(parts) if (out or err) else f"[job {job_id}] status={st}（暂无输出）"


def _job_kill(job_id: str) -> str:
    job = _jobs.get(job_id)
    if not job:
        return f"后台任务不存在：{job_id}"
    if job.done.is_set():
        return f"[job {job_id}] 已结束（{job.status}）"
    try:
        job.proc.kill()
        job.done.set()
        job.status = "killed"
        return f"[job {job_id}] 已停止"
    except Exception as e:  # noqa: BLE001
        return f"停止失败：{e}"


def _tool_skill(args: dict, workspace: str) -> str:
    """skill 工具：按名称加载完整指令（对齐 dsh tool-skill 的按需加载）。"""
    from . import skills as _skills
    name = str(args.get("name", "")).strip()
    if not name:
        return "请提供要加载的 skill 名称（skill 参数 name）。"
    # 读取用户自定义 skill 目录（从 app_config）
    custom_dir = None
    try:
        from .models import AppConfig
        from .db import SessionLocal
        db = SessionLocal()
        try:
            row = db.query(AppConfig).filter(AppConfig.key == "skill_dir").first()
            custom_dir = row.value if row else None
        finally:
            db.close()
    except Exception:
        pass
    sk = _skills.get_skill(name, custom_dir)
    if not sk:
        avail = _skills.all_skills(custom_dir)
        names = "、".join(s["name"] for s in avail) or "（当前无可用 skill）"
        return f"未找到名为「{name}」的 skill。可用 skill：{names}"
    # 渲染完整指令（对齐 dsh renderSkillContent 的 <skill_content> 块）
    body = sk.get("content") or sk.get("description") or "（无内容）"
    return (
        f"<skill_content name=\"{sk['name']}\">\n"
        f"【{sk['name']}】 {sk.get('description', '') or ''}\n\n"
        f"{body}"
        f"\n</skill_content>"
    )


def _session_event_search(query: str, session_id: str | None, ctx: dict | None) -> str:
    """全文搜某会话的 event_log 事件（对齐 dsh session_event_search）。

    session_id 缺省时用当前会话（ctx.sessionId）；可选跨会话。
    返回命中事件(seq+kind+内容摘要)，最多若干条。
    """
    import glob as _g
    import json as _j
    q = (query or "").strip()
    if not q:
        return "session_event_search 需要 query"
    from . import event_log as _el
    target = session_id or ((ctx or {}).get("sessionId") if ctx else None)
    base = _el._EVENT_LOG_DIR
    if not os.path.isdir(base):
        return "（尚无执行事件日志）"
    if target:
        dirs = [os.path.join(base, target)]
    else:
        dirs = [d for d in _g.glob(os.path.join(base, "*")) if os.path.isdir(d)]
    ql = q.lower()
    hits: list[dict] = []
    for d in dirs:
        for evf in _g.glob(os.path.join(d, "*.jsonl")):
            sid = os.path.basename(d)
            nid = os.path.basename(evf)[:-6]
            try:
                with open(evf, encoding="utf-8", errors="replace") as f:
                    for raw in f:
                        if ql in raw.lower():
                            try:
                                rec = _j.loads(raw)
                            except Exception:
                                continue
                            kind = rec.get("kind")
                            if kind in ("llm_done", "tool_call", "tool_result", "reflection", "turn_end", "step_start"):
                                text = (rec.get("text") or rec.get("tool") or rec.get("result_preview") or rec.get("reasoning") or rec.get("reason") or rec.get("status") or "")[:200]
                                hits.append({"sid": sid, "nid": nid, "seq": rec.get("seq"), "kind": kind, "ts": rec.get("ts"), "preview": text})
            except Exception:
                continue
    if not hits:
        return f"在会话{target or '全部'}的事件中未找到与「{query}」相关的内容。"
    hits.sort(key=lambda h: h["ts"] or 0)
    lines = [f"—— 事件命中 {len(hits[:20])} 条 ——"]
    for h in hits[:20]:
        who = f"{h['sid']}/{h['nid']}"
        lines.append(f"[seq {h['seq']} {h['sid']}] #{h['kind']} {h['preview']}")
    return "\n".join(lines)


def _session_event_read(session_id: str | None, node_id: str | None, seq: int, ctx: dict | None) -> str:
    """按 seq 读某个会话事件的完整内容（对齐 dsh session_event_read）。"""
    import glob as _g
    import json as _j
    from . import event_log as _el
    base = _el._EVENT_LOG_DIR
    target = session_id or ((ctx or {}).get("sessionId") if ctx else None)
    if not target or not base:
        return "无法定位会话。"
    pattern = os.path.join(base, target, "*.jsonl")
    for evf in _g.glob(pattern):
        if node_id and os.path.basename(evf)[:-6] != node_id:
            continue
        try:
            with open(evf, encoding="utf-8", errors="replace") as f:
                for raw in f:
                    try:
                        rec = _j.loads(raw)
                    except Exception:
                        continue
                    if rec.get("seq") == seq:
                        return _j.dumps(rec, ensure_ascii=False, indent=2)
        except Exception:
            continue
    return f"未找到 seq={seq} 的事件。"


def _web_search(queries: list[str]) -> str:
    """联网搜索：优先 DeepSeek native（高质量综合答案 + 权威来源，对齐 dsh），
    失败时回退到现有免费引擎（Tavily/Bing/DDG）。
    """
    # 1) DeepSeek native 搜索（dsh 同款）
    try:
        from . import web_search as _ws
    except ImportError:
        import web_search as _ws
    try:
        result = _ws.native_search(queries)
        if result.get("ok"):
            formatted = _ws.format_search_result(result)
            # 至少要有答案或来源才算真拿到结果
            if result.get("summary") or result.get("sources"):
                return formatted
    except Exception as e:  # noqa: BLE001
        import traceback as _tb
        print("[web_search] native failed:", repr(e), flush=True)

    # 2) 回退：多查询逐个走旧引擎
    outputs = []
    for _q in queries[:2]:
        outputs.append(_web_search_legacy(_q, "basic"))
    return "\n\n---\n\n".join(outputs) if outputs else "（搜索失败）"


def _web_search_legacy(query: str, depth: str = "basic") -> str:
    """旧实现：免费引擎搜索（保留 fallback）。"""
    if depth == "deep":
        return _web_search_deep(query)
    # 快速搜索：Tavily 优先，失败回退 Bing，再回退 DuckDuckGo
    api_key = _get_tavily_key()
    if api_key:
        r = _tavily_search(query, api_key)
        if r and r != "没有搜索结果":
            return r
    result = _bing_search(query)
    if result and result != "没有搜索结果":
        return result
    return _ddg_search(query)


def _web_search_deep(query: str) -> str:
    """重型检索：跨多个来源搜集结果做交叉，必要时自动换词扩展，再抓最相关原文深挖。

    返回结构化文本，告知收录来源数量与"仅单一来源"的置信度提示，
    引导使用者（agent/LLM）在关键结论上主动做多源交叉核验。
    """
    import re as _re

    engine_results: list[tuple[str, str]] = []  # (来源, 文本)

    def _add(name: str, text: str) -> None:
        if text and text != "没有搜索结果" and not str(text).startswith("联网") \
                and not str(text).startswith("Tavily"):
            engine_results.append((name, text))

    # 1) 三个引擎并行搜集
    api_key = _get_tavily_key()
    if api_key:
        _add("Tavily", _tavily_search(query, api_key))
    _add("Bing", _bing_search(query))
    _add("DuckDuckGo", _ddg_search(query))

    # 2) 若三个来源都不理想，换更聚焦的词再试（自动扩展）
    if len(engine_results) < 2:
        refined = _refine_query(query)
        if refined and refined != query:
            if api_key:
                _add("Tavily(扩展)", _tavily_search(refined, api_key))
            _add("Bing(扩展)", _bing_search(refined))
            _add("DuckDuckGo(扩展)", _ddg_search(refined))

    # 去重：按来源+首个 URL/标题片段粗略去重
    seen: set[str] = set()
    distinct: list[str] = []
    for name, text in engine_results:
        key = _re.sub(r"\s+", "", (text or ""))[:60]
        if key in seen:
            continue
        seen.add(key)
        distinct.append(text)

    # 3) 从最富信息的结果里提取 URL，抓前 2 条原文深挖（尽力而为，失败不阻塞）
    snippets = []
    for text in distinct:
        for u in re.findall(r"https?://[^\s)]+", text):
            if u not in snippets:
                snippets.append(u)
    fetched_details: list[str] = []
    for u in snippets[:2]:
        try:
            body = _fetch_sync(u)
            if body and not body.startswith("该页面") and not body.startswith("网页"):
                fetched_details.append(f"[原文 {u}]\n{_clip_text(body, 900)}")
        except Exception:
            continue

    # 4) 组装返回：来源覆盖度 + 各来源 + 原文深挖
    parts = [
        f"【搜索来源】共 {len(distinct)} 个独立来源（{len(engine_results)} 次请求）。",
        "",
    ]
    for i, text in enumerate(distinct, 1):
        parts.append(f"—— 来源 {i} ——\n{_clip_text(text, 1200)}")
    if fetched_details:
        parts.append("【命中原文深挖】")
        parts.extend(fetched_details)
    if len(distinct) <= 1:
        parts.append("⚠ 仅 1 个来源，信息置信度有限：关键结论请用第二来源交叉核验后再下判断。")
    return "\n\n".join(parts)


def _refine_query(query: str) -> str:
    """自动扩展检索词：尝试几种变体，返回更可能命中的一次查询。

    对中文/英文 query 做保守增强：补常见限定词（正式名称、最新、官方、详情），
    以及给关键词加引号提升精确匹配。默认知晓多来源核实。
    """
    import re as _re
    s = (query or "").strip()
    if not s:
        return s
    variants = [
        s,
        s + " 官方 最新 详情",
        s + " 新闻 发布会 详情",
        f'"{s}"',
    ]
    # 中文查询：去掉重复/无意义动词，补主题限定
    if _re.search(r"[\u4e00-\u9fa5]", s):
        cleaned = _re.sub(r"[些怎么请问帮我检索介绍讲讲调查一下什么是找一个]/g", " ", s)
        cleaned = _re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            variants.append(cleaned + " 详情 最新动态")
    return variants[-1]



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
            content_type = resp.headers.get("content-type", "").lower()
            raw = resp.text
            # 若为 HTML，转成 Markdown（dsh web_fetch 同款，保留标题/链接/列表/代码块结构）
            try:
                from . import web_search as _ws
            except ImportError:
                import web_search as _ws
            if "text/html" in content_type or raw.lstrip().startswith("<") or ("<html" in raw[:2000].lower()):
                body = _ws.html_to_markdown(raw)
                body = f"[抓取 {url} 的 Markdown 正文]\n\n{body or '（页面无有效正文）'}"
            else:
                # 非 HTML（如 JSON/纯文本）：直接截断返回
                body = raw[:12000]
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


def _fetch_sync(url: str) -> str:
    """深度搜索内部使用的同步抓取，语义与 _web_fetch 一致。"""
    if not (url.startswith("http://") or url.startswith("https://")):
        url = "https://" + url
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=True,
        )
        resp.raise_for_status()
        text = resp.text[:8000]
        text = re.sub(r"<script[\s\S]*?</script>", "", text)
        text = re.sub(r"<style[\s\S]*?</style>", "", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        body = text.strip()
        if not body or len(body) < 80:
            return "该页面可能为 JS 动态渲染或存在反爬，抓取不到有效正文。"
        return body
    except Exception:
        return ""
