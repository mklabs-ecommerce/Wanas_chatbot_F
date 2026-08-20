import { useEffect, useState } from 'react'
import * as api from '../api'
import ActivityPanel from '../components/ActivityPanel'
import ConversationDetail from '../components/ConversationDetail'
import ConversationList from '../components/ConversationList'
import DateRangePicker, { defaultRange, type DateRange } from '../components/DateRangePicker'
import EmptyState from '../components/EmptyState'
import EscalationFunnel from '../components/EscalationFunnel'
import KpiCard from '../components/KpiCard'
import RevenueChart from '../components/RevenueChart'
import TopProducts from '../components/TopProducts'
import {
  PLACEHOLDER_CHANNELS,
  type Channel,
  type ChannelOrAll,
  type ConversationSummary,
  type Snapshot,
  type Sort,
} from '../types'

export default function Dashboard({ channel }: { channel: ChannelOrAll }) {
  const [range, setRange] = useState<DateRange>(defaultRange())
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null)
  const [error, setError] = useState<string | null>(null)

  const [conversations, setConversations] = useState<ConversationSummary[]>([])
  const [sort, setSort] = useState<Sort>('recent')
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const isPlaceholder = (PLACEHOLDER_CHANNELS as ChannelOrAll[]).includes(channel)
  // "All" is an analytics-only view - the owner's call: a merged conversation list
  // across every channel would mix contexts that only make sense read per-channel.
  const showConversations = channel !== 'all' && !isPlaceholder

  useEffect(() => {
    setSnapshot(null)
    setError(null)
    api
      .getAnalytics(channel, range)
      .then(setSnapshot)
      .catch((err) => setError(err.message ?? 'تعذر تحميل البيانات'))
  }, [channel, range])

  useEffect(() => {
    if (!showConversations) return
    api
      .listConversations(channel, { sort })
      .then((result) => setConversations(result.conversations))
      .catch(() => setConversations([]))
  }, [channel, sort, showConversations])

  if (isPlaceholder) {
    return (
      <div className="p-4 md:p-6">
        <EmptyState channel={channel as Channel} />
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-4 p-4 md:p-6">
      <DateRangePicker value={range} onChange={setRange} />

      {error && (
        <div className="rounded-xl bg-(--color-negative-bg) p-4 text-sm text-(--color-negative)">{error}</div>
      )}

      {!snapshot && !error && (
        <div className="rounded-xl bg-(--color-card) p-8 text-center text-sm text-(--color-ink-soft) ring-1 ring-(--color-line)">
          جارٍ التحميل...
        </div>
      )}

      {snapshot && (
        <>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <KpiCard
              label="الطلبات"
              value={snapshot.orders.current.toLocaleString()}
              trend={snapshot.orders}
            />
            <KpiCard
              label="الإيراد"
              value={snapshot.revenue.current.toLocaleString()}
              suffix={snapshot.currency}
              trend={snapshot.revenue}
            />
            <KpiCard
              label="متوسط قيمة الطلب"
              value={snapshot.average_order_value.toLocaleString()}
              suffix={snapshot.currency}
            />
            <KpiCard
              label="عملاء جدد / عائدون"
              value={`${snapshot.customers.new} / ${snapshot.customers.returning}`}
            />
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <div className="lg:col-span-2">
              <RevenueChart daily={snapshot.daily} currency={snapshot.currency} />
            </div>
            <TopProducts products={snapshot.top_products} currency={snapshot.currency} />
          </div>

          <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
            <PaymentSplitCard snapshot={snapshot} />
            <EscalationFunnel snapshot={snapshot} />
            <ActivityPanel byHour={snapshot.activity.by_hour} byWeekday={snapshot.activity.by_weekday} />
          </div>
        </>
      )}

      {showConversations && (
        <ConversationList
          conversations={conversations}
          sort={sort}
          onSortChange={setSort}
          onSelect={setSelectedId}
          selectedId={selectedId}
        />
      )}

      {selectedId && (
        <ConversationDetail channel={channel} conversationId={selectedId} onClose={() => setSelectedId(null)} />
      )}
    </div>
  )
}

function PaymentSplitCard({ snapshot }: { snapshot: Snapshot }) {
  const { cash_on_delivery, online } = snapshot.payment_split
  const total = cash_on_delivery.count + online.count
  const codPct = total ? (cash_on_delivery.count / total) * 100 : 0

  return (
    <div className="rounded-2xl bg-(--color-card) p-4 shadow-sm ring-1 ring-(--color-line)">
      <h3 className="mb-3 font-bold text-(--color-ink)">طريقة الدفع</h3>
      {total === 0 ? (
        <p className="text-sm text-(--color-ink-soft)">لا توجد طلبات في هذه الفترة</p>
      ) : (
        <>
          <div className="mb-2 h-3 overflow-hidden rounded-full bg-(--color-bg)">
            <div className="h-full bg-(--color-accent-500)" style={{ width: `${codPct}%` }} />
          </div>
          <div className="flex justify-between text-sm">
            <span className="text-(--color-ink)">
              الدفع عند الاستلام: <span className="ltr-num font-semibold">{cash_on_delivery.count}</span>
            </span>
            <span className="text-(--color-ink)">
              الدفع أونلاين: <span className="ltr-num font-semibold">{online.count}</span>
            </span>
          </div>
        </>
      )}
    </div>
  )
}
