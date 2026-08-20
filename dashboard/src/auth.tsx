import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import * as api from './api'
import type { Account } from './types'

interface AuthState {
  account: Account | null
  loading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [account, setAccount] = useState<Account | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const unsubscribe = api.onSessionExpired(() => setAccount(null))
    if (!api.getSession()) {
      setLoading(false)
      return unsubscribe
    }
    api
      .me()
      .then(setAccount)
      .catch(() => setAccount(null))
      .finally(() => setLoading(false))
    return unsubscribe
  }, [])

  const login = async (username: string, password: string) => {
    const loggedInAccount = await api.login(username, password)
    setAccount(loggedInAccount)
  }

  const logout = async () => {
    await api.logout().catch(() => {})
    setAccount(null)
  }

  return (
    <AuthContext.Provider value={{ account, loading, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside AuthProvider')
  return context
}
