import { useEffect, useRef } from 'react';
import { useGraphStore } from '../store/useGraphStore';
import type { AppendMode } from '../types';

const MODES: { key: AppendMode; label: string; hint: string }[] = [
  { key: 'serial', label: '串行', hint: '在锚点之后追加子节点' },
  { key: 'parallel', label: '并行', hint: '在锚点同层追加兄弟节点（parent = 锚点的父）' },
  { key: 'join', label: '合并 join', hint: '选择 ≥2 个节点，创建共同下游' },
];

export default function GlobalAppendBar() {
  const appendMode = useGraphStore((s) => s.appendMode);
  const anchorIds = useGraphStore((s) => s.anchorIds);
  const inputText = useGraphStore((s) => s.inputText);
  const nodes = useGraphStore((s) => s.nodes);
  const rootId = useGraphStore((s) => s.rootId);
  const focusNonce = useGraphStore((s) => s.focusNonce);
  const selectingSource = useGraphStore((s) => s.selectingSource);
  const sourceIds = useGraphStore((s) => s.sourceIds);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (focusNonce > 0) inputRef.current?.focus();
  }, [focusNonce]);

  const anchorLabel = (() => {
    if (appendMode === 'join') {
      if (anchorIds.length === 0) return '选择 ≥2 个节点…';
      return anchorIds.map((id) => nodes[id]?.title ?? id).join(' + ');
    }
    const id = anchorIds[0];
    return id ? (nodes[id]?.title ?? id) : '根节点（会话起点）';
  })();

  const effectiveAnchor = anchorIds[0] ?? rootId;

  const hint = (() => {
    const m = MODES.find((x) => x.key === appendMode)!;
    if (appendMode === 'parallel' && effectiveAnchor === rootId) {
      return '根节点不可并行追加，请先选择其他节点';
    }
    if (appendMode === 'join' && anchorIds.length < 2) {
      return '请再选择至少 1 个节点（共需 ≥2）';
    }
    return m.hint;
  })();

  return (
    <div className="append-bar">
      <div className="anchor-box">
        <span className="anchor-label">锚点</span>
        <span className={`anchor-value ${anchorIds.length ? 'has' : ''}`}>{anchorLabel}</span>
        {anchorIds.length ? (
          <button className="chip-btn" onClick={() => useGraphStore.getState().clearAnchors()}>
            清除
          </button>
        ) : null}
      </div>

      <div className="mode-seg">
        {MODES.map((m) => (
          <button
            key={m.key}
            className={appendMode === m.key ? 'seg-btn active' : 'seg-btn'}
            onClick={() => useGraphStore.getState().setAppendMode(m.key)}
          >
            {m.label}
          </button>
        ))}
      </div>

      <input
        ref={inputRef}
        className="append-input"
        placeholder={hint}
        value={inputText}
        onChange={(e) => useGraphStore.getState().setInputText(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') useGraphStore.getState().submitAppend();
        }}
      />

      <button
        className={selectingSource ? 'btn btn-warn' : 'btn btn-primary'}
        onClick={() =>
          selectingSource
            ? useGraphStore.getState().cancelNewCard()
            : useGraphStore.getState().startNewCard()
        }
      >
        {selectingSource ? '✕ 取消新建' : '＋ 新建卡片'}
      </button>

      {selectingSource ? (
        <div className="new-card-banner">
          <span>选择要结合的已有卡片（可多选，也可不选）：已选 {sourceIds.length} 张</span>
          <button
            className="btn btn-primary"
            onClick={() => useGraphStore.getState().confirmNewCard()}
          >
            ✓ 创建新卡片
          </button>
        </div>
      ) : null}
    </div>
  );
}
