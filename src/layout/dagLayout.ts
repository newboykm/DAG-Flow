import ELK, { type ElkNode, type ElkExtendedEdge } from 'elkjs/lib/elk.bundled.js';
import type { DagNode } from '../types';

export interface LayoutNode {
  id: string;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface LayoutEdge {
  id: string;
  source: string;
  target: string;
  points: { x: number; y: number }[];
}

export interface LayoutResult {
  nodes: LayoutNode[];
  edges: LayoutEdge[];
  width: number;
  height: number;
}

const EMPTY_LAYOUT: LayoutResult = { nodes: [], edges: [], width: 0, height: 0 };

/** 用 elkjs（layered, RIGHT）对可见节点做从左到右的 DAG 布局，返回节点左上角坐标与折线边（边为源右缘 → 目标左缘）。 */
export async function layoutGraph(
  nodes: DagNode[],
  sizes: Record<string, { width: number; height: number }>,
): Promise<LayoutResult> {
  if (nodes.length === 0) return EMPTY_LAYOUT;

  const ids = new Set(nodes.map((n) => n.nodeId));
  const elk = new ELK();

  const children: ElkNode[] = nodes.map((n) => {
    const s = sizes[n.nodeId] ?? { width: 224, height: 96 };
    return {
      id: n.nodeId,
      width: s.width,
      height: s.height,
      ports: [
        { id: `${n.nodeId}.out`, width: 4, height: 4 },
        { id: `${n.nodeId}.in`, width: 4, height: 4 },
      ],
    };
  });
  const edges: ElkExtendedEdge[] = nodes.flatMap((n) =>
    n.parentIds
      .filter((p) => ids.has(p))
      .map((p) => ({
        id: `${p}->${n.nodeId}`,
        sources: [`${p}.out`],
        targets: [`${n.nodeId}.in`],
      })),
  );

  const graph: ElkNode = {
    id: 'dag-root',
    layoutOptions: {
      'elk.algorithm': 'layered',
      'elk.direction': 'RIGHT',
      'elk.spacing.nodeNode': '48',
      'elk.layered.spacing.nodeNodeBetweenLayers': '72',
      'elk.edgeRouting': 'ORTHOGONAL',
      'elk.layered.edgeNodeOverlapAvoidance': 'true',
      'elk.padding': '[top=28,left=28,bottom=28,right=28]',
    },
    children,
    edges,
  };

  const result = await elk.layout(graph);

  const layoutNodes: LayoutNode[] = (result.children ?? []).map((c) => ({
    id: c.id,
    x: c.x ?? 0,
    y: c.y ?? 0,
    width: c.width ?? sizes[c.id]?.width ?? 224,
    height: c.height ?? sizes[c.id]?.height ?? 96,
  }));

  // 一键整理规则：最长路径分层 + 同层均匀间隔纵向排列（覆盖 elkjs 原始坐标）
  alignLayers(layoutNodes, nodes);

  const nodeById = new Map(layoutNodes.map((n) => [n.id, n]));
  const portOf = new Map<string, { x: number; y: number }>();
  for (const n of layoutNodes) {
    const out = outPortX(n);
    const mid = n.y + n.height / 2;
    portOf.set(`${n.id}.out`, { x: out, y: mid });
    portOf.set(`${n.id}.in`, { x: n.x, y: mid });
  }

  const layoutEdges: LayoutEdge[] = (result.edges ?? []).map((e) => {
    const source = e.sources[0];
    const target = e.targets[0];
    let points: { x: number; y: number }[];

    if (e.sections && e.sections.length > 0) {
      const all: { x: number; y: number }[] = [];
      e.sections.forEach((sec, i) => {
        // 端口侧：用节点边界的锚点替换 elkjs 的端口坐标（渲染坐标一致）
        if (i === 0) {
          const s = source && portOf.get(source);
          if (s) all.push(s);
        }
        for (const p of sec.bendPoints ?? []) all.push(p);
        const last = sec.endPoint;
        if (all.length === 0 || !samePoint(all[all.length - 1], last)) all.push(last);
      });
      // 目标端口 → 卡片左缘锚点
      if (all.length) {
        const t = target && portOf.get(target);
        if (t) all[all.length - 1] = t;
      }
      points = dedupePoints(all);
    } else {
      const s = source && portOf.get(source);
      const t = target && portOf.get(target);
      if (s && t) points = [s, t];
      else points = fallbackPoints(nodeById.get(sourceNodeId(source)), nodeById.get(targetNodeId(target)));
    }

    return { id: e.id, source: sourceNodeId(source), target: targetNodeId(target), points };
  });

  // 让共享同一段中间通道的边在相邻层之间错开一点纵向间距，避免多条线重叠交汇
  const byTarget = groupEdgesByTarget(layoutEdges);
  for (const edges of byTarget.values()) {
    nudgeSharedRuns(edges);
  }

  return {
    nodes: layoutNodes,
    edges: layoutEdges,
    width: boundsOf(layoutNodes).w,
    height: boundsOf(layoutNodes).h,
  };
}

function boundsOf(nodes: LayoutNode[]): { w: number; h: number } {
  let w = 0;
  let h = 0;
  for (const n of nodes) {
    w = Math.max(w, n.x + n.width);
    h = Math.max(h, n.y + n.height);
  }
  return { w: w + 28, h: h + 28 };
}

function dedupePoints(points: { x: number; y: number }[]): { x: number; y: number }[] {
  return points.filter((p, i) => i === 0 || p.x !== points[i - 1].x || p.y !== points[i - 1].y);
}

/**
 * 一键整理：最长路径层号 + 父子水平对齐 + 兄弟组居中 + 同层避让。
 * - 层号 = 节点到根的最长路径深度（join 跨层时取最深层号）。
 * - 每个节点纵坐标对齐其父节点纵坐标（中心对齐）；多父节点取各父节点纵坐标中点。
 * - 同一父节点下的多个子节点（兄弟）以父节点 y 为中心上下均匀分布。
 * - 同一层内不同父节点来的卡片若重叠，向下推开并保持最小间距。
 */
function alignLayers(layoutNodes: LayoutNode[], dagNodes: DagNode[]): void {
  if (layoutNodes.length === 0) return;

  const byId = new Map(layoutNodes.map((n) => [n.id, n]));
  const dagById = new Map(dagNodes.map((n) => [n.nodeId, n]));

  // 最长路径层号（自顶向下 DP，只统计可见父节点）
  const layer = new Map<string, number>();
  const visit = (id: string): number => {
    if (layer.has(id)) return layer.get(id)!;
    const dag = dagById.get(id);
    const parents = (dag?.parentIds ?? []).filter((p) => byId.has(p));
    let maxParent = 0;
    for (const p of parents) maxParent = Math.max(maxParent, visit(p) + 1);
    layer.set(id, maxParent);
    return maxParent;
  };
  for (const n of layoutNodes) visit(n.id);

  const layers = new Map<number, LayoutNode[]>();
  for (const n of layoutNodes) {
    const l = layer.get(n.id) ?? 0;
    const arr = layers.get(l);
    if (arr) arr.push(n);
    else layers.set(l, [n]);
  }
  // 层内先用 elkjs 原始 y 排序，作为兄弟/同层节点的稳定顺序
  for (const arr of layers.values()) arr.sort((a, b) => a.y - b.y);

  const maxLayer = Math.max(...Array.from(layers.keys()));
  const H_GAP = 28; // 卡片之间垂直最小间距
  const W_GAP = 64; // 层与层之间的水平间距
  const PAD = 28; // 画布内边距

  // 每列 x 位置：从左到右累加（层宽 = 该层最大卡片宽）
  let x = 0;
  const layerX = new Map<number, number>();
  for (let l = 0; l <= maxLayer; l++) {
    const arr = layers.get(l);
    if (!arr) continue;
    layerX.set(l, x);
    x += Math.max(...arr.map((n) => n.width), 1) + W_GAP;
  }

  // 按层推进，计算每个节点中心 y：父对齐 + 兄弟组居中 + 层内扫掠避让
  const centerY = new Map<string, number>();
  const sortedLayers = Array.from(layers.keys()).sort((a, b) => a - b);
  for (const l of sortedLayers) {
    const arr = layers.get(l)!;

    // 按「可见父节点集合」分组：同一父（组）的兄弟成一组；join 节点单独成组
    const groupOrder: string[] = [];
    const groups = new Map<string, LayoutNode[]>();
    for (const n of arr) {
      const dag = dagById.get(n.id);
      const parentIds = (dag?.parentIds ?? []).filter((p) => byId.has(p));
      const key = parentIds.length ? [...parentIds].sort().join('|') : '__root__';
      if (!groups.has(key)) {
        groups.set(key, []);
        groupOrder.push(key);
      }
      groups.get(key)!.push(n);
    }

    // 每组期望中心：无父（根）为 0；有父为各父节点中心 y 的平均（多父即中点）
    const preferred = new Map<string, number>();
    for (const [key] of groups) {
      if (key === '__root__') {
        preferred.set(key, 0);
        continue;
      }
      let sum = 0;
      let cnt = 0;
      for (const p of key.split('|')) {
        const cy = centerY.get(p);
        if (cy != null) {
          sum += cy;
          cnt += 1;
        }
      }
      preferred.set(key, cnt ? sum / cnt : 0);
    }

    // 组按期望中心排序，逐组放置；组间重叠则向下推开
    const sortedGroups = groupOrder
      .map((key) => ({ key, group: groups.get(key)! }))
      .sort((a, b) => (preferred.get(a.key) ?? 0) - (preferred.get(b.key) ?? 0));

    let prevBottom = -Infinity;
    for (const { key, group } of sortedGroups) {
      const blockHeight =
        group.reduce((s, n) => s + n.height, 0) + (group.length - 1) * H_GAP;
      const desiredTop = (preferred.get(key) ?? 0) - blockHeight / 2;
      const top = Math.max(desiredTop, prevBottom + H_GAP);

      // 组内：兄弟以组中心均匀分布（自上而下依次落位）
      let cursor = top;
      for (const child of group) {
        centerY.set(child.id, cursor + child.height / 2);
        cursor += child.height + H_GAP;
      }
      prevBottom = top + blockHeight;
    }
  }

  // 落位：x 按层、y 按中心换算成左上角，再整体平移到非负区域
  let minX = Infinity;
  let minY = Infinity;
  for (const n of layoutNodes) {
    n.x = layerX.get(layer.get(n.id) ?? 0) ?? n.x;
    const cy = centerY.get(n.id) ?? 0;
    n.y = cy - n.height / 2;
    minX = Math.min(minX, n.x);
    minY = Math.min(minY, n.y);
  }
  const shiftX = PAD - minX;
  const shiftY = PAD - minY;
  for (const n of layoutNodes) {
    n.x += shiftX;
    n.y += shiftY;
  }
}

function samePoint(a: { x: number; y: number }, b: { x: number; y: number }): boolean {
  return Math.abs(a.x - b.x) < 1e-6 && Math.abs(a.y - b.y) < 1e-6;
}

/** 源端口锚点：卡片右缘中点（在 RIGHT 布局下，出边从右缘出发）。 */
function outPortX(n: LayoutNode): number {
  return n.x + n.width;
}

/** 端口 id（`nodeId.out` / `nodeId.in`）→ 节点 id。 */
function sourceNodeId(port: string): string {
  return port.replace(/\.(out|in)$/, '');
}

/** 端口 id → 节点 id（与 sourceNodeId 同规则，保留语义化命名）。 */
function targetNodeId(port: string): string {
  return sourceNodeId(port);
}

/** 按目标节点分组，后续在「汇入同一点」的边之间做通道错位。 */
function groupEdgesByTarget(edges: LayoutEdge[]): Map<string, LayoutEdge[]> {
  const m = new Map<string, LayoutEdge[]>();
  for (const e of edges) {
    const list = m.get(e.target);
    if (list) list.push(e);
    else m.set(e.target, [e]);
  }
  return m;
}

/**
 * 对汇入同一目标的多条边，若它们共享同一段横向中间通道（相邻两个 y 相等的点），
 * 则按顺序纵向错开几像素，避免多条线完全重叠交汇。
 */
function nudgeSharedRuns(edges: LayoutEdge[]): void {
  if (edges.length < 2) return;
  for (let i = 0; i < edges.length; i++) {
    for (let j = i + 1; j < edges.length; j++) {
      const a = edges[i];
      const b = edges[j];
      if (a.points.length < 3 || b.points.length < 3) continue;
      const aMid = a.points.slice(1, -1);
      const bMid = b.points.slice(1, -1);
      const shared = aMid.some((pa) =>
        bMid.some((pb) => Math.abs(pa.y - pb.y) < 1e-6 && Math.abs(pa.x - pb.x) < 60),
      );
      if (!shared) continue;
      const already = edges
        .slice(0, i)
        .some((e) => e.points.slice(1, -1).some((pe) => Math.abs(pe.y - bMid[0].y) < 24));
      const offset = already ? -10 : 10;
      b.points = b.points.map((p) => ({ x: p.x, y: p.y + offset }));
    }
  }
}

function fallbackPoints(
  source?: LayoutNode,
  target?: LayoutNode,
): { x: number; y: number }[] {
  if (!source || !target) return [];
  const dir = Math.atan2(
    target.y + target.height / 2 - (source.y + source.height / 2),
    target.x + target.width / 2 - (source.x + source.width / 2),
  );
  const s = closestBoundaryPoint(source, dir, Math.PI / 2);
  const t = closestBoundaryPoint(target, dir, -Math.PI / 2);
  return [s, t];
}

/** 从节点中心沿给定方向求卡片边界交点（兜底：elkjs 未给 bendPoints / sections 时使用）。 */
function closestBoundaryPoint(
  n: LayoutNode,
  dir: number,
  bias: number,
): { x: number; y: number } {
  const cx = n.x + n.width / 2;
  const cy = n.y + n.height / 2;
  const theta = dir + bias;
  const dx = Math.cos(theta);
  const dy = Math.sin(theta);
  let tx = Infinity;
  if (dx !== 0) tx = Math.min((n.width / 2) / Math.abs(dx), (n.height / 2) / Math.abs(dy));
  if (!isFinite(tx)) tx = Math.min(n.width, n.height) / 2;
  return { x: cx + dx * tx, y: cy + dy * tx };
}
