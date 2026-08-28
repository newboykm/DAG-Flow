import { useGraphStore } from '../store/useGraphStore';

export default function ApprovalPrompt() {
  const pending = useGraphStore((s) => s.pendingApproval);
  if (!pending) return null;

  const argsText = pending.args
    ? Object.entries(pending.args)
        .map(([k, v]) => `${k}=${typeof v === 'string' ? v : JSON.stringify(v)}`)
        .join('\n')
    : '';

  return (
    <div className="modal-overlay">
      <div className="modal approval-modal">
        <div className="modal-title">需要人工审批</div>
        <div className="modal-sub">
          卡片即将调用敏感工具 <b>{pending.tool}</b>，是否允许？
        </div>
        <pre className="approval-args">{argsText}</pre>
        <div className="modal-actions">
          <button
            className="btn btn-primary"
            onClick={() => useGraphStore.getState().approveApproval(pending.approvalId)}
          >
            允许
          </button>
          <button
            className="btn"
            onClick={() => useGraphStore.getState().rejectApproval(pending.approvalId)}
          >
            拒绝
          </button>
        </div>
      </div>
    </div>
  );
}
