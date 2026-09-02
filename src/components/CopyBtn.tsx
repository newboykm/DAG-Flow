/**
 * CopyBtn — 一键复制按钮，带"点击后变对号"反馈。
 */
import { useRef, useState } from 'react';

export default function CopyBtn({ text, className }: { text: string; className?: string }) {
  const [done, setDone] = useState(false);
  const t = useRef<number | null>(null);

  const copy = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (!text) return;
    navigator.clipboard?.writeText(text).then(() => {
      setDone(true);
      if (t.current) window.clearTimeout(t.current);
      t.current = window.setTimeout(() => setDone(false), 1200);
    }).catch(() => {});
  };

  return (
    <button className={className} title="复制" onClick={copy}>
      {done ? '✓' : '⧉'}
    </button>
  );
}
