import { theme as antdTheme } from 'antd'
import type { ThemeConfig } from 'antd'

// 设计基线（design-taste-frontend 方法）：白色调对话工作台
// - 唯一 accent：蓝 #2563eb，中性色统一暖灰 stone
// - 字体：Geist Sans / Geist Mono（Fontsource 自托管，开发工具气质）
// - 形状系统（SHAPE CONSISTENCY，全页统一）：控件 10px > 卡片/列表项 12px > 容器 16px > 微件全圆
// - 状态语义色独立于 accent：蓝灰=运行中、绿=完成、红=拒绝/错误、琥珀=待审批
export const themeConfig: ThemeConfig = {
  token: {
    colorPrimary: '#2563eb',
    colorInfo: '#2563eb',
    colorSuccess: '#15803d',
    colorWarning: '#d97706',
    colorError: '#dc2626',
    colorBgLayout: '#fafaf9',
    colorBgContainer: '#ffffff',
    colorText: '#1c1917',
    colorTextSecondary: '#57534e',
    colorTextPlaceholder: '#78716c', // stone-500，保证占位符对白底 AA 对比度
    colorBorder: '#e7e5e4',
    colorBorderSecondary: '#f5f5f4',
    borderRadius: 10, // 控件档（按钮/输入/Select 等），容器档由组件层 rounded-2xl 承担
    fontSize: 14,
    fontFamily: "'Geist Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
  },
  components: {
    Layout: {
      headerBg: '#ffffff',
      headerHeight: 56,
      headerPadding: '0 24px',
      siderBg: '#f5f5f4', // stone-100：导航区浅灰退后，对话区纯白突出
      // Sider theme="light" 时使用 lightSiderBg 覆盖 siderBg，必须同时设置（否则渲染为白）
      lightSiderBg: '#f5f5f4',
    },
    Button: {
      controlHeight: 34,
      primaryColor: '#ffffff',
    },
    Input: {
      // borderless 输入框保持白底、无内阴影，避免与容器 focus ring 双重高亮
      hoverBg: '#ffffff',
      activeBg: '#ffffff',
      activeShadow: 'none',
    },
  },
}

// 暗色主题:darkAlgorithm 派生 + 品牌暖灰覆盖。
// 关键联动:colorBgContainer == --tf-white(dark)、siderBg == --tf-stone-100(dark)、
// colorBorder == --tf-stone-200(dark),保证 antd 组件与 Tailwind 区域视觉一致。
export const darkThemeConfig: ThemeConfig = {
  algorithm: antdTheme.darkAlgorithm,
  token: {
    colorPrimary: '#3b82f6', // 暗底上比亮色提亮一档,维持 AA 对比
    colorInfo: '#3b82f6',
    colorSuccess: '#22c55e',
    colorWarning: '#f59e0b',
    colorError: '#ef4444',
    colorBgLayout: '#171513',
    colorBgContainer: '#1c1a18',
    colorText: '#e7e5e4',
    colorTextSecondary: '#a8a29e',
    colorTextPlaceholder: '#78716c',
    colorBorder: '#2e2b28',
    colorBorderSecondary: '#262422',
    borderRadius: 10,
    fontSize: 14,
    fontFamily: "'Geist Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
  },
  components: {
    Layout: {
      headerBg: '#1c1a18',
      siderBg: '#242220',
      lightSiderBg: '#242220',
    },
    Button: {
      controlHeight: 34,
      primaryColor: '#ffffff',
    },
    Input: {
      hoverBg: '#1c1a18',
      activeBg: '#1c1a18',
      activeShadow: 'none',
    },
  },
}
