import React, { useMemo } from 'react';
import { marked } from 'marked';

interface MarkdownContentProps {
  content: string;
  className?: string;
}

// Configure marked with GitHub Flavored Markdown
marked.setOptions({
  gfm: true,
  breaks: true,
});

export const MarkdownContent: React.FC<MarkdownContentProps> = ({ content, className = '' }) => {
  const html = useMemo(() => {
    if (!content) return '';
    try {
      return marked.parse(content) as string;
    } catch (err) {
      console.error('Failed to parse markdown:', err);
      return content;
    }
  }, [content]);

  return (
    <div
      className={`markdown-body leading-relaxed text-xs sm:text-sm select-text ${className}`}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  );
};
