import { CHANNEL_LABELS, type Channel } from '../types'

export default function EmptyState({ channel }: { channel: Channel }) {
  return (
    <div className="flex flex-col items-center justify-center rounded-2xl bg-(--color-card) p-12 text-center shadow-sm ring-1 ring-(--color-line)">
      <div className="mb-3 flex h-14 w-14 items-center justify-center rounded-full bg-(--color-bg) text-2xl">
        🔌
      </div>
      <h3 className="mb-1 font-bold text-(--color-ink)">{CHANNEL_LABELS[channel]} مش متوصل لسه</h3>
      <p className="max-w-sm text-sm text-(--color-ink-soft)">
        القناة دي لسه ملهاش تكامل مع الشات بوت، فمفيش بيانات نعرضها هنا. هتظهر النتائج
        تلقائياً بمجرد ما القناة تتوصل.
      </p>
    </div>
  )
}
