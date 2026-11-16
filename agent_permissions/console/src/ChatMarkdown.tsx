import { memo } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

const plugins = [remarkGfm];
const components: Components = {
  a: ({ href, title, children }) =>
    href ? (
      <a
        href={href}
        title={title}
        target={href.startsWith("#") ? undefined : "_blank"}
        rel="noopener noreferrer"
      >
        {children}
      </a>
    ) : (
      <span>{children}</span>
    ),
  table: ({ children }) => (
    <div
      className="chat-markdown-table"
      role="region"
      aria-label="Markdown 表格"
      tabIndex={0}
    >
      <table>{children}</table>
    </div>
  ),
};

// Earlier messages keep their parsed tree while the current answer receives deltas.
export const ChatMarkdown = memo(function ChatMarkdown({
  content,
}: {
  content: string;
}) {
  return (
    <div className="chat-markdown">
      <Markdown remarkPlugins={plugins} components={components} skipHtml>
        {content}
      </Markdown>
    </div>
  );
});
