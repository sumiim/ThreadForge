import { useState } from 'react'
import { Button, Input } from 'antd'
import { SendOutlined, StopOutlined } from '@ant-design/icons'

interface ComposerProps {
  model: string
  running: boolean
  onSend: (content: string) => void
  onStop: () => void
}

// 输入区：16px 圆角容器 + focus 时 accent ring；模型名显示在右下角
export default function Composer({ model, running, onSend, onStop }: ComposerProps) {
  const [value, setValue] = useState('')

  const handleSend = () => {
    if (!value.trim() || running) return
    onSend(value)
    setValue('')
  }

  return (
    <div className="shrink-0 px-6 pb-6 lg:px-10">
      <div className="mx-auto max-w-4xl">
        <div className="rounded-2xl border border-stone-200 bg-white shadow-sm transition-shadow focus-within:border-blue-500 focus-within:ring-4 focus-within:ring-blue-100">
          <div className="px-4 pb-1 pt-3">
            <Input.TextArea
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onPressEnter={(e) => {
                if (!e.shiftKey) {
                  e.preventDefault()
                  handleSend()
                }
              }}
              placeholder={running ? 'Agent 正在运行…' : '描述任务，Enter 发送，Shift + Enter 换行'}
              autoSize={{ minRows: 1, maxRows: 8 }}
              disabled={running}
              variant="borderless"
              className="text-sm"
              // 压掉 antd borderless 的 focus-visible 内描边（键盘焦点可见性由容器 ring 承担）
              style={{ padding: 0, border: 'none', boxShadow: 'none', outline: 'none' }}
            />
          </div>
          <div className="flex items-center justify-between border-t border-stone-100 px-4 py-2">
            <span className="font-mono text-[11px] text-stone-500">Enter 发送 · Shift + Enter 换行</span>
            <div className="flex items-center gap-3">
              <span className="font-mono text-[11px] text-stone-500">{model}</span>
              {running ? (
                <Button
                  danger
                  type="primary"
                  icon={<StopOutlined />}
                  onClick={onStop}
                  className="transition-transform active:scale-95"
                >
                  停止
                </Button>
              ) : (
                <Button
                  type="primary"
                  shape="circle"
                  icon={<SendOutlined />}
                  disabled={!value.trim()}
                  onClick={handleSend}
                  aria-label="发送"
                  className="transition-transform active:scale-95"
                />
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
