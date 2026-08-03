import type { ReactNode } from 'react'
import { Empty, Tag } from 'antd'
import { CodeOutlined, CompressOutlined, SafetyOutlined, ThunderboltOutlined } from '@ant-design/icons'
import type { SkillMetadata } from '../../api/types'

// 图标映射（数据层不持有 ReactNode，映射留在组件层）
const skillIcons: Record<string, ReactNode> = {
  'code-review': <CodeOutlined />,
  'security-review': <SafetyOutlined />,
  simplify: <CompressOutlined />,
}

// Skills 子页面：技能卡片列表（V1 展示占位，执行接入在后续版本）
export default function SkillsView({ items }: { items: SkillMetadata[] }) {
  return (
    <div className="h-full overflow-y-auto px-6 py-8 lg:px-10">
      <div className="mx-auto max-w-4xl">
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
            <ThunderboltOutlined />
          </span>
          <h1 className="text-lg font-semibold tracking-tight text-stone-900">Skills</h1>
        </div>
        <p className="mt-2 text-sm text-stone-500">后端声明的 Agent 技能目录。</p>

        {items.length === 0 ? (
          <Empty className="mt-16" description="后端未返回 Skills" />
        ) : (
          <div className="mt-6 grid gap-3 sm:grid-cols-2">
          {items.map((skill) => (
            <div key={skill.id} className="rounded-2xl border border-stone-100 bg-stone-50 p-4">
              <div className="flex items-center gap-2.5">
                <span className="text-blue-600">{skillIcons[skill.id] ?? <ThunderboltOutlined />}</span>
                <span className="font-mono text-sm font-medium text-stone-800">{skill.name}</span>
                <Tag className="ml-auto">计划中</Tag>
              </div>
              <p className="mt-1.5 text-xs text-stone-500">{skill.description}</p>
            </div>
          ))}
          </div>
        )}
      </div>
    </div>
  )
}
