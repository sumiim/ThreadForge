// ThreadForge 品牌 mark：使用 icon/ 目录产出的真实图标（PNG）
// 经 Vite 打包（import），Electron file:// 与 Web 均正确解析
import logoUrl from '../assets/threadforge-logo.png'

export default function Logo({ size = 20, className }: { size?: number; className?: string }) {
  return (
    <img
      src={logoUrl}
      width={size}
      height={size}
      className={className}
      alt=""
      aria-hidden
      draggable={false}
    />
  )
}
