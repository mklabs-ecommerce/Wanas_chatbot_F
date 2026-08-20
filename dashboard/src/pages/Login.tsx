import { useState, type FormEvent } from 'react'
import { useAuth } from '../auth'
import { ApiError } from '../api'

export default function Login() {
  const { login } = useAuth()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    setError(null)
    setBusy(true)
    try {
      await login(username, password)
    } catch (err) {
      setError(err instanceof ApiError ? err.message : 'اسم المستخدم أو كلمة المرور غلط')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-(--color-bg) px-4">
      <form
        onSubmit={onSubmit}
        className="w-full max-w-sm rounded-2xl bg-(--color-card) p-8 shadow-sm ring-1 ring-(--color-line)"
      >
        <h1 className="mb-1 text-center text-xl font-bold text-(--color-ink)">
          لوحة تحكم واناس جاليري
        </h1>
        <p className="mb-6 text-center text-sm text-(--color-ink-soft)">سجّل دخولك للمتابعة</p>

        <label className="mb-3 block">
          <span className="mb-1 block text-sm font-medium text-(--color-ink-soft)">
            اسم المستخدم
          </span>
          <input
            className="w-full rounded-lg border border-(--color-line) px-3 py-2 text-(--color-ink) outline-none focus:border-(--color-accent-500) focus:ring-2 focus:ring-(--color-accent-100)"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoFocus
            autoComplete="username"
          />
        </label>

        <label className="mb-5 block">
          <span className="mb-1 block text-sm font-medium text-(--color-ink-soft)">
            كلمة المرور
          </span>
          <input
            type="password"
            className="w-full rounded-lg border border-(--color-line) px-3 py-2 text-(--color-ink) outline-none focus:border-(--color-accent-500) focus:ring-2 focus:ring-(--color-accent-100)"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
          />
        </label>

        {error && (
          <p className="mb-4 rounded-lg bg-(--color-negative-bg) px-3 py-2 text-sm text-(--color-negative)">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={busy || !username || !password}
          className="w-full rounded-lg bg-(--color-accent-600) py-2.5 font-semibold text-white transition hover:bg-(--color-accent-700) disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? 'جارٍ الدخول...' : 'دخول'}
        </button>
      </form>
    </div>
  )
}
