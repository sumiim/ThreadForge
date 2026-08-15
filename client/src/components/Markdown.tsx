import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Components } from 'react-markdown'
import type { ReactElement, ReactNode } from 'react'
import CodeBlock from './CodeBlock'
import ReasoningRow from './ReasoningRow'

/**
 * 从 <code className="language-xxx"> 中提取语言名
 */
function extractLang(className?: string): string | undefined {
  if (!className) return undefined
  for (const token of className.split(' ')) {
    if (token.startsWith('language-')) return token.slice(9)
  }
  return undefined
}

/**
 * 自定义渲染器：
 * - 代码围栏（fence）→ 根据语言派发到 CodeBlock 或 ReasoningRow
 * - 内联代码 → 保持原有 <code> 样式
 */
const components: Partial<Components> = {
  // pre 是 markdown 围栏的顶层容器，其唯一子节点是 code
  pre({ children }: { children?: ReactNode }) {
    // 尝试从子节点中提取 code 元素和语言
    const child = Array.isArray(children) ? children[0] : children
    if (child && typeof child === 'object' && 'type' in child) {
      const codeEl = child as ReactElement<{ className?: string; children?: ReactNode }>
      const lang = extractLang(codeEl.props?.className)

      // 从 code 子节点中提取纯文本
      const codeText = extractTextContent(codeEl)

      // 推理块 → ReasoningRow
      if (lang === 'think') {
        return <ReasoningRow text={codeText} />
      }

      // 其他代码块 → CodeBlock
      if (codeText) {
        return <CodeBlock code={codeText} lang={lang} />
      }
    }
    // fallback：原样渲染 pre
    return <pre>{children}</pre>
  },
  // 内联代码
  code({ className, children, ...props }) {
    // 如果它有语言类名且是单行短代码，说明是 markdown 的 inlineCode
    // （围栏已有 pre 接管，这个分支只命中内联）
    return (
      <code className={className} {...props}>
        {children}
      </code>
    )
  },
}

/** 递归提取 ReactElement 树中的文本内容 */
function extractTextContent(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(extractTextContent).join('')
  if (node && typeof node === 'object' && 'props' in node) {
    const el = node as ReactElement<{ children?: ReactNode }>
    return extractTextContent(el.props?.children)
  }
  return ''
}

// Agent 回答的 Markdown 渲染：GFM + 自定义 code block / reasoning 渲染
export default function Markdown({ content }: { content: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {content}
      </ReactMarkdown>
    </div>
  )
}
