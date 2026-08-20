import { useState } from 'react'

export interface DateRange {
  start: string
  end: string
}

function iso(date: Date): string {
  return date.toISOString().slice(0, 10)
}

// "end" is sent to the backend as an exclusive boundary (see admin.analytics.service),
// so "today" must be represented as tomorrow's date here, not today's.
function tomorrow(): Date {
  const date = new Date()
  date.setDate(date.getDate() + 1)
  return date
}

const PRESETS: { key: string; label: string; range: () => DateRange }[] = [
  {
    key: 'today',
    label: 'اليوم',
    range: () => {
      const end = tomorrow()
      const start = new Date(end)
      start.setDate(start.getDate() - 1)
      return { start: iso(start), end: iso(end) }
    },
  },
  {
    key: 'week',
    label: 'آخر ٧ أيام',
    range: () => {
      const end = tomorrow()
      const start = new Date(end)
      start.setDate(start.getDate() - 7)
      return { start: iso(start), end: iso(end) }
    },
  },
  {
    key: 'month',
    label: 'الشهر ده',
    range: () => {
      const end = tomorrow()
      const start = new Date(end.getFullYear(), end.getMonth(), 1)
      return { start: iso(start), end: iso(end) }
    },
  },
  {
    key: 'year',
    label: 'السنة دي',
    range: () => {
      const end = tomorrow()
      const start = new Date(end.getFullYear(), 0, 1)
      return { start: iso(start), end: iso(end) }
    },
  },
]

export function defaultRange(): DateRange {
  return PRESETS.find((preset) => preset.key === 'month')!.range()
}

export default function DateRangePicker({
  value,
  onChange,
}: {
  value: DateRange
  onChange: (range: DateRange) => void
}) {
  const [customOpen, setCustomOpen] = useState(false)
  const activePreset = PRESETS.find((preset) => {
    const range = preset.range()
    return range.start === value.start && range.end === value.end
  })

  return (
    <div className="flex flex-wrap items-center gap-2">
      {PRESETS.map((preset) => (
        <button
          key={preset.key}
          onClick={() => {
            setCustomOpen(false)
            onChange(preset.range())
          }}
          className={
            'rounded-full px-3 py-1.5 text-sm font-medium transition ' +
            (activePreset?.key === preset.key
              ? 'bg-(--color-accent-600) text-white'
              : 'bg-(--color-card) text-(--color-ink-soft) ring-1 ring-(--color-line)')
          }
        >
          {preset.label}
        </button>
      ))}
      <button
        onClick={() => setCustomOpen((open) => !open)}
        className={
          'rounded-full px-3 py-1.5 text-sm font-medium transition ' +
          (!activePreset
            ? 'bg-(--color-accent-600) text-white'
            : 'bg-(--color-card) text-(--color-ink-soft) ring-1 ring-(--color-line)')
        }
      >
        مخصص
      </button>

      {customOpen && (
        <div className="flex items-center gap-2 rounded-full bg-(--color-card) px-3 py-1 ring-1 ring-(--color-line)">
          <input
            type="date"
            value={value.start}
            onChange={(event) => onChange({ ...value, start: event.target.value })}
            className="ltr-num bg-transparent text-sm outline-none"
          />
          <span className="text-(--color-ink-soft)">إلى</span>
          <input
            type="date"
            value={value.end}
            onChange={(event) => onChange({ ...value, end: event.target.value })}
            className="ltr-num bg-transparent text-sm outline-none"
          />
        </div>
      )}
    </div>
  )
}
