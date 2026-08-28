import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useGraphStore } from '../store/useGraphStore';
import { layoutGraph, type LayoutEdge, type LayoutNode, type LayoutResult } from '../layout/dagLayout';
import { childrenMap, visibleNodeIds } from '../graph/graphUtils';
import type { DagNode } from '../types';
import TaskNodeCard from './TaskNodeCard';
import RootNode from './RootNode';

const ROOT_SIZE = { width: 132, height: 44 };
const COLLAPSED_SIZE = { width: 232, height: 150 };
const EXPANDED_SIZE = { width: 364, height: 344 };

interface Viewport {
  x: number;
  y: number;
  k: number;
}

function EdgePath({
  edge,
  dim,
  layoutById,
}: {
  edge: LayoutEdge;
  dim: boolean;
  layoutById: Map<string, LayoutNode>;
}) {
  if (edge.points.length === 0) return null;
  const d = directCurvePath(edge, layoutById);
  return (
    <path
      d={d}
      className={dim ? 'dag-edge dimmed' : 'dag-edge'}
      markerEnd="url(#dag-arrow)"
      markerStart="url(#dag-dot)"
    />
  );
}

/**
 * 直接连线：固定从父卡片右缘中点出发，到子卡片左缘中点结束，
 * 中间用一条柔和的贝塞尔曲线连接（不做折线）。
 */
function directCurvePath(edge: LayoutEdge, layoutById: Map<string, LayoutNode>): string {
  const src = layoutById.get(edge.source);
  const tgt = layoutById.get(edge.target);
  if (!src || !tgt) return '';

  // 固定锚点：父右缘中点、子左缘中点（含 dragOffset，拖动时实时跟随）
  const s = { x: src.x + src.width, y: src.y + src.height / 2 };
  const t = { x: tgt.x, y: tgt.y + tgt.height / 2 };

  // 曲线控制点：沿水平方向各延伸一段，让曲线柔和弯曲
  const c1 = {
    x: s.x + (t.x - s.x) * 0.5,
    y: s.y,
  };
  const c2 = {
    x: t.x - (t.x - s.x) * 0.5,
    y: t.y,
  };
  return `M ${s.x} ${s.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${t.x} ${t.y}`;
}

