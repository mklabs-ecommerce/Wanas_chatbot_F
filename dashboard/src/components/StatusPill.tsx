// Colour groups, not an exhaustive status map - a status this hasn't seen still gets a
// readable neutral pill instead of falling through to nothing.
const POSITIVE = new Set(['fulfilled', 'shipped', 'paid', 'resolved', 'positive'])
const NEGATIVE = new Set(['cancelled', 'negative', 'failed'])
const WARNING = new Set(['pending', 'open', 'not shipped yet', 'unfulfilled', 'neutral'])

export default function StatusPill({ text }: { text: string }) {
  const key = text.toLowerCase()
  const tone = POSITIVE.has(key)
    ? 'bg-(--color-positive-bg) text-(--color-accent-700)'
    : NEGATIVE.has(key)
      ? 'bg-(--color-negative-bg) text-(--color-negative)'
      : WARNING.has(key)
        ? 'bg-amber-50 text-amber-700'
        : 'bg-(--color-bg) text-(--color-ink-soft)'

  return <span className={'rounded-full px-2.5 py-1 text-xs font-semibold ' + tone}>{text}</span>
}
