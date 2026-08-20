import { ALL_TABS, CHANNEL_LABELS, PLACEHOLDER_CHANNELS, type ChannelOrAll } from '../types'

const PLACEHOLDER_TABS: ChannelOrAll[] = PLACEHOLDER_CHANNELS

interface Props {
  active: ChannelOrAll
  onSelect: (channel: ChannelOrAll) => void
}

// One nav, two layouts: a right-hand sidebar (RTL - the first flex child in a row sits
// on the right with no manual positioning) on a wide screen, a horizontally scrollable
// tab strip under the top bar on a phone. Same data, same click handler either way, so
// the two can never disagree about which tab is active.
export default function Nav({ active, onSelect }: Props) {
  return (
    <>
      <nav className="hidden w-56 shrink-0 flex-col gap-1 border-e border-(--color-line) bg-(--color-card) p-4 md:flex">
        <div className="mb-4 px-2 text-lg font-extrabold text-(--color-accent-700)">واناس جاليري</div>
        {ALL_TABS.map((tab) => (
          <NavItem key={tab} tab={tab} active={active === tab} onSelect={onSelect} />
        ))}
      </nav>

      <nav className="flex gap-2 overflow-x-auto border-b border-(--color-line) bg-(--color-card) px-3 py-2 md:hidden">
        {ALL_TABS.map((tab) => (
          <button
            key={tab}
            onClick={() => onSelect(tab)}
            className={
              'shrink-0 rounded-full px-4 py-1.5 text-sm font-medium transition ' +
              (active === tab
                ? 'bg-(--color-accent-600) text-white'
                : 'bg-(--color-bg) text-(--color-ink-soft)')
            }
          >
            {CHANNEL_LABELS[tab]}
          </button>
        ))}
      </nav>
    </>
  )
}

function NavItem({
  tab,
  active,
  onSelect,
}: {
  tab: ChannelOrAll
  active: boolean
  onSelect: (channel: ChannelOrAll) => void
}) {
  const isPlaceholder = PLACEHOLDER_TABS.includes(tab)
  return (
    <button
      onClick={() => onSelect(tab)}
      className={
        'flex items-center justify-between rounded-lg px-3 py-2 text-start text-sm font-medium transition ' +
        (active
          ? 'bg-(--color-accent-600) text-white'
          : 'text-(--color-ink-soft) hover:bg-(--color-bg)')
      }
    >
      <span>{CHANNEL_LABELS[tab]}</span>
      {isPlaceholder && !active && (
        <span className="rounded-full bg-(--color-line) px-2 py-0.5 text-[10px] text-(--color-ink-soft)">
          قريباً
        </span>
      )}
    </button>
  )
}
