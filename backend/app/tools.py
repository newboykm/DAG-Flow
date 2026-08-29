"""Agent 工具集定义：OpenAI function-calling schema + 敏感度标记。

工具分两类：
- 非敏感（只读）：read_file / list_dir / search_files / grep_content / web_search / web_fetch
- 敏感（需人工审批）：write_file / exec_command
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    # 敏感工具在调用前需要用户在卡片里审批（human-in-the-loop）
    # write_file / exec_command 改为按内容动态判定（见 needs_approval），此处置 False
    requires_approval: bool = False


TOOLS: list[Tool] = [
    Tool(
        name="read_file",
        description="读取工作目录内某个文件的完整内容。参数用相对/绝对路径。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径（工作目录内）"},
            },
            "required": ["path"],
        },
    ),
    Tool(
        name="write_file",
        description="在工作目录内写入/覆盖一个文件。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
                "content": {"type": "string", "description": "要写入的内容"},
            },
            "required": ["path", "content"],
        },
    ),
    Tool(
        name="list_dir",
        description="列出工作目录内某个目录下的文件/子目录（默认根目录）。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径，默认 '.'"},
            },
            "required": [],
        },
    ),
    Tool(
        name="exec_command",
        description="在工作目录内执行一条命令并返回 stdout/stderr。运行环境为 Windows，已兼容常见类 Unix 命令（ls/wc/tail/grep/cat/find 等会经 Git-bash 执行）；改文件建议用 write_file，删除/危险命令需审批。",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令"},
            },
            "required": ["command"],
        },
    ),
    Tool(
        name="search_files",
        description="按文件名模式在工作目录内递归搜索文件（glob）。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "glob 模式，如 *.py"},
            },
            "required": ["pattern"],
        },
    ),
    Tool(
        name="grep_content",
        description="在工作目录内递归搜索文件内容（正则）。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "glob": {"type": "string", "description": "可选文件过滤，如 *.py"},
            },
            "required": ["pattern"],
        },
    ),
    Tool(
        name="read_parent_output",
        description=(
            "读取上游（父）节点的完整产出内容。参数 parent_title 填上游卡片标题，"
            "block_no 可选（不填则返回该上游节点全部内容块按时间顺序的全文）。"
            "当摘要索引不够、需要上游的完整任务/对话内容时调用本工具。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "parent_title": {"type": "string", "description": "上游节点标题（目录索引里的名字）"},
                "block_no": {"type": "integer", "description": "可选，指定第几块（1 起），不填返回全部"},
            },
            "required": ["parent_title"],
        },
    ),
    Tool(
        name="search_parent_memory",
        description=(
            "在上游（父）节点的历史产出中做语义检索，返回与 query 最相关的若干内容片段（RAG）。"
            "当需要上游的某个细节、但不想读全部全文时调用本工具。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索关键词/问题"},
                "top_k": {"type": "integer", "description": "返回条数，默认 4"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="web_search",
        description="联网搜索关键词，返回摘要结果。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="web_fetch",
        description="获取某个 URL 的网页内容。",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要获取的网页 URL"},
            },
            "required": ["url"],
        },
    ),
    Tool(
        name="remember",
        description="保存一条长期记忆（跨会话、跨卡片可用，覆盖同名）。type: user=用户偏好 / feedback=工作反馈 / project=项目目标 / reference=外部资源。scope: project=当前项目 / global=所有项目。",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "记忆名（简短英文标识）"},
                "type": {"type": "string", "description": "记忆类型：user/feedback/project/reference"},
                "scope": {"type": "string", "description": "作用域：project 或 global"},
                "body": {"type": "string", "description": "记忆正文（建议含 **Why:** 与 **How to apply:** 说明原因和适用方式）"},
            },
            "required": ["name", "body"],
        },
    ),
    Tool(
        name="memory_search",
        description="搜索/读取已有长期记忆（关键词匹配 name/type/正文，返回摘要列表）。",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词，空则列出全部"},
            },
            "required": [],
        },
    ),
    Tool(
        name="forget",
        description="删除一条长期记忆（当它已过时/错误/被取代时）。",
        parameters={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "description": "作用域：project 或 global"},
                "name": {"type": "string", "description": "要删除的记忆名"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="run_subagent",
        description="派生子代理完成一个聚焦子任务（如调研一段代码、起草一段内容）。子代理与你使用同一模型和工具，可读写，完成后返回结论。复杂任务请拆成多个子任务依次/并行调用。",
        parameters={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "子任务的明确描述（含上下文，子代理看不到你的其它对话）"},
                "max_rounds": {"type": "integer", "description": "工具调用轮数上限，默认 4"},
            },
            "required": ["task"],
        },
    ),
    Tool(
        name="run_python",
        description=(
            "在工作目录内执行一段 Python 代码并返回 stdout/stderr。用于计算、解析、快速验证、数据加工等；"
            "注意：代码在受控沙箱中运行，无法联网、无法访问工作目录外路径。写文件/删文件等操作请用 write_file 或显式属于敏感命令。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的 Python 代码"},
            },
            "required": ["code"],
        },
    ),
    Tool(
        name="read_image",
        description=(
            "读取工作目录内的图片/二进制文本文件，返回其内容概要（UTF-8 可读部分；若为图片，返回文件大小与是否为图片等元信息，"
            "暂不提供视觉识别）。主要用于判断文件是否存在、类型与大小。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件路径"},
            },
            "required": ["path"],
        },
    ),
]

TOOL_BY_NAME: dict[str, Tool] = {t.name: t for t in TOOLS}


# 免审批的「只读/查询」命令白名单（前缀匹配，严格模式）
_READONLY_COMMANDS = (
    "echo", "type", "cat", "dir", "ls", "where", "whoami", "cd", "ver",
    "git status", "git log", "git diff", "git show", "git branch", "git remote -v",
    "python --version", "python -v", "py --version", "node --version", "npm --version",
    "pip --version", "pip list", "pip show",
    "set", "printenv",
)
# 明显危险的命令前缀（写/删除/安装/推送/关机等）→ 必须审批
_DANGEROUS_COMMANDS = (
    "del ", "erase ", "rm ", "rmdir ", "format ", "mkfs",
    "pip install", "pip uninstall", "npm install", "npm uninstall",
    "git push", "git commit", "git reset", "git clean",
    "shutdown", "reboot", "taskkill", "kill", "reg", "net user", "net stop",
    "curl", "wget", "move ", "copy ", "xcopy", "robocopy", "ren ",
    "start ", "schtasks", "powershell", "cmd /c",
)


def command_needs_approval(command: str) -> bool:
    """判断 shell 命令是否需要人工审批（只读/查询类免审，其余审批）。"""
    cmd = (command or "").strip()
    if not cmd:
        return False
    lower = cmd.lower()
    # 危险命令黑名单优先
    for d in _DANGEROUS_COMMANDS:
        if lower.startswith(d):
            return True
    # 只读/查询白名单免审
    for ok in _READONLY_COMMANDS:
        if lower.startswith(ok):
            return False
    # 其它未知命令一律审批（保守）
    return True


def needs_approval(name: str, args: dict) -> bool:
    """动态审批判定：exec_command 按命令内容判定；run_python 执行任意代码需审批；write_file 免审。"""
    if name == "exec_command":
        return command_needs_approval(args.get("command", ""))
    if name == "run_python":
        # 能执行任意代码 → 需人工审批（防越权/危险操作）
        return True
    # write_file：普通写文件免审（路径越界由 tool_executor._safe_path 拒绝）
    return False


def openai_tools() -> list[dict]:
    """转成 OpenAI 的 tools 参数格式。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            },
        }
        for t in TOOLS
    ]
