import type { ReactNode } from 'react'
import { CodeOutlined, CompressOutlined, SafetyOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { SKILL_OPTIONS } from '../../api/constants'

// 图标映射（数据层不持有 ReactNode，映射留在组件层）
const skillIcons: Record<string, ReactNode> = {
  'code-review': <CodeOutlined />,
  'security-review': <SafetyOutlined />,
  simplify: <CompressOutlined />,
}

// Skills 子页面：技能卡片列表（V1 展示占位，执行接入在后续版本）
export default function SkillsView() {
  return (
    <div className="h-full overflow-y-auto px-6 py-8 lg:px-10">
      <div className="mx-auto max-w-4xl">
        <div className="flex items-center gap-2.5">
          <span className="flex h-8 w-8 items-center justify-center rounded-xl bg-blue-50 text-blue-600">
            <ThunderboltOutlined />
          </span>
          <h1 className="text-lg font-semibold tracking-tight text-stone-900">Skills</h1>
        </div>
        <p className="mt-2 text-sm text-stone-500">可用的 Agent 技能。V1 为展示占位，执行接入在后续版本。</p>

        <div className="mt-6 grid gap-3 sm:grid-cols-2">
          {SKILL_OPTIONS.map((skill) => (
            <div key={skill.id} className="rounded-2xl border border-stone-100 bg-stone-50 p-4">
              <div className="flex items-center gap-2.5">
                <span className="text-blue-600">{skillIcons[skill.id]}</span>
                <span className="font-mono text-sm font-medium text-stone-800">{skill.name}</span>
              </div>
              <p className="mt-1.5 text-xs text-stone-500">{skill.desc}</p>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
