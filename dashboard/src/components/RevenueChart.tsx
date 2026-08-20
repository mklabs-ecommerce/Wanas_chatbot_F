import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

interface Props {
  daily: { date: string; orders: number; revenue: number }[]
  currency: string
}

// A day-by-day bar for a short range; grouped into weeks once there are more bars than
// would be readable (a "year" range would otherwise be 365 slivers).
const MAX_BARS = 45

function buildBars(daily: Props['daily']) {
  if (daily.length <= MAX_BARS) {
    return daily.map((row) => ({ label: shortDate(row.date), revenue: row.revenue }))
  }
  const weeks: { label: string; revenue: number }[] = []
  for (let i = 0; i < daily.length; i += 7) {
    const chunk = daily.slice(i, i + 7)
    weeks.push({
      label: shortDate(chunk[0].date),
      revenue: chunk.reduce((sum, row) => sum + row.revenue, 0),
    })
  }
  return weeks
}

function shortDate(iso: string) {
  const [, month, day] = iso.split('-')
  return `${day}/${month}`
}

export default function RevenueChart({ daily, currency }: Props) {
  const bars = buildBars(daily)
  const hasData = bars.some((bar) => bar.revenue > 0)

  return (
    <div className="rounded-2xl bg-(--color-card) p-4 shadow-sm ring-1 ring-(--color-line)">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="font-bold text-(--color-ink)">الإيراد خلال الفترة</h3>
        <span className="text-xs text-(--color-ink-soft)">{currency}</span>
      </div>
      {!hasData ? (
        <div className="flex h-56 items-center justify-center text-sm text-(--color-ink-soft)">
          لا توجد طلبات في هذه الفترة
        </div>
      ) : (
        // Chart axes stay left-to-right regardless of page direction - the same
        // convention Arabic-localized analytics tools (Analytics, Power BI) use, since
        // a time axis reading "newest-to-oldest" right-to-left is more confusing than a
        // page that briefly switches to LTR for one chart. Numbers/dates are LTR too
        // (see .ltr-num), so this keeps both consistent rather than fighting the grain
        // for one component only.
        <div dir="ltr" className="h-56">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={bars} margin={{ top: 4, right: 4, bottom: 0, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--color-line)" vertical={false} />
              <XAxis
                dataKey="label"
                tick={{ fontSize: 11, fill: 'var(--color-ink-soft)' }}
                axisLine={{ stroke: 'var(--color-line)' }}
                tickLine={false}
              />
              <YAxis
                tick={{ fontSize: 11, fill: 'var(--color-ink-soft)' }}
                axisLine={false}
                tickLine={false}
                width={44}
              />
              <Tooltip
                formatter={(value) => [Number(value).toLocaleString() + ' ' + currency, 'الإيراد']}
                contentStyle={{ direction: 'rtl', borderRadius: 8, borderColor: 'var(--color-line)' }}
              />
              <Bar dataKey="revenue" fill="var(--color-accent-500)" radius={[4, 4, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  )
}
