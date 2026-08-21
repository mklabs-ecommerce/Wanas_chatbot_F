import type { ReactNode } from 'react'
import { SORT_LABELS, SORTS, type ConversationSummary, type Sort } from '../types'

const SENTIMENT_DOT: Record<string, string> = {
  negative: 'bg-(--color-negative)',
  neutral: 'bg-amber-400',
  positive: 'bg-(--color-accent-500)',
}

function timeAgo(iso: string | null): string {
  if (!iso) return ''
  const diffMs = Date.now() - new Date(iso).getTime()
  const minutes = Math.round(diffMs / 60000)
  if (minutes < 1) return 'الآن'
  if (minutes < 60) return `من ${minutes} دقيقة`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `من ${hours} ساعة`
  const days = Math.round(hours / 24)
  return `من ${days} يوم`
}

interface Props {
  conversations: ConversationSummary[]
  sort: Sort
  onSortChange: (sort: Sort) => void
  onSelect: (conversationId: string) => void
  selectedId: string | null
}

export default function ConversationList({ conversations, sort, onSortChange, onSelect, selectedId }: Props) {
  return (
    <div className="rounded-2xl bg-(--color-card) shadow-sm ring-1 ring-(--color-line)">
      <div className="flex items-center justify-between border-b border-(--color-line) p-4">
        <h3 className="font-bold text-(--color-ink)">المحادثات الأخيرة</h3>
        <select
          value={sort}
          onChange={(event) => onSortChange(event.target.value as Sort)}
          className="rounded-lg border border-(--color-line) bg-(--color-card) px-2 py-1 text-sm text-(--color-ink-soft)"
        >
          {SORTS.map((option) => (
            <option key={option} value={option}>
              {SORT_LABELS[option]}
            </option>
          ))}
        </select>
      </div>

      {conversations.length === 0 ? (
        <p className="p-6 text-center text-sm text-(--color-ink-soft)">لا توجد محادثات في هذه الفترة</p>
      ) : (
        <ul className="max-h-[32rem] divide-y divide-(--color-line) overflow-y-auto">
          {conversations.map((conversation) => (
            <li key={conversation.conversation_id}>
              <button
                onClick={() => onSelect(conversation.conversation_id)}
                className={
                  'flex w-full flex-col gap-1 p-3 text-start transition hover:bg-(--color-bg) ' +
                  (selectedId === conversation.conversation_id ? 'bg-(--color-bg)' : '')
                }
              >
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    {conversation.worst_sentiment && (
                      <span
                        className={'h-2 w-2 shrink-0 rounded-full ' + SENTIMENT_DOT[conversation.worst_sentiment]}
                        title={conversation.worst_sentiment}
                      />
                    )}
                    <span className="font-bold text-(--color-ink)">{conversation.customer_name}</span>
                  </div>
                  <span className="text-xs text-(--color-ink-soft)">{timeAgo(conversation.last_at)}</span>
                </div>
                <div className="ltr-num flex gap-1 text-xs text-(--color-ink-soft)">
                  {conversation.order_count > 0 && <Badge>{conversation.order_count} طلب</Badge>}
                  {conversation.ticket_count > 0 && <Badge tone="negative">{conversation.ticket_count} شكوى</Badge>}
                </div>
                <p className="line-clamp-2 text-sm text-(--color-ink)">
                  {conversation.last_message || '(بدون رسائل بعد)'}
                </p>
                <span className="ltr-num text-xs text-(--color-ink-soft)">
                  {conversation.message_count} رسالة
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Badge({ children, tone }: { children: ReactNode; tone?: 'negative' }) {
  return (
    <span
      className={
        'rounded-full px-1.5 py-0.5 ' +
        (tone === 'negative'
          ? 'bg-(--color-negative-bg) text-(--color-negative)'
          : 'bg-(--color-positive-bg) text-(--color-accent-700)')
      }
    >
      {children}
    </span>
  )
}
