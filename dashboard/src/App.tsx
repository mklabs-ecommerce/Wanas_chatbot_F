import { useState } from 'react'
import { AuthProvider, useAuth } from './auth'
import Nav from './components/Nav'
import TopBar from './components/TopBar'
import Dashboard from './pages/Dashboard'
import Login from './pages/Login'
import type { ChannelOrAll } from './types'

function Shell() {
  const [channel, setChannel] = useState<ChannelOrAll>('all')

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <Nav active={channel} onSelect={setChannel} />
      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar channel={channel} />
        <main className="flex-1 overflow-x-hidden">
          {/* key forces a remount on tab switch - without it, state like "which
              conversation is open" survives the channel change and the detail panel
              tries to fetch the old conversation id under the new channel. */}
          <Dashboard key={channel} channel={channel} />
        </main>
      </div>
    </div>
  )
}

function Gate() {
  const { account, loading } = useAuth()

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-(--color-bg) text-(--color-ink-soft)">
        جارٍ التحميل...
      </div>
    )
  }

  return account ? <Shell /> : <Login />
}

export default function App() {
  return (
    <AuthProvider>
      <Gate />
    </AuthProvider>
  )
}
