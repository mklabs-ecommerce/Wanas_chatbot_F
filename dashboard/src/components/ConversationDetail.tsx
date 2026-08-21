import { useCallback, useEffect, useState, type FormEvent, type ReactNode } from 'react'
import * as api from '../api'
import { ApiError } from '../api'
import type { ChannelOrAll, ConversationDetail as Detail } from '../types'
import StatusPill from './StatusPill'

// While the panel is open on an Instagram conversation, refresh it periodically so an
// incoming customer reply shows up without the owner closing and reopening the panel.
// Internal-dashboard code only - not the customer-facing widget, so it carries none of
// the risk that kept live delivery off the web channel this round.
const POLL_MS = 5000

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
  const [replyText, setReplyText] = useState('')
  const [sending, setSending] = useState(false)
  const [sendError, setSendError] = useState<string | null>(null)

  const load = useCallback(() => {
    api
      .getConversation(channel, conversationId)
      .then((result) => setDetail(result))
      .catch((err) => setError(err instanceof ApiError ? err.message : 'تعذر تحميل المحادثة'))
  }, [channel, conversationId])

  useEffect(() => {
    setDetail(null)
    setError(null)
    load()
  }, [load])

  useEffect(() => {
    if (detail?.channel !== 'instagram') return
    const timer = window.setInterval(load, POLL_MS)
    return () => window.clearInterval(timer)
  }, [detail?.channel, load])

  async function sendReply(event: FormEvent) {
    event.preventDefault()
    const text = replyText.trim()
    if (!text || sending) return
    setSending(true)
    setSendError(null)
    try {
      await api.replyToConversation(conversationId, text)
      setReplyText('')
      load()
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : 'تعذر إرسال الرد')
    } finally {
      setSending(false)
    }
  }

  async function handBackToBot() {
    try {
      await api.resumeBot(conversationId)
      load()
    } catch (err) {
      setSendError(err instanceof ApiError ? err.message : 'تعذر إرجاعها للبوت')
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-stretch justify-end bg-black/30 md:items-center md:justify-center">
      <div className="flex h-full w-full max-w-2xl flex-col bg-(--color-card) shadow-xl md:h-[85vh] md:rounded-2xl">
        <div className="flex items-center justify-between border-b border-(--color-line) p-4">
          <div>
            <h3 className="font-bold text-(--color-ink)">{detail?.customer_name ?? 'تفاصيل المحادثة'}</h3>
            {detail?.channel === 'instagram' && (
              <span
                className={
                  'mt-1 inline-block rounded-full px-2 py-0.5 text-xs font-semibold ' +
                  (detail.owner_active
                    ? 'bg-amber-50 text-amber-700'
                    : 'bg-(--color-positive-bg) text-(--color-accent-700)')
                }
              >
                {detail.owner_active ? 'بتتابعها بنفسك دلوقتي' : 'بيرد عليها البوت'}
              </span>
            )}
          </div>
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
                    className={'flex ' + (message.author === 'customer' ? 'justify-start' : 'justify-end')}
                  >
                    <div
                      className={
                        'max-w-[80%] rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap ' +
                        (message.author === 'customer'
                          ? 'bg-(--color-bg) text-(--color-ink)'
                          : message.author === 'owner'
                            ? 'bg-amber-50 text-(--color-ink)'
                            : 'bg-(--color-accent-50) text-(--color-ink)')
                      }
                    >
                      {message.author === 'owner' && (
                        <div className="mb-1 text-xs font-semibold text-amber-700">أنت</div>
                      )}
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

        {detail?.channel === 'instagram' && (
          <div className="border-t border-(--color-line) p-4">
            {sendError && <p className="mb-2 text-xs text-(--color-negative)">{sendError}</p>}
            <div className="flex items-center justify-between gap-2">
              <form onSubmit={sendReply} className="flex flex-1 gap-2">
                <input
                  value={replyText}
                  onChange={(event) => setReplyText(event.target.value)}
                  placeholder="اكتب ردك هنا..."
                  disabled={sending}
                  className="flex-1 rounded-lg border border-(--color-line) bg-(--color-bg) px-3 py-2 text-sm text-(--color-ink)"
                />
                <button
                  type="submit"
                  disabled={sending || !replyText.trim()}
                  className="rounded-lg bg-(--color-accent-500) px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
                >
                  إرسال
                </button>
              </form>
              {detail.owner_active && (
                <button
                  onClick={handBackToBot}
                  className="shrink-0 rounded-lg px-3 py-2 text-sm text-(--color-ink-soft) hover:bg-(--color-bg)"
                >
                  رجّعها للبوت
                </button>
              )}
            </div>
          </div>
        )}
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
