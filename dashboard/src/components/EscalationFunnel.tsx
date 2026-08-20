import type { Snapshot } from '../types'

export default function EscalationFunnel({ snapshot }: { snapshot: Snapshot }) {
  const { escalation, funnel } = snapshot

  return (
    <div className="rounded-2xl bg-(--color-card) p-4 shadow-sm ring-1 ring-(--color-line)">
      <h3 className="mb-3 font-bold text-(--color-ink)">التصعيد ورحلة العميل</h3>

      <div className="mb-4">
        <div className="flex items-baseline justify-between">
          <span className="text-sm text-(--color-ink-soft)">نسبة التصعيد لشكوى</span>
          <span className="ltr-num text-lg font-bold text-(--color-ink)">
            {escalation.rate_pct === null ? '—' : escalation.rate_pct.toFixed(1) + '%'}
          </span>
        </div>
        <p className="mt-0.5 text-xs text-(--color-ink-soft)">
          {escalation.ticket_count} شكوى من أصل {escalation.conversation_count} محادثة
        </p>
      </div>

      <div className="flex flex-col gap-2">
        <FunnelStep
          label="التعليقات"
          value={funnel.comments}
          note={funnel.comments === null ? 'غير متاح لهذه القناة' : null}
        />
        <FunnelStep label="رسائل مباشرة اتفتحت" value={funnel.dms_opened} />
        <FunnelStep label="محادثات" value={funnel.conversations} />
        <FunnelStep label="طلبات" value={funnel.orders} highlight />
      </div>

      {funnel.conversation_to_order_pct !== null && (
        <p className="mt-3 text-xs text-(--color-ink-soft)">
          <span className="ltr-num">{funnel.conversation_to_order_pct.toFixed(1)}%</span> من
          المحادثات انتهت بطلب
        </p>
      )}
    </div>
  )
}

function FunnelStep({
  label,
  value,
  note,
  highlight,
}: {
  label: string
  value: number | null
  note?: string | null
  highlight?: boolean
}) {
  return (
    <div className="flex items-center justify-between rounded-lg bg-(--color-bg) px-3 py-2">
      <span className="text-sm text-(--color-ink-soft)">{label}</span>
      {value === null ? (
        <span className="text-xs text-(--color-ink-soft)">{note}</span>
      ) : (
        <span
          className={
            'ltr-num text-sm font-bold ' + (highlight ? 'text-(--color-accent-700)' : 'text-(--color-ink)')
          }
        >
          {value.toLocaleString()}
        </span>
      )}
    </div>
  )
}
