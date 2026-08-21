// Mirrors the JSON shapes app/modules/admin/*/schemas.py's to_dict() methods produce.
// Kept in one file since the backend is the single source of truth for these shapes -
// a mismatch here is a bug in this file, not a second definition to maintain in sync.

export type Channel = 'web' | 'whatsapp' | 'instagram' | 'tiktok' | 'facebook'
export type ChannelOrAll = Channel | 'all'

export const REAL_CHANNELS: Channel[] = ['web', 'instagram']
export const PLACEHOLDER_CHANNELS: Channel[] = ['whatsapp', 'tiktok', 'facebook']
export const ALL_TABS: ChannelOrAll[] = ['all', 'web', 'instagram', 'whatsapp', 'tiktok', 'facebook']

export const CHANNEL_LABELS: Record<ChannelOrAll, string> = {
  all: 'الكل',
  web: 'الموقع',
  instagram: 'انستجرام',
  whatsapp: 'واتساب',
  tiktok: 'تيك توك',
  facebook: 'فيسبوك',
}

export interface Account {
  id: number
  username: string
  role: 'owner' | 'staff'
  disabled: boolean
  created_at: string
}

export interface LoginResult {
  token: string
  expires_at: string
  account: Account
}

export interface Trend {
  current: number
  previous: number
  change_pct: number | null
}

export interface TopProduct {
  title: string
  quantity: number
  revenue: number
}

export interface Snapshot {
  channel: ChannelOrAll
  period: { start: string; end: string }
  connected: boolean
  orders: Trend
  revenue: Trend
  average_order_value: number
  currency: string
  top_products: TopProduct[]
  daily: { date: string; orders: number; revenue: number }[]
  customers: { new: number; returning: number }
  conversations: { count: number; messages: number }
  support_tickets: {
    count: number
    by_status: Record<string, number>
    resolution_tracking_available: boolean
  }
  feedback: {
    count: number
    by_sentiment: Record<string, number>
    rating_available: boolean
  }
  payment_split: {
    cash_on_delivery: { count: number; revenue: number }
    online: { count: number; revenue: number }
  }
  escalation: {
    rate_pct: number | null
    ticket_count: number
    conversation_count: number
  }
  funnel: {
    comments: number | null
    dms_opened: number | null
    conversations: number
    orders: number
    comment_to_dm_pct: number | null
    conversation_to_order_pct: number | null
  }
  activity: {
    by_hour: number[]
    by_weekday: number[]
    utc_offset_hours: number
  }
}

export interface ConversationSummary {
  conversation_id: string
  channel: Channel
  customer_name: string
  started_at: string | null
  last_at: string | null
  message_count: number
  last_message: string
  order_count: number
  ticket_count: number
  feedback_count: number
  piece_count: number
  unpaid_link_count: number
  worst_sentiment: 'negative' | 'neutral' | 'positive' | null
}

export interface ConversationListResult {
  channel: ChannelOrAll
  count: number
  sort: string
  conversations: ConversationSummary[]
}

export interface OrderRow {
  number: string
  placed_on: string
  status: string
  payment_status: string
  cash_on_delivery: boolean
  arrived: boolean
  total: string
  subtotal: string
  delivery: string
  currency: string
  phone: string | null
  email: string | null
  ships_to: string
  cancelled: boolean
  admin_url: string | null
  items: { title: string; quantity: number; variant: string | null }[]
}

export interface FeedbackRow {
  comment: string
  sentiment: string
  label: string
  order_number: string | null
  customer_name: string
  contact: string
  created_at: string | null
}

export interface TicketRow {
  reference: string
  category: string
  label: string
  summary: string
  status: string
  order_number: string | null
  contact: string
  created_at: string | null
}

export interface ConversationMessage {
  role: 'user' | 'model'
  // Who actually wrote it - a bot and an owner both store as role "model" (see
  // app/modules/chat/repository.py's OWNER_PROVIDER), so this is what tells them apart.
  author: 'customer' | 'bot' | 'owner'
  content: string
}

export interface ConversationDetail {
  conversation_id: string
  channel: Channel
  customer_name: string
  // Reply/takeover exists for Instagram only - see admin/conversations/__init__.py.
  owner_active: boolean
  taken_over_at: string | null
  messages: ConversationMessage[]
  orders: OrderRow[]
  orders_readable: boolean
  feedback: FeedbackRow[]
  tickets: TicketRow[]
}

export const SORTS = ['recent', 'oldest', 'messages', 'tickets', 'pieces', 'orders', 'feedback'] as const
export type Sort = (typeof SORTS)[number]

export const SORT_LABELS: Record<Sort, string> = {
  recent: 'الأحدث',
  oldest: 'الأقدم',
  messages: 'عدد الرسائل',
  tickets: 'عدد الشكاوى',
  pieces: 'عدد القطع',
  orders: 'عدد الطلبات',
  feedback: 'عدد الآراء',
}
