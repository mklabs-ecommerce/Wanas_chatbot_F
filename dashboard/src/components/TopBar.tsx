import { CHANNEL_LABELS, type ChannelOrAll } from '../types'
import { useAuth } from '../auth'

export default function TopBar({ channel }: { channel: ChannelOrAll }) {
  const { account, logout } = useAuth()

  return (
    <header className="flex items-center justify-between border-b border-(--color-line) bg-(--color-card) px-4 py-3 md:px-6">
      <div>
        <h1 className="text-lg font-bold text-(--color-ink)">{CHANNEL_LABELS[channel]}</h1>
        <p className="text-xs text-(--color-ink-soft)">نظرة عامة على الأداء</p>
      </div>
      <div className="flex items-center gap-3">
        <div className="hidden text-end sm:block">
          <div className="text-sm font-medium text-(--color-ink)">{account?.username}</div>
          <div className="text-xs text-(--color-ink-soft)">
            {account?.role === 'owner' ? 'مالك' : 'موظف'}
          </div>
        </div>
        <button
          onClick={() => logout()}
          className="rounded-lg border border-(--color-line) px-3 py-1.5 text-sm font-medium text-(--color-ink-soft) transition hover:bg-(--color-bg)"
        >
          خروج
        </button>
      </div>
    </header>
  )
}
