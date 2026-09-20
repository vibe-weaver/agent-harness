import { createElement } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

function slugify(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^\w一-鿿\s-]/g, '')
    .replace(/\s+/g, '-')
}

function heading(level: number) {
  return function Heading({ children }: { children?: React.ReactNode }) {
    const text = children?.toString() ?? ''
    const id = slugify(text)
    return createElement(`h${level}`, { id, style: { scrollMarginTop: '100px' } }, children)
  }
}

/** 自定义 a 标签渲染 — 只处理真实外部链接 */
function AnchorLink({ href, children, ...props }: any) {
  // 如果是外部链接，正常跳转
  if (href && (href.startsWith('http://') || href.startsWith('https://') || href.startsWith('mailto:'))) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer" {...props}>
        {children}
      </a>
    )
  }
  // 如果是锚点链接
  if (href && href.startsWith('#')) {
    return <a href={href} {...props}>{children}</a>
  }
  // 如果是 data URI（内嵌数据），直接跳转
  if (href && href.startsWith('data:')) {
    return <a href={href} {...props}>{children}</a>
  }
  // 其他情况（如相对路径 output.md）— 渲染为纯文本，不作为链接
  // 因为后端后处理已经把真正的文件提取为文件卡片了
  return <span style={{ color: 'var(--text-secondary)' }}>{children}</span>
}

interface Props {
  content: string
}

const components = {
  h1: heading(1),
  h2: heading(2),
  h3: heading(3),
  h4: heading(4),
  h5: heading(5),
  h6: heading(6),
  a: AnchorLink,
}

export function MarkdownRenderer({ content }: Props) {
  return (
    <div className="md-content">
      <style>{`
        /* ── DeepSeek 网页端排版风格（参考截图） ── */
        /* 基础: 16px / 26px — 比 DSH 的 28px 更紧凑，匹配网页端实际观感 */
        .md-content {
          font-size: 16px;
          line-height: 26px;
          color: var(--text-primary);
          overflow-wrap: anywhere;
        }

        /* ── 标题 — 上方留白充足，下方紧凑 ── */
        .md-content h1, .md-content h2, .md-content h3 {
          font-weight: 700;
          line-height: 1.35;
          margin: 36px 0 14px;
        }
        .md-content h1 {
          font-size: 1.75em;
          padding-bottom: 0.25em;
          border-bottom: 1px solid var(--line);
        }
        .md-content h2 {
          font-size: 1.45em;
          padding-bottom: 0.2em;
          border-bottom: 1px solid var(--line);
        }
        .md-content h3 {
          font-size: 1.2em;
        }
        .md-content h4, .md-content h5, .md-content h6 {
          font-weight: 600;
          margin: 20px 0 10px;
          line-height: 1.4;
        }
        .md-content h4 { font-size: 1.05em; }
        .md-content h5 { font-size: 0.95em; }
        .md-content h6 { font-size: 0.9em; color: var(--text-secondary); }
        /* h4-h6 后跟列表时缩小间距 */
        .md-content :where(h4, h5, h6) + :where(ul, ol) { margin-top: 6px; }
        .md-content :where(h4, h5, h6):has(+ :where(ul, ol)) { margin-bottom: 6px; }

        /* ── 段落 — 适中间距，不松散也不拥挤 ── */
        .md-content p {
          margin: 14px 0;
          color: var(--text-primary);
        }

        /* ── 行内代码 — 浅色背景 + 小圆角 ── */
        .md-content :not(pre) > code {
          display: inline-flex;
          align-items: center;
          box-sizing: border-box;
          font-size: 0.85em;
          font-family: 'SF Mono', 'Fira Code', Consolas, monospace;
          background: rgba(135, 131, 120, 0.15);
          color: #eb5757;
          border-radius: 4px;
          padding: 1px 5px;
          line-height: 1.6;
        }

        /* ── 代码块 — 大圆角 + 充足内边距 ── */
        .md-content pre {
          margin: 18px 0;
          background: rgba(175, 184, 193, 0.15);
          border: 1px solid rgba(175, 184, 193, 0.35);
          border-radius: 10px;
          padding: 18px 20px;
          overflow-x: auto;
        }
        .md-content pre code {
          background: none;
          color: var(--text-primary);
          padding: 0;
          font-size: 0.85em;
          line-height: 1.65;
          font-family: 'SF Mono', 'Fira Code', Consolas, monospace;
        }

        /* ── 引用 ── */
        .md-content blockquote {
          border-left: 3px solid rgba(152, 102, 56, 0.4);
          margin: 16px 0;
          padding: 8px 0 8px 16px;
          color: var(--text-secondary);
        }
        .md-content blockquote p { margin: 6px 0; }

        /* ── 列表 — 紧凑但清晰 ── */
        .md-content ul, .md-content ol {
          margin: 14px 0;
          padding-left: 22px;
        }
        .md-content li:not(:first-child) {
          margin-top: 6px;
        }
        .md-content li {
          color: var(--text-primary);
        }
        .md-content li > :where(ul, ol) {
          margin-top: 4px;
        }
        .md-content li::marker {
          line-height: 26px;
          color: var(--text-secondary);
          font-size: 0.95em;
        }
        .md-content li > p {
          margin: 6px 0;
        }
        .md-content li > *:first-child { margin-top: 0; }
        .md-content li > *:last-child { margin-bottom: 0; }

        /* ── 链接 ── */
        .md-content a {
          color: var(--accent);
          text-decoration: none;
          transition: color 0.15s;
        }
        .md-content a:hover {
          text-decoration: underline;
        }

        /* ── 强调 — 加粗更明显 ── */
        .md-content strong {
          font-weight: 650;
          color: var(--text-primary);
        }

        /* ── 分割线 ── */
        .md-content hr {
          display: block;
          border: none;
          height: 1px;
          margin: 32px 0;
          background: var(--line);
        }

        /* ── 图片 ── */
        .md-content img {
          max-width: 100%;
          border-radius: 8px;
        }

        /* ── 表格 — 宽松行高 + 清晰分隔线 ── */
        .md-content table {
          border-collapse: collapse;
          width: 100%;
          max-width: 100%;
          margin: 18px 0;
          overflow-x: auto;
          display: block;
        }
        .md-content thead {
          border-bottom: 1.5px solid var(--line);
        }
        .md-content th {
          text-align: start;
          padding: 11px 14px;
          font-weight: 600;
          font-size: 0.88em;
          color: var(--text-primary);
          white-space: nowrap;
        }
        .md-content td {
          padding: 11px 14px;
          border-bottom: 1px solid var(--line);
          font-size: 0.88em;
          color: var(--text-secondary);
          vertical-align: top;
        }
        .md-content tr:last-child td {
          border-bottom: none;
        }
        .md-content th:first-child,
        .md-content td:first-child { padding-left: 0; }
        .md-content th:last-child,
        .md-content td:last-child { padding-right: 0; }

        /* ── 首尾元素去除多余 margin ── */
        .md-content > *:first-child,
        .md-content p:first-child {
          margin-top: 0 !important;
        }
        .md-content > *:last-child,
        .md-content p:last-child {
          margin-bottom: 0 !important;
        }
      `}</style>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{content}</ReactMarkdown>
    </div>
  )
}
