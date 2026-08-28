"""DAG 拓扑工具：环检测、最长路径层号、初始状态判定（对照需求 §3.4 / §4.2.5 / §7）。"""
from typing import Iterable

TERMINAL = {"done", "failed", "cancelled"}


def would_create_cycle(
    nodes: dict[str, set[str]],
    source_id: str,
    new_parent_ids: Iterable[str],
) -> bool:
    """判断给 source_id 增加新的父节点后是否成环。

    新边方向是 parent -> source。成环当且仅当从 source 沿现有出边走，
    能回到某个新父节点本身。
    """
    new_parents = set(new_parent_ids)
    if source_id in new_parents:
        return True
    stack = [source_id]
    seen = {source_id}
    while stack:
        cur = stack.pop()
        for nxt in nodes.get(cur, ()):
            if nxt in new_parents:
                return True
            if nxt not in seen:
                seen.add(nxt)
                stack.append(nxt)
    return False


def longest_path_layers(parents: dict[str, set[str]]) -> dict[str, int]:
    """每个节点到根的最长路径深度（join 取最深层号）。"""
    layer: dict[str, int] = {}
    memo: dict[str, int] = {}

    def visit(nid: str) -> int:
        if nid in memo:
            return memo[nid]
        best = 0
        for p in parents.get(nid, ()):
            best = max(best, visit(p) + 1)
        memo[nid] = best
        return best

    for nid in parents:
        layer[nid] = visit(nid)
    return layer


def compute_initial_status(status_of: dict[str, str], parent_ids: list[str]) -> str:
    """追加节点时的初始状态判定（§4.2.5 + §7）。

    - 所有父 done → ready
    - 任一父 failed/cancelled → blocked
    - 否则 pending
    """
    sts = [status_of.get(p) for p in parent_ids if p in status_of]
    if sts and all(s == "done" for s in sts):
        return "ready"
    if any(s in ("failed", "cancelled") for s in sts):
        return "blocked"
    return "pending"


def is_terminal(status: str) -> bool:
    return status in TERMINAL
