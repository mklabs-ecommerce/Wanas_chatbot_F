const WEEKDAY_LABELS = ['الإثنين', 'الثلاثاء', 'الأربعاء', 'الخميس', 'الجمعة', 'السبت', 'الأحد']

export default function ActivityPanel({
  byHour,
  byWeekday,
}: {
  byHour: number[]
  byWeekday: number[]
}) {
  const total = byHour.reduce((sum, value) => sum + value, 0)

  return (
    <div className="rounded-2xl bg-(--color-card) p-4 shadow-sm ring-1 ring-(--color-line)">
      <h3 className="mb-1 font-bold text-(--color-ink)">أوقات التواصل الأكثر ازدحاماً</h3>
      <p className="mb-4 text-xs text-(--color-ink-soft)">بتوقيت القاهرة المحلي، مفيد لتوقيت التوصيل والدعم</p>

      {total === 0 ? (
        <p className="text-sm text-(--color-ink-soft)">لا توجد رسائل في هذه الفترة</p>
      ) : (
        <>
          <div className="mb-5">
            <div className="mb-2 text-xs font-medium text-(--color-ink-soft)">حسب أيام الأسبوع</div>
            <Bars values={byWeekday} labels={WEEKDAY_LABELS} />
          </div>
          <div>
            <div className="mb-2 text-xs font-medium text-(--color-ink-soft)">حسب ساعات اليوم</div>
            <HourBars values={byHour} />
          </div>
        </>
      )}
    </div>
  )
}

function Bars({ values, labels }: { values: number[]; labels: string[] }) {
  const max = Math.max(1, ...values)
  return (
    <div className="flex flex-col gap-1.5">
      {values.map((value, index) => (
        <div key={labels[index]} className="flex items-center gap-2">
          <span className="w-16 shrink-0 text-xs text-(--color-ink-soft)">{labels[index]}</span>
          <div className="h-2 flex-1 overflow-hidden rounded-full bg-(--color-bg)">
            <div
              className="h-full rounded-full bg-(--color-accent-400)"
              style={{ width: `${(value / max) * 100}%` }}
            />
          </div>
          <span className="ltr-num w-6 shrink-0 text-end text-xs text-(--color-ink-soft)">{value}</span>
        </div>
      ))}
    </div>
  )
}

// 24 bars is too dense for the weekday layout's labels, so hours get a plain sparkline
// strip instead - the shape of the day matters more here than reading an exact hour.
function HourBars({ values }: { values: number[] }) {
  const max = Math.max(1, ...values)
  return (
    <div dir="ltr" className="flex h-16 items-end gap-0.5">
      {values.map((value, hour) => (
        <div
          key={hour}
          title={`${hour}:00 — ${value}`}
          className="flex-1 rounded-t bg-(--color-accent-400)"
          style={{ height: `${Math.max(4, (value / max) * 100)}%` }}
        />
      ))}
    </div>
  )
}
