import { useGraphStore } from '../store/useGraphStore';
import { STATUS_META } from '../graph/statusMeta';
import type { FilterStatus, NodeStatus } from '../types';

const FILTERS: FilterStatus[] = ['all', 'running', 'ready', 'pending', 'done', 'failed', 'blocked', 'cancelled', 'paused'];

export default function FilterBar() {
  const filterStatus = useGraphStore((s) => s.filterStatus);
  const filterKeyword = useGraphStore((s) => s.filterKeyword);
  const setStatus = useGraphStore((s) => s.setFilterStatus);
  const setKeyword = useGraphStore((s) => s.setFilterKeyword);

  return (
    <div className="filter-bar">
      <div className="status-chips">
        {FILTERS.map((f) => (
          <button
            key={f}
            className={filterStatus === f ? 'chip active' : 'chip'}
            onClick={() => setStatus(f)}
          >
            {f === 'all' ? '全部' : STATUS_META[f as NodeStatus].label}
          </button>
        ))}
      </div>
      <input
        className="search-input"
        placeholder="搜索任务标题…"
        value={filterKeyword}
        onChange={(e) => setKeyword(e.target.value)}
      />
    </div>
  );
}
