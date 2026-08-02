// ThreadForge 品牌 mark：三股线交织（thread + forge）
// 与 public/favicon.svg 同图形，避免两处路径漂移
export default function Logo({ size = 20, className }: { size?: number; className?: string }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" className={className} aria-hidden>
      <rect width="64" height="64" rx="20" fill="#172554" />
      <path
        d="M14 46 Q 22 22 32 32 T 52 16"
        stroke="#93c5fd"
        strokeWidth="6.5"
        fill="none"
        strokeLinecap="round"
      />
      <path
        d="M14 34 Q 22 10 32 20 T 52 4"
        stroke="#60a5fa"
        strokeWidth="5"
        fill="none"
        strokeLinecap="round"
      />
      <path
        d="M50 46 Q 42 22 32 32 T 12 16"
        stroke="#2563eb"
        strokeWidth="6.5"
        fill="none"
        strokeLinecap="round"
      />
    </svg>
  )
}