export default function FlowCanvas() {
  const nodes = useGraphStore((s) => s.nodes);
  const collapsed = useGraphStore((s) => s.collapsed);
  const filterStatus = useGraphStore((s) => s.filterStatus);
  const filterKeyword = useGraphStore((s) => s.filterKeyword);
  const anchorIds = useGraphStore((s) => s.anchorIds);
  const sessionId = useGraphStore((s) => s.sessionId);
  const fitNonce = useGraphStore((s) => s.fitNonce);
  const selectingSource = useGraphStore((s) => s.selectingSource);
  const sourceIds = useGraphStore((s) => s.sourceIds);
  const focusRequest = useGraphStore((s) => s.focusRequest);
  const openWindows = useGraphStore((s) => s.openWindows);
  const windowZ = useGraphStore((s) => s.windowZ);

  // 正在拖动改大小的窗口 id：拖动中冻结连线计算，松手再算
  const [resizingNodeId, setResizingNodeId] = useState<string | null>(null);
  const resizeDragRef = useRef<{ nodeId: string; sx: number; sy: number; ow: number; oh: number } | null>(null);
  const titleDragRef = useRef<{ nodeId: string; sx: number; sy: number; ox: number; oy: number } | null>(null);

  const containerRef = useRef<HTMLDivElement>(null);
  const [view, setView] = useState<Viewport>({ x: 40, y: 40, k: 1 });
  const viewRef = useRef(view);
  viewRef.current = view;
  const [containerSize, setContainerSize] = useState({ w: 0, h: 0 });
  const dragRef = useRef<{ sx: number; sy: number; vx: number; vy: number } | null>(null);
  const movedRef = useRef(false);

  // 容器尺寸测量（用于视口裁剪）
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const update = () => setContainerSize({ w: el.clientWidth, h: el.clientHeight });
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const visible = useMemo(() => {
    return visibleNodeIds(nodes)
      .map((id) => nodes[id])
      .filter(Boolean);
  }, [nodes]);

  const children = useMemo(() => childrenMap(nodes), [nodes]);

  const sizes = useMemo(() => {
    const m: Record<string, { width: number; height: number }> = {};
    for (const n of visible) {
      if (n.kind === 'root') {
        m[n.nodeId] = ROOT_SIZE;
      } else if (n.customSize && !(collapsed[n.nodeId] ?? true)) {
        // 展开态：用户自定义尺寸优先（永久保留）
        m[n.nodeId] = { ...n.customSize };
      } else {
        const hasChildren = (children[n.nodeId] ?? []).length > 0;
        const CO = { ...COLLAPSED_SIZE };
        const EX = { ...EXPANDED_SIZE };
        m[n.nodeId] =
          (collapsed[n.nodeId] ?? true)
            ? CO // 折叠态：所有卡片统一大尺寸（含三个按钮，放得下）
            : hasChildren
              ? { width: 340, height: 260 }
              : EX;
      }
    }
    return m;
  }, [visible, collapsed, children]);

  const [layout, setLayout] = useState<LayoutResult>({ nodes: [], edges: [], width: 0, height: 0 });

  // 拓扑指纹：只在「节点增删 / 父子关系变化」时触发重排；尺寸（customSize）变化不触发
  const topoKey = visible
    .map((n) => `${n.nodeId}:${(n.parentIds ?? []).join(',')}`)
    .sort()
    .join('|');

  // 折叠态指纹：折叠/展开切换会改变卡片尺寸，需要重算布局更新连线端点
  const collapsedKey = Object.keys(collapsed)
    .filter((id) => collapsed[id])
    .sort()
    .join('|');

  // elkjs 是异步布局：拓扑或折叠态变化后重新计算（customSize 变化不重排）
  useEffect(() => {
    let cancelled = false;
    layoutGraph(visible, sizes).then((l) => {
      if (!cancelled) setLayout(l);
    });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [topoKey, collapsedKey]);

  // 布局位置 + 手动拖动 offset + 实际尺寸（customSize 覆盖），用于连线端点实时吸附卡片
  const offsetLayoutById = useMemo(() => {
    const m = new Map<string, LayoutNode>();
    for (const ln of layout.nodes) {
      const node = nodes[ln.id];
      const dx = node?.dragOffset?.dx ?? 0;
      const dy = node?.dragOffset?.dy ?? 0;
      const isExpanded = !(collapsed[ln.id] ?? true);
      const cw = node?.customSize && isExpanded ? node.customSize.width : ln.width;
      const ch = node?.customSize && isExpanded ? node.customSize.height : ln.height;
      m.set(ln.id, { ...ln, x: ln.x + dx, y: ln.y + dy, width: cw, height: ch });
    }
    return m;
  }, [layout.nodes, nodes, collapsed]);

  const fit = useCallback(() => {
    const el = containerRef.current;
    if (!el || !layout.width || !layout.height) return;
    const cw = el.clientWidth;
    const ch = el.clientHeight;
    const pad = 80;
    const k = Math.min((cw - pad * 2) / layout.width, (ch - pad * 2) / layout.height, 1.2);
    const vk = Math.max(0.25, Math.min(k, 1.6));
    setView({ x: (cw - layout.width * vk) / 2, y: (ch - layout.height * vk) / 2, k: vk });
  }, [layout.width, layout.height]);

  // 会话切换 / 请求重置时 fit（布局就绪后才执行）
  const lastFitKey = useRef<string>('');
  useEffect(() => {
    if (!layout.width || !layout.height) return;
    const key = `${sessionId}:${fitNonce}`;
    if (lastFitKey.current !== key) {
      lastFitKey.current = key;
      fit();
    }
  }, [sessionId, fitNonce, fit, layout.width, layout.height]);

  // 双击卡片：zoomIn=放大 2 倍并居中该卡片；否则还原到之前的 view
  const savedViewRef = useRef<Viewport | null>(null);
  useEffect(() => {
    if (!focusRequest) return;
    const ln = offsetLayoutById.get(focusRequest.nodeId);
    const el = containerRef.current;
    if (!el) return;

    if (focusRequest.zoomIn) {
      // 保存当前 view 以便还原
      savedViewRef.current = viewRef.current;
      if (ln) {
        const targetK = 2;
        const cx = el.clientWidth / 2;
        const cy = el.clientHeight / 2;
        const nx = ln.x + ln.width / 2;
        const ny = ln.y + ln.height / 2;
        setView({ k: targetK, x: cx - nx * targetK, y: cy - ny * targetK });
      }
    } else {
      if (savedViewRef.current) {
        setView(savedViewRef.current);
        savedViewRef.current = null;
      } else {
        fit();
      }
    }
  }, [focusRequest, offsetLayoutById]);


  // 缩放
  const zoomAt = useCallback((clientX: number, clientY: number, factor: number) => {
    const el = containerRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const mx = clientX - rect.left;
    const my = clientY - rect.top;
    setView((v) => {
      const k = Math.min(2.5, Math.max(0.2, v.k * factor));
      const gx = (mx - v.x) / v.k;
      const gy = (my - v.y) / v.k;
      return { k, x: mx - gx * k, y: my - gy * k };
    });
  }, []);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.12 : 1 / 1.12);
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [zoomAt]);

  // 视口裁剪：大图只渲染可视区域内的节点/边
  const rendered = useMemo(() => {
    const cull = visible.length > 80;
    if (!cull) return { nodes: layout.nodes, edges: layout.edges };
    const margin = 240;
    const gx = -view.x / view.k - margin;
    const gy = -view.y / view.k - margin;
    const gw = (containerSize.w || 1200) / view.k + margin * 2;
    const gh = (containerSize.h || 800) / view.k + margin * 2;
    const nodeSet = new Set(
      layout.nodes
        .filter((n) => n.x > gx && n.x < gx + gw && n.y > gy && n.y < gy + gh)
        .map((n) => n.id),
    );
    return {
      nodes: layout.nodes.filter((n) => nodeSet.has(n.id)),
      edges: layout.edges.filter((e) => nodeSet.has(e.source) && nodeSet.has(e.target)),
    };
  }, [layout, visible.length, view, containerSize]);

  const childrenCount = useMemo(() => {
    const m: Record<string, number> = {};
    for (const id of Object.keys(nodes)) m[id] = children[id]?.length ?? 0;
    return m;
  }, [nodes, children]);

  const isDimmed = useCallback(
    (n: DagNode) => {
      const byStatus = filterStatus !== 'all' && n.status !== filterStatus;
      const kw = filterKeyword.trim().toLowerCase();
      const byKw = kw.length > 0 && !n.title.toLowerCase().includes(kw);
      return byStatus || byKw;
    },
    [filterStatus, filterKeyword],
  );

  const onPointerDown = (e: React.PointerEvent) => {
    const target = e.target as HTMLElement;
    if (target.closest('.dag-node')) return;
    if (e.button !== 0 && e.button !== 1) return;
    dragRef.current = { sx: e.clientX, sy: e.clientY, vx: view.x, vy: view.y };
    movedRef.current = false;
    containerRef.current?.setPointerCapture?.(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.sx;
    const dy = e.clientY - d.sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) movedRef.current = true;
    if (movedRef.current) setView((v) => ({ ...v, x: d.vx + dx, y: d.vy + dy }));
  };

  const onPointerUp = () => {
    dragRef.current = null;
  };

  const onCanvasClick = (e: React.MouseEvent) => {
    const target = e.target as HTMLElement;
    if (target.closest('.dag-node')) return;
    if (movedRef.current) return;
    useGraphStore.getState().clearAnchors();
  };

  // 窗口层连线：每个打开窗口与其上游/下游节点的边（画布容器坐标）
  const windowEdgesCacheRef = useRef<{ sx: number; sy: number; tx: number; ty: number; id: string }[]>([]);
  const windowEdges = useMemo(() => {
    // 拖动改大小过程中冻结连线（返回上次缓存），松手后再重算
    if (resizingNodeId) return windowEdgesCacheRef.current;
    const edges: { sx: number; sy: number; tx: number; ty: number; id: string }[] = [];
    const toScreen = (nodeId: string) => {
      // 若目标是已开窗节点，用窗口实际位置；否则用 DAG 布局位置换算到屏幕
      const wrect = openWindows[nodeId];
      if (wrect) {
        return {
          x: wrect.x,
          y: wrect.y,
          w: wrect.width,
          h: wrect.height,
        };
      }
      const ln = offsetLayoutById.get(nodeId);
      if (!ln) return null;
      return {
        x: view.x + ln.x * view.k,
        y: view.y + ln.y * view.k,
        w: ln.width * view.k,
        h: ln.height * view.k,
      };
    };
    for (const nodeId of Object.keys(openWindows)) {
      const rect = openWindows[nodeId];
      if (!rect) continue;
      const node = nodes[nodeId];
      if (!node) continue;
      const winLeft = { x: rect.x, y: rect.y + rect.height / 2 };
      const winRight = { x: rect.x + rect.width, y: rect.y + rect.height / 2 };
      for (const pid of node.parentIds || []) {
        const p = toScreen(pid);
        if (p) edges.push({ sx: p.x + p.w, sy: p.y + p.h / 2, tx: winLeft.x, ty: winLeft.y, id: `${pid}->${nodeId}` });
      }
      for (const cid of children[nodeId] ?? []) {
        const c = toScreen(cid);
        if (c) edges.push({ sx: winRight.x, sy: winRight.y, tx: c.x, ty: c.y + c.h / 2, id: `${nodeId}->${cid}` });
      }
    }
    windowEdgesCacheRef.current = edges;
    return edges;
  }, [resizingNodeId, openWindows, nodes, children, offsetLayoutById, view]);

  return (
    <div
      className="canvas"
      ref={containerRef}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerLeave={onPointerUp}
      onClick={onCanvasClick}
    >
      <div
        className="viewport"
        style={{
          transform: `translate(${view.x}px, ${view.y}px) scale(${view.k})`,
          width: layout.width,
          height: layout.height,
        }}
      >
        <svg className="edge-layer" width={layout.width} height={layout.height}>
          <defs>
            <marker
              id="dag-arrow"
              viewBox="0 0 12 12"
              refX="10"
              refY="6"
              markerWidth="6.5"
              markerHeight="6.5"
              markerUnits="strokeWidth"
              orient="auto-start-reverse"
            >
              <path d="M 0 0.6 L 12 6 L 0 11.4 z" fill="#94a3b8" />
            </marker>
            <marker
              id="dag-dot"
              viewBox="0 0 12 12"
              refX="6"
              refY="6"
              markerWidth="4.5"
              markerHeight="4.5"
              markerUnits="strokeWidth"
              orient="auto"
            >
              <circle cx="6" cy="6" r="4" fill="#94a3b8" />
            </marker>
          </defs>
          {rendered.edges
            .filter((e) => !openWindows[e.source] && !openWindows[e.target])
            .map((e) => (
              <EdgePath
                key={e.id}
                edge={e}
                dim={isDimmed(nodes[e.source]) || isDimmed(nodes[e.target])}
                layoutById={offsetLayoutById}
              />
            ))}
        </svg>

        {rendered.nodes.map((ln) => {
          const node = nodes[ln.id];
          if (!node || openWindows[node.nodeId]) return null; // 已开窗的节点不在 DAG 里重复显示
          const dim = isDimmed(node);
          const isAnchor = anchorIds.includes(node.nodeId);
          const sourceSelected = selectingSource && sourceIds.includes(node.nodeId);
          const dx = node.dragOffset?.dx ?? 0;
          const dy = node.dragOffset?.dy ?? 0;
          const zoomed = focusRequest?.zoomIn === true && focusRequest.nodeId === node.nodeId;
          if (node.kind === 'root') {
            return (
              <RootNode
                key={node.nodeId}
                node={node}
                x={ln.x + dx}
                y={ln.y + dy}
                width={ln.width}
                height={ln.height}
                isAnchor={isAnchor}
                dim={dim}
                sourceSelected={sourceSelected}
              />
            );
          }
          return (
            <TaskNodeCard
              key={node.nodeId}
              node={node}
              x={ln.x + dx}
              y={ln.y + dy}
              collapsed={collapsed[node.nodeId] ?? true}
              width={
                collapsed[node.nodeId] ?? true
                  ? COLLAPSED_SIZE.width
                  : node.customSize
                    ? node.customSize.width
                    : ln.width
              }
              height={
                collapsed[node.nodeId] ?? true
                  ? COLLAPSED_SIZE.height
                  : node.customSize
                    ? node.customSize.height
                    : ln.height
              }
              isAnchor={isAnchor}
              dim={dim}
              sourceSelected={sourceSelected}
              zoomed={zoomed}
              zoomComp={zoomed ? 1 / view.k : undefined}
              childrenCount={childrenCount[node.nodeId] ?? 0}
            />
          );
        })}
      </div>

      {/* 窗口层连线（与上下游节点的边） */}
      {windowEdges.length > 0 ? (
        <svg className="window-edge-layer" style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none', zIndex: 15 }}>
          <defs>
            <marker id="win-arrow" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="6.5" markerHeight="6.5" markerUnits="strokeWidth" orient="auto-start-reverse">
              <path d="M 0 0.6 L 12 6 L 0 11.4 z" fill="#94a3b8" />
            </marker>
          </defs>
          {windowEdges.map((e) => {
            const mx = (e.sx + e.tx) / 2;
            return (
              <path
                key={e.id}
                d={`M ${e.sx} ${e.sy} C ${mx} ${e.sy}, ${mx} ${e.ty}, ${e.tx} ${e.ty}`}
                fill="none"
                stroke="#94a3b8"
                strokeWidth="1.5"
                markerEnd="url(#win-arrow)"
              />
            );
          })}
        </svg>
      ) : null}

      {/* 多窗口层：打开的卡片窗口（可拖标题移动、右下角改大小） */}
      {Object.entries(openWindows).map(([nodeId, rect]) => {
        const node = nodes[nodeId];
        if (!node || !rect) return null;
        return (
          <div
            key={nodeId}
            className="card-window"
            style={{
              left: rect.x,
              top: rect.y,
              width: rect.width,
              height: rect.height,
              zIndex: 20 + (windowZ[nodeId] ?? 0),
            }}
            onPointerDown={(e) => {
              e.stopPropagation();
              useGraphStore.getState().raiseWindow(nodeId);
            }}
            onPointerMove={(e) => e.stopPropagation()}
            onPointerUp={(e) => e.stopPropagation()}
            onWheel={(e) => e.stopPropagation()}
            onClick={(e) => e.stopPropagation()}
          >
            <div
              className="card-window-title"
              onPointerDown={(e) => {
                e.stopPropagation();
                const el = e.currentTarget as HTMLElement;
                el.setPointerCapture?.(e.pointerId);
                titleDragRef.current = { nodeId, sx: e.clientX, sy: e.clientY, ox: rect.x, oy: rect.y };
              }}
              onPointerMove={(e) => {
                const d = titleDragRef.current;
                if (!d || d.nodeId !== nodeId) return;
                e.stopPropagation();
                useGraphStore.getState().moveWindow(nodeId, d.ox + (e.clientX - d.sx), d.oy + (e.clientY - d.sy));
              }}
              onPointerUp={(e) => {
                if (titleDragRef.current?.nodeId !== nodeId) return;
                e.stopPropagation();
                titleDragRef.current = null;
              }}
              onPointerCancel={(e) => {
                if (titleDragRef.current?.nodeId !== nodeId) return;
                e.stopPropagation();
                titleDragRef.current = null;
              }}
            >
              <span className="card-window-title-text">{node.title}</span>
              <button
                className="card-window-close"
                onMouseDown={(e) => {
                  e.stopPropagation();
                  useGraphStore.getState().toggleWindow(nodeId, rect);
                }}
              >
                ✕
              </button>
            </div>
            <div className="card-window-body">
              <TaskNodeCard
                node={node}
                x={0}
                y={0}
                width={rect.width}
                height={rect.height - 28}
                collapsed={false}
                isAnchor={false}
                dim={false}
                sourceSelected={false}
                zoomed={false}
                childrenCount={childrenCount[nodeId] ?? 0}
                inWindow
              />
            </div>
            <div
              className="card-window-resize"
              onPointerDown={(e) => {
                e.stopPropagation();
                const el = e.currentTarget as HTMLElement;
                el.setPointerCapture?.(e.pointerId);
                resizeDragRef.current = {
                  nodeId,
                  sx: e.clientX,
                  sy: e.clientY,
                  ow: rect.width,
                  oh: rect.height,
                };
                setResizingNodeId(nodeId);
              }}
              onPointerMove={(e) => {
                const d = resizeDragRef.current;
                if (!d || d.nodeId !== nodeId) return;
                e.stopPropagation();
                useGraphStore.getState().resizeWindow(
                  nodeId,
                  d.ow + (e.clientX - d.sx),
                  d.oh + (e.clientY - d.sy),
                );
              }}
              onPointerUp={(e) => {
                if (resizeDragRef.current?.nodeId !== nodeId) return;
                e.stopPropagation();
                resizeDragRef.current = null;
                setResizingNodeId(null);
              }}
              onPointerCancel={(e) => {
                if (resizeDragRef.current?.nodeId !== nodeId) return;
                e.stopPropagation();
                resizeDragRef.current = null;
                setResizingNodeId(null);
              }}
            >
              ◢
            </div>
          </div>
        );
      })}
    </div>
  );
}
