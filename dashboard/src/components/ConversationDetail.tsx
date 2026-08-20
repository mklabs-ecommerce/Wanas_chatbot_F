import { useEffect, useState, type ReactNode } from 'react'
import * as api from '../api'
import type { ChannelOrAll, ConversationDetail as Detail } from '../types'
import StatusPill from './StatusPill'

export default function ConversationDetail({
  channel,
  conversationId,
  onClose,
}: {
  channel: ChannelOrAll
  conversationId: string
  onClose: () => void
}) {
  const [detail, setDetail] = useState<Detail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setDetail(null)
    setError(null)
    api
      .getConversation(channel, conversationId)
      .then((result) => !cancelled && setDetail(result))
      .catch((err) => !cancelled && setError(err.message ?? 'تعذر تحميل المحادثة'))
    return () => {
      cancelled = true
    }
  }, [channel, conversationId])

  return (
    <div className="fixed inset-0 z-50 flex items-stretch justify-end bg-black/30 md:items-center md:justify-center">
      <div className="flex h-full w-full max-w-2xl flex-col bg-(--color-card) shadow-xl md:h-[85vh] md:rounded-2xl">
        <div className="flex items-center justify-between border-b border-(--color-line) p-4">
          <h3 className="font-bold text-(--color-ink)">تفاصيل المحادثة</h3>
          <button
            onClick={onClose}
            className="rounded-lg px-3 py-1 text-sm text-(--color-ink-soft) hover:bg-(--color-bg)"
          >
            إغلاق
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {error && <p className="text-sm text-(--color-negative)">{error}</p>}
          {!detail && !error && <p className="text-sm text-(--color-ink-soft)">جارٍ التحميل...</p>}

          {detail && (
            <div className="flex flex-col gap-6">
              <section className="flex flex-col gap-2">
                {detail.messages.length === 0 && (
                  <p className="text-sm text-(--color-ink-soft)">لا توجد رسائل بعد</p>
                )}
                {detail.messages.map((message, index) => (
                  <div
                    key={index}
                    className={'flex ' + (message.role === 'user' ? 'justify-start' : 'justify-end')}
                  >
                    <div
                      className={
                        'max-w-[80%] rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap ' +
                        (message.role === 'user'
                          ? 'bg-(--color-bg) text-(--color-ink)'
                          : 'bg-(--color-accent-50) text-(--color-ink)')
                      }
                    >
                      {message.content}
                    </div>
                  </div>
                ))}
              </section>

              {detail.orders.length > 0 && (
                <Section title="الطلبات">
                  <div className="flex flex-col gap-2">
                    {detail.orders.map((order) => (
                      <div
                        key={order.number}
                        className="flex items-center justify-between rounded-lg bg-(--color-bg) px-3 py-2 text-sm"
                      >
                        <div>
                          <span className="ltr-num font-medium text-(--color-ink)">{order.number}</span>
                          <span className="ms-2 text-(--color-ink-soft)">{order.placed_on}</span>
                        </div>
                        <div className="flex items-center gap-2">
                          <span className="ltr-num text-(--color-ink-soft)">{order.total}</span>
                          <StatusPill text={order.cancelled ? 'ملغي' : order.status} />
                        </div>
                      </div>
                    ))}
                  </div>
                </Section>
              )}
              {!detail.orders_readable && (
                <p className="text-xs text-(--color-negative)">تعذر قراءة بيانات الطلبات من Shopify الآن</p>
              )}

              {detail.tickets.length > 0 && (
                <Section title="الشكاوى">
                  <div className="flex flex-col gap-2">
                    {detail.tickets.map((ticket) => (
                      <div key={ticket.reference} className="rounded-lg bg-(--color-bg) px-3 py-2 text-sm">
                        <div className="mb-1 flex items-center justify-between">
                          <span className="font-medium text-(--color-ink)">{ticket.label}</span>
                          <StatusPill text={ticket.status} />
                        </div>
                        <p className="text-(--color-ink-soft)">{ticket.summary}</p>
                      </div>
                    ))}
                  </div>
                </Section>
              )}

              {detail.feedback.length > 0 && (
                <Section title="الآراء">
                  <div className="flex flex-col gap-2">
                    {detail.feedback.map((item, index) => (
                      <div key={index} className="rounded-lg bg-(--color-bg) px-3 py-2 text-sm">
                        <div className="mb-1">
                          <StatusPill text={item.label} />
                        </div>
                        <p className="text-(--color-ink)">{item.comment}</p>
                      </div>
                    ))}
                  </div>
                </Section>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section>
      <h4 className="mb-2 text-sm font-bold text-(--color-ink-soft)">{title}</h4>
      {children}
    </section>
  )
}
