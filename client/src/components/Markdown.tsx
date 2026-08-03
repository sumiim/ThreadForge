import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

// Agent 回答的 Markdown 渲染：GFM（表格/删除线）+ .md-body 统一样式
// 代码高亮暂未引入（V1 无真实代码内容需求），后续可加 rehype 插件
export default function Markdown({ content }: { content: string }) {
  return (
    <div className="md-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  )
}
