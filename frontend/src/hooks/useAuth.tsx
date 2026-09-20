import { useState, useCallback, useEffect, createContext, useContext, type ReactNode } from 'react'
import {
  getToken,
  getEmail,
  fetchUserInfo,
  logout as apiLogout,
  type UserInfo,
} from '../lib/auth-api'

interface AuthState {
  user: UserInfo | null
  loading: boolean
  isLoggedIn: boolean
  login: () => Promise<void>
  logout: () => void
  refresh: () => Promise<void>
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserInfo | null>(null)
  const [loading, setLoading] = useState(true)

  const refresh = useCallback(async () => {
    const token = getToken()
    if (!token) {
      setUser(null)
      setLoading(false)
      return
    }
    try {
      const info = await fetchUserInfo()
      setUser(info)
    } catch {
      setUser(null)
    } finally {
      setLoading(false)
    }
  }, [])

  const login = useCallback(async () => {
    await refresh()
  }, [refresh])

  const logout = useCallback(() => {
    apiLogout()
    setUser(null)
  }, [])

  useEffect(() => {
    refresh()
  }, [refresh])

  return (
    <AuthContext.Provider value={{ user, loading, isLoggedIn: !!user, login, logout, refresh }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) {
    throw new Error('useAuth must be used within AuthProvider')
  }
  return ctx
}

export function useAuthEmail(): string | null {
  return getEmail()
}
