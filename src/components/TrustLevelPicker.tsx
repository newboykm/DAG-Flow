/**
 * TrustLevelPicker — 审核信任级选择（全部信任 / 部分信任 / 全部不信任）。
 * 控制 agent 的敏感操作是否需要人工审批。全局生效，卡片输入框上可随时切换。
 * 菜单用 Portal 渲染到 body + fixed 定位，确保在卡片/画布之上不被裁剪遮挡。
 */
import { createPortal } from 'react-dom';
import { useEffect, useRef, useState } from 'react';
import { api } from '../api';

const LABEL: Record<string, string> = { all: '全部信任', partial: '部分信任', none: '全部不信任' };
const TIP: Record<string, string> = {
  all: '敏感操作全部免审批，agent 全自主',
  partial: '危险命令/运行代码需审批，普通写文件免审',
  none: '所有写/编辑/命令/运行代码都要人工审批',
};
const ORDER = ['all', 'partial', 'none'] as const;

export default function TrustLevelPicker() {
  const [level, setLevel] = useState<string>('partial');
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const btnRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    api.getTrustLevel().then((r) => setLevel(r.level)).catch(() => {});
  }, []);

  const toggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    const el = btnRef.current;
    if (!el) return;
    if (open) {
      setOpen(false);
      setPos(null);
      return;
    }
    const r = el.getBoundingClientRect();
    // 菜单宽度约 250，居中于按钮或对齐左边；下方如果空间不够就向上弹
    const menuW = 250;
    let left = r.left;
    if (left + menuW > window.innerWidth - 8) left = Math.max(8, window.innerWidth - menuW - 8);
    const spaceBelow = window.innerHeight - r.bottom - 8;
    const menuH = ORDER.length * 44 + 16;
    const top = spaceBelow > menuH ? r.bottom + 4 : Math.max(8, r.top - menuH - 4);
    setPos({ top, left });
    setOpen(true);
  };

  const choose = (lv: string) => {
    setLevel(lv);
    setOpen(false);
    setPos(null);
    api.setTrustLevel(lv).catch(() => {});
  };

  // 点击别处关闭
  useEffect(() => {
    if (!open) return;
    const onDown = () => { setOpen(false); setPos(null); };
    window.addEventListener('mousedown', onDown);
    return () => window.removeEventListener('mousedown', onDown);
  }, [open]);

  return (
    <>
      <button
        ref={btnRef}
        className="trust-btn"
        title={TIP[level] ?? ''}
        onClick={toggle}
      >
        <span className="trust-dot" data-level={level} />
        审核：{LABEL[level] ?? level}
        <span className="trust-chevron">{open ? '▴' : '▾'}</span>
      </button>
      {open && pos
        ? createPortal(
            <div
              className="trust-menu trust-menu-fixed"
              style={{ top: pos.top, left: pos.left }}
              onMouseDown={(e) => e.stopPropagation()}
            >
              {ORDER.map((lv) => (
                <button
                  key={lv}
                  className={`trust-opt ${level === lv ? 'is-active' : ''}`}
                  onClick={(e) => { e.stopPropagation(); choose(lv); }}
                >
                  <span className="trust-dot" data-level={lv} />
                  <span className="trust-opt-col">
                    <span className="trust-opt-label">{LABEL[lv]}</span>
                    <span className="trust-opt-tip">{TIP[lv]}</span>
                  </span>
                </button>
              ))}
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
