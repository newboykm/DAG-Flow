"""动态上下文注入（对齐 dsh 的 runtime context 思想）。

dsh 通过 system-prompt context 机制把运行时事实（当前时间、工作目录、git 状态、
环境等）在每次组装时注入模型提示，让 agent 知道"现在是什么时候、在哪、工作区状态"，
对时效性任务与 git/文件操作尤为重要。
"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime


def dynamic_context(workspace: str | None = None) -> str:
    """生成一段动态上下文文本（语言无关，中文）。"""
    now = datetime.now()
    parts: list[str] = [
        f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}（当地时间，周{'一二三四五六日'[now.weekday()]}）",
        f"工作目录：{workspace or os.getcwd()}",
    ]

    # git 状态（尽力而为，无 git 仓库则跳过）
    try:
        if workspace and os.path.isdir(os.path.join(workspace, ".git")):
            branch = subprocess.run(
                ["git", "-C", workspace, "branch", "--show-current"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            status = subprocess.run(
                ["git", "-C", workspace, "status", "--porcelain"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            dirty = len([l for l in status.splitlines() if l]) if status else 0
            if branch:
                parts.append(f"git 分支：{branch}，工作区未提交变更 {dirty} 处" if dirty else f"git 分支：{branch}，工作区干净")
    except Exception:
        pass

    # 平台
    plat = "Windows" if os.name == "nt" else ("macOS" if sys_platform_is_darwin() else "Linux")
    parts.append(f"运行平台：{plat}")

    return "\n".join(parts)


def sys_platform_is_darwin() -> bool:
    import sys
    return sys.platform == "darwin"
