import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

const markdownComponents: Components = {
  a({ href, children }) {
    const external = href?.startsWith('http://') || href?.startsWith('https://')
    return <a href={href} target={external ? '_blank' : undefined} rel={external ? 'noreferrer noopener' : undefined}>{children}</a>
  },
  table({ children }) {
    return <div className="markdown-table-wrap"><table>{children}</table></div>
  },
  img({ src, alt }) {
    return <img src={src} alt={alt ?? ''} loading="lazy" referrerPolicy="no-referrer" />
  },
}

export function MarkdownContent({ children, className = '' }: { children: string; className?: string }) {
  return <div className={`markdown-content ${className}`.trim()}>
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents} skipHtml>{children}</ReactMarkdown>
  </div>
}
