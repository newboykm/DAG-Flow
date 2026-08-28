import type { DagNode, NodeStatus } from '../types';

export const TERMINAL: NodeStatus[] = ['done', 'failed', 'cancelled'];

export function isTerminal(s: NodeStatus): boolean {
  return TERMINAL.includes(s);
}

/** parentId -> 子节点 id 列表 */
export function childrenMap(nodes: Record<string, DagNode>): Record<string, string[]> {
  const m: Record<string, string[]> = {};
  for (const n of Object.values(nodes)) {
    for (const p of n.parentIds) {
      (m[p] ??= []).push(n.nodeId);
    }
  }
  return m;
}

/** 某节点的全部下游节点数量（用于「折叠子树」占位） */

/** 排除被子树折叠隐藏的节点，得到参与布局的可见节点 id 列表 */
export function visibleNodeIds(nodes: Record<string, DagNode>): string[] {
  const hidden = new Set<string>();
  const children = childrenMap(nodes);
  for (const n of Object.values(nodes)) {
    if (n.subtreeCollapsed) {
      const stack = [...(children[n.nodeId] ?? [])];
      while (stack.length) {
        const cur = stack.pop()!;
        hidden.add(cur);
        stack.push(...(children[cur] ?? []));
      }
    }
  }
  return Object.keys(nodes).filter((id) => !hidden.has(id));
}

/** 追加节点时的初始状态判定（§4.2.5 + §7 状态机） */
export function computeInitialStatus(
  nodes: Record<string, DagNode>,
  parentIds: string[],
): NodeStatus {
  const parents = parentIds.map((p) => nodes[p]).filter(Boolean);
  if (parents.length > 0 && parents.every((p) => p.status === 'done')) return 'ready';
  if (parents.some((p) => p.status === 'failed' || p.status === 'cancelled')) return 'blocked';
  return 'pending';
}
