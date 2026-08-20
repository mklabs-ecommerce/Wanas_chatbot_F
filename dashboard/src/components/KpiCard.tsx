import type { Trend } from '../types'

interface Props {
  label: string
  value: string
  trend?: Trend | null
  suffix?: string
}

export default function KpiCard({ label, value, trend, suffix }: Props) {
  return (
    <div className="rounded-2xl bg-(--color-card) p-4 shadow-sm ring-1 ring-(--color-line)">
      <div className="text-sm text-(--color-ink-soft)">{label}</div>
      <div className="mt-1 flex items-baseline gap-1">
        <span className="ltr-num text-2xl font-extrabold text-(--color-ink)">{value}</span>
        {suffix && <span className="text-sm text-(--color-ink-soft)">{suffix}</span>}
      </div>
      {trend && <TrendBadge trend={trend} />}
    </div>
  )
}

export function TrendBadge({ trend }: { trend: Trend }) {
  if (trend.change_pct === null) {
    return <div className="mt-2 text-xs text-(--color-ink-soft)">لا توجد بيانات مقارنة</div>
  }
  const isUp = trend.change_pct >= 0
  return (
    <div
      className={
        'ltr-num mt-2 inline-flex w-fit items-center gap-1 rounded-full px-2 py-0.5 text-xs font-semibold ' +
        (isUp
          ? 'bg-(--color-positive-bg) text-(--color-accent-700)'
          : 'bg-(--color-negative-bg) text-(--color-negative)')
      }
    >
      <span>{isUp ? '▲' : '▼'}</span>
      <span>{Math.abs(trend.change_pct).toFixed(1)}%</span>
    </div>
  )
}
