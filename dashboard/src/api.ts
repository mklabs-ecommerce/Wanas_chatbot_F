// Thin wrapper over fetch. Knows the wire format (bearer token, JSON) and nothing about
// what a page does with the data - the same "dumb client" split the Python backend uses
// for its own integrations.

import type {
  Account,
  ChannelOrAll,
  ConversationDetail,
  ConversationListResult,
  LoginResult,
  Snapshot,
  Sort,
} from './types'

const TOKEN_KEY = 'wanas_admin_token'
const EXPIRES_KEY = 'wanas_admin_expires_at'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

// Fires when a call comes back 401, or the stored session has already expired - the
// one place that decides "the session is gone", so every caller reacts the same way
// instead of each screen inventing its own redirect-to-login logic.
type SessionListener = () => void
const sessionListeners: SessionListener[] = []
export function onSessionExpired(listener: SessionListener): () => void {
  sessionListeners.push(listener)
  return () => {
    const index = sessionListeners.indexOf(listener)
    if (index >= 0) sessionListeners.splice(index, 1)
  }
}

export function getSession(): { token: string; expiresAt: string } | null {
  const token = localStorage.getItem(TOKEN_KEY)
  const expiresAt = localStorage.getItem(EXPIRES_KEY)
  if (!token || !expiresAt) return null
  if (new Date(expiresAt).getTime() <= Date.now()) {
    clearSession()
    return null
  }
  return { token, expiresAt }
}

function storeSession(result: LoginResult) {
  localStorage.setItem(TOKEN_KEY, result.token)
  localStorage.setItem(EXPIRES_KEY, result.expires_at)
}

export function clearSession() {
  localStorage.removeItem(TOKEN_KEY)
  localStorage.removeItem(EXPIRES_KEY)
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const session = getSession()
  const headers = new Headers(init.headers)
  headers.set('Content-Type', 'application/json')
  if (session) headers.set('Authorization', 'Bearer ' + session.token)

  const response = await fetch('/admin/api' + path, { ...init, headers })

  if (response.status === 401) {
    clearSession()
    sessionListeners.forEach((listener) => listener())
    throw new ApiError(401, 'الجلسة انتهت، سجل دخول تاني')
  }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(response.status, body.detail || 'حصل خطأ غير متوقع')
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export async function login(username: string, password: string): Promise<Account> {
  const result = await request<LoginResult>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
  storeSession(result)
  return result.account
}

export async function logout(): Promise<void> {
  try {
    await request('/auth/logout', { method: 'POST' })
  } finally {
    clearSession()
  }
}

export function me(): Promise<Account> {
  return request('/auth/me')
}

export function listStaff(): Promise<{ staff: Account[] }> {
  return request('/auth/staff')
}

export function createStaff(username: string, password: string): Promise<Account> {
  return request('/auth/staff', { method: 'POST', body: JSON.stringify({ username, password }) })
}

export function removeStaff(accountId: number): Promise<void> {
  return request('/auth/staff/' + accountId, { method: 'DELETE' })
}

export function getAnalytics(
  channel: ChannelOrAll,
  range?: { start: string; end: string },
): Promise<Snapshot> {
  const query = range ? `?start=${range.start}&end=${range.end}` : ''
  return request('/analytics/' + channel + query)
}

export function listConversations(
  channel: ChannelOrAll,
  opts: { limit?: number; sort?: Sort } = {},
): Promise<ConversationListResult> {
  const params = new URLSearchParams()
  if (opts.limit) params.set('limit', String(opts.limit))
  if (opts.sort) params.set('sort', opts.sort)
  const query = params.toString() ? '?' + params.toString() : ''
  return request('/conversations/' + channel + query)
}

export function getConversation(
  channel: ChannelOrAll,
  conversationId: string,
): Promise<ConversationDetail> {
  return request('/conversations/' + channel + '/' + encodeURIComponent(conversationId))
}
