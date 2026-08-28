import { useRef } from 'react';
import type { DagNode } from '../types';
import { useGraphStore } from '../store/useGraphStore';

interface Props {
  node: DagNode;
  x: number;
  y: number;
  width: number;
  height: number;
  isAnchor: boolean;
  dim: boolean;
  sourceSelected?: boolean;
}

export default function RootNode({ node, x, y, width, height, isAnchor, dim, sourceSelected }: Props) {
  const cls = [
    'dag-node',
    'dag-node--root',
    dim ? 'dimmed' : '',
    isAnchor ? 'is-anchor' : '',
    sourceSelected ? 'is-source-selected' : '',
  ]
    .filter(Boolean)
    .join(' ');

  const dragRef = useRef<{ sx: number; sy: number; moved: boolean } | null>(null);

  const handleClick = () => {
    useGraphStore.getState().clickNode(node.nodeId);
  };

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    dragRef.current = { sx: e.clientX, sy: e.clientY, moved: false };
    (e.currentTarget as HTMLElement).setPointerCapture?.(e.pointerId);
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.sx;
    const dy = e.clientY - d.sy;
    if (!d.moved && Math.abs(dx) + Math.abs(dy) > 3) d.moved = true;
    if (d.moved) {
      useGraphStore.getState().moveNode(node.nodeId, dx, dy);
      d.sx = e.clientX;
      d.sy = e.clientY;
    }
  };

  const onPointerUp = () => {
    dragRef.current = null;
  };

  return (
    <div
      className={cls}
      style={{ left: x, top: y, width, height }}
      onClick={handleClick}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      title={isAnchor ? '会话起点（已设为锚点，可拖动）' : '会话起点（点击设为锚点，可拖动）'}
    >
      <div className="root-pill">
        <span className="root-dot" />
        <span className="root-label">会话起点</span>
        {isAnchor ? <span className="root-anchor-hint">锚点</span> : null}
        {sourceSelected ? <span className="source-badge">✓ 已选</span> : null}
      </div>
    </div>
  );
}
