/**
 * MarkdownBlock — 回复消息的层级结构渲染（react-markdown，对齐 dsh 的 markdown 渲染）。
 * 支持标题/粗体/斜体/列表/代码块/链接/表格等层级结构。
 */
import ReactMarkdown from 'react-markdown';
import { useMemo } from 'react';

export default function MarkdownBlock({ text }: { text: string }) {
  // 简单的流式场景：文本未结束时不渲染代码块（避免闪烁），其它照常
  const content = useMemo(() => text ?? '', [text]);
  return (
    <div className="markdown-body">
      <ReactMarkdown>{content}</ReactMarkdown>
    </div>
  );
}
