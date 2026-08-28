import { useMemo, useState } from 'react';
import { useGraphStore } from '../store/useGraphStore';
import { api } from '../api';

export default function Sidebar() {
  const sessions = useGraphStore((s) => s.sessions);
  const sessionId = useGraphStore((s) => s.sessionId);
  const [showNew, setShowNew] = useState(false);
  const [title, setTitle] = useState('');
  const [workspace, setWorkspace] = useState('');
  const [pickerOpen, setPickerOpen] = useState(false);
  const [pickerPath, setPickerPath] = useState('');
  const [pickerEntries, setPickerEntries] = useState<{ name: string; isDir: boolean; path: string }[]>([]);
  const [pickerParent, setPickerParent] = useState('');

  const handleNew = () => {
    setShowNew(true);
    setTitle('');
    setWorkspace('');
  };

  const confirmNew = () => {
    const t = title.trim();
    const w = workspace.trim();
    useGraphStore.getState().newSession(t || undefined, w || undefined);
    setShowNew(false);
    setTitle('');
    setWorkspace('');
  };

  const openPicker = async (path: string) => {
    setPickerOpen(true);
    const res = await api.browseDir(path);
    setPickerPath(res.path);
    setPickerParent(res.parent || '');
    setPickerEntries(res.entries || []);
  };

  const browseWorkspace = () => openPicker('');

  const pickerNav = async (path: string) => {
    const res = await api.browseDir(path);
    setPickerPath(res.path);
    setPickerParent(res.parent || '');
    setPickerEntries(res.entries || []);
  };

  const pickerSelect = () => {
    setWorkspace(pickerPath);
    setPickerOpen(false);
  };

  // 按工作区（workspace）分组
  const groups = useMemo(() => {
    const m = new Map<string, typeof sessions>();
    for (const s of sessions) {
      const key = s.workspace || '未指定工作区';
      const arr = m.get(key);
      if (arr) arr.push(s);
      else m.set(key, [s]);
    }
    return Array.from(m.entries());
  }, [sessions]);

  return (
    <div className="sidebar">
      <div className="sidebar-header">
        <button className="btn btn-primary sidebar-new" onClick={handleNew}>
          ＋ 新建会话
        </button>
      </div>

      {showNew ? (
        <div className="sidebar-new-form">
          <input
            autoFocus
            value={title}
            placeholder="会话标题（可留空，稍后用首问自动命名）"
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') confirmNew();
              if (e.key === 'Escape') setShowNew(false);
            }}
          />
          <div className="workspace-row">
            <input
              value={workspace}
              placeholder="工作目录路径（如 D:\projects\myapp）"
              onChange={(e) => setWorkspace(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') confirmNew();
              }}
            />
            <button
              className="btn"
              title="打开文件管理器选择目录"
              onClick={browseWorkspace}
            >
              浏览
            </button>
          </div>
          <div className="sidebar-new-actions">
            <button className="btn" onClick={confirmNew}>创建</button>
            <button className="btn" onClick={() => setShowNew(false)}>取消</button>
          </div>
        </div>
      ) : null}

      {pickerOpen ? (
        <div className="modal-overlay picker-overlay">
          <div className="modal dir-picker-modal">
            <div className="modal-title">选择工作目录</div>
            <div className="dir-picker-path">
              {pickerPath || '（选择盘符）'}
            </div>
            <div className="dir-picker-list">
              {pickerParent || pickerPath ? (
                <button
                  className="dir-picker-item"
                  onClick={() => pickerNav(pickerParent)}
                >
                  <span className="dir-picker-icon">📁</span> ..
                </button>
              ) : null}
              {pickerEntries.map((e) => (
                <button
                  key={e.path}
                  className="dir-picker-item"
                  onClick={() => pickerNav(e.path)}
                >
                  <span className="dir-picker-icon">{e.isDir ? '📁' : '📄'}</span>
                  {e.name}
                </button>
              ))}
            </div>
            <div className="modal-actions">
              <button className="btn btn-primary" onClick={pickerSelect}>
                选择此目录
              </button>
              <button className="btn" onClick={() => setPickerOpen(false)}>
                取消
              </button>
            </div>
          </div>
        </div>
      ) : null}

      <div className="sidebar-list">
        {groups.length === 0 ? (
          <div className="sidebar-empty">暂无历史会话</div>
        ) : (
          groups.map(([group, items]) => (
            <div key={group} className="session-group">
              <div className="session-group-title">{group}</div>
              {items.map((s) => (
                <div
                  key={s.sessionId}
                  className={s.sessionId === sessionId ? 'session-item active' : 'session-item'}
                  onClick={() => useGraphStore.getState().loadSession(s.sessionId)}
                  title={s.title || '未命名会话'}
                >
                  <span className="session-title">{s.title || '未命名会话'}</span>
                  <span className="session-time">{formatTime(s.updatedAt)}</span>
                  <button
                    className="session-delete"
                    title="删除会话"
                    onClick={(e) => {
                      e.stopPropagation();
                      if (window.confirm(`确定删除会话「${s.title || '未命名会话'}」吗？该操作不可恢复。`)) {
                        useGraphStore.getState().deleteSession(s.sessionId);
                      }
                    }}
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function formatTime(iso: string): string {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  if (sameDay) return `${hh}:${mm}`;
  return `${d.getMonth() + 1}/${d.getDate()} ${hh}:${mm}`;
}
