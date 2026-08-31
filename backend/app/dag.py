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


def running_relatives(node_ids: set[str], status_of: dict[str, str], parents: dict[str, set[str]]) -> list[str]:
    """检测父子互斥：遍历给定节点集合的祖先与后代，返回其中正在运行的节点 id。

    用于"父子不能同时执行"：若某个节点与它已 running 的父/子同框执行，应阻止/提醒。

    参数：
      node_ids  - 候选节点集合（可能是单个节点或一个子树）
      status_of - nodeId -> status
      parents   - nodeId -> 父节点 id 集合（用于向上/向下遍历）
    返回：与候选有亲缘关系且正在运行(running)的节点 id 列表。
    """
    def ancestors(nid: str, seen: set[str]) -> set[str]:
        out: set[str] = set()
        stack = [p for p in parents.get(nid, ())]
        while stack:
            p = stack.pop()
            if p in seen:
                continue
            seen.add(p)
            out.add(p)
            stack.extend(parents.get(p, ()))
        return out

    def descendants(nid: str, all_nodes: set[str]) -> set[str]:
        out: set[str] = set()
        stack = [n for n in all_nodes if nid in parents.get(n, ())]
        while stack:
            c = stack.pop()
            if c in out:
                continue
            out.add(c)
            stack.extend(n for n in all_nodes if c in parents.get(n, ()))
        return out

    all_nodes = set(status_of.keys())
    running: list[str] = []
    for nid in node_ids:
        # 祖先（父链）+ 后代（子链）
        chain = ancestors(nid, set()) | descendants(nid, all_nodes) | {nid}
        for c in chain:
            if c != nid and status_of.get(c) == "running":
                running.append(c)
    return sorted(set(running))
