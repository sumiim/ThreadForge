import { Button, Typography } from 'antd'

// 脚手架占位页：验证 antd + Tailwind 共存，后续按 features/ 拆分真实页面
export default function App() {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-4 bg-gray-100">
      <Typography.Title level={2} className="mb-0">
        ThreadForge Console
      </Typography.Title>
      <Typography.Text type="secondary">Session / Task 工作台脚手架已就绪</Typography.Text>
      <Button type="primary">开始一个新会话</Button>
    </div>
  )
}
