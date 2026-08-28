import { useState } from 'react';
import { useGraphStore } from '../store/useGraphStore';

export default function UsageBar() {
  const usage = useGraphStore((s) => s.sessionUsage);
  const [editingBudget, setEditingBudget] = useState(false);
  const [budgetText, setBudgetText] = useState('');

  const balance = usage.balance;
  const balanceText =
    balance === null ? '未设预算' : `余额 ¥${balance >= 0 ? balance.toFixed(4) : balance.toFixed(4)}`;

  const commitBudget = () => {
    const v = parseFloat(budgetText);
    useGraphStore.getState().setSessionBudget(Number.isFinite(v) ? v : null);
    setEditingBudget(false);
    setBudgetText('');
  };

  return (
    <div className="hint-bar usage-bar">
      <span>Token：{usage.promptTokens + usage.completionTokens}</span>
      <span className="sep">·</span>
      <span>本次费用：¥{usage.cost.toFixed(4)}</span>
      <span className="sep">·</span>
      {editingBudget ? (
        <>
          <input
            className="budget-input"
            autoFocus
            placeholder="预算(元)"
            value={budgetText}
            onChange={(e) => setBudgetText(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commitBudget();
              if (e.key === 'Escape') setEditingBudget(false);
            }}
          />
          <button className="btn btn-small" onClick={commitBudget}>确定</button>
        </>
      ) : (
        <span className="budget-text" onClick={() => { setBudgetText(usage.budget != null ? String(usage.budget) : ''); setEditingBudget(true); }} style={{ cursor: 'pointer' }} title="点击设置预算">
          {balanceText}
        </span>
      )}
    </div>
  );
}
