import { useGraphStore, CONCURRENCY } from '../store/useGraphStore';

export default function Toolbar() {
  const sessionId = useGraphStore((s) => s.sessionId);
  const nodeCount = useGraphStore((s) => Object.keys(s.nodes).length);
  const contextVersion = useGraphStore((s) => s.contextVersion);
  const contextEntries = useGraphStore((s) => s.contextEntries);

  return (
    <div className="toolbar">
      <div className="brand">
        <span className="brand-mark" aria-label="DAG Flow logo">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">
            <defs>
              <linearGradient id="dag-logo-grad" x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%" stopColor="#0055ff" />
                <stop offset="100%" stopColor="#6a8dff" />
              </linearGradient>
              <marker id="dag-logo-arrow" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="4.5" markerHeight="4.5" orient="auto">
                <path d="M0 0 L9 5 L0 10 z" fill="#0055ff" />
              </marker>
            </defs>
            <g stroke="url(#dag-logo-grad)" strokeWidth="2.2" strokeLinecap="round">
              <path d="M5 18 Q8 13 11.5 10" markerEnd="url(#dag-logo-arrow)" />
              <path d="M19 18 Q16 13 12.5 10" markerEnd="url(#dag-logo-arrow)" />
            </g>
            <g fill="url(#dag-logo-grad)">
              <circle cx="5" cy="18" r="2.2" />
              <circle cx="19" cy="18" r="3" />
              <circle cx="12" cy="6" r="4" />
            </g>
          </svg>
        </span>
        <div>
          <div className="brand-title">DAG Flow</div>
          <div className="brand-sub">
            会话 {sessionId} · {nodeCount} 节点 · 并发上限 {CONCURRENCY} · 上下文 v{contextVersion}（{contextEntries.length} 条）
          </div>
        </div>
      </div>
      <div className="toolbar-actions">
        <button className="btn" onClick={() => useGraphStore.getState().tidyCanvas()}>
          一键整理
        </button>
        <button
          className="btn"
          title="设置（大模型 / Skill）"
          onClick={() => useGraphStore.getState().setSettingsOpen(true)}
        >
          设置
        </button>
      </div>
    </div>
  );
}
