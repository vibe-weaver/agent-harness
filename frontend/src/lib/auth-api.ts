const API_BASE = '/api/v1'
const TOKEN_KEY = 'auth_token'
const EMAIL_KEY = 'auth_email'

/** 获取存储的 JWT token */
export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY)
  } catch {
    return null
  }
}

/** 获取存储的邮箱 */
export function getEmail(): string | null {
  try {
    return localStorage.getItem(EMAIL_KEY)
  } catch {
    return null
  }
}

/** 保存登录信息 */
function saveAuth(token: string, email: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token)
    localStorage.setItem(EMAIL_KEY, email)
  } catch {
    // ignore
  }
  window.dispatchEvent(new Event('auth-change'))
}

/** 清除登录信息 */
export function clearAuth(): void {
  try {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(EMAIL_KEY)
  } catch {
    // ignore
  }
  window.dispatchEvent(new Event('auth-change'))
}

/** 构建带 Authorization header 的请求配置 */
export function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const token = getToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...extra,
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return headers
}

export interface AuthResult {
  access_token: string
  token_type: string
  email: string | null
}

export interface UserInfo {
  id: number
  email: string | null
  username: string | null
  is_admin: boolean
}

/** 发送邮箱验证码 */
export async function sendCode(email: string): Promise<{ message: string }> {
  const res = await fetch(`${API_BASE}/auth/send-code`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email }),
  })
  let data: any
  try {
    data = await res.json()
  } catch {
    throw new Error(res.statusText || `发送验证码失败（${res.status}）`)
  }
  if (!res.ok) {
    const detail = data.detail
    if (typeof detail === 'string') throw new Error(detail)
    if (Array.isArray(detail)) throw new Error(detail.map((e: any) => e.msg || JSON.stringify(e)).join('; '))
    throw new Error('发送验证码失败')
  }
  return data
}

/** 注册 */
export async function register(email: string, password: string, code: string): Promise<AuthResult> {
  const res = await fetch(`${API_BASE}/auth/register`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password, code }),
  })
  let data: any
  try {
    data = await res.json()
  } catch {
    throw new Error(res.statusText || `注册失败（${res.status}）`)
  }
  if (!res.ok) {
    const detail = data.detail
    if (typeof detail === 'string') throw new Error(detail)
    if (Array.isArray(detail)) throw new Error(detail.map((e: any) => e.msg || JSON.stringify(e)).join('; '))
    throw new Error('注册失败')
  }
  saveAuth(data.access_token, data.email || email)
  return data
}

/** 登录 */
export async function login(email: string, password: string): Promise<AuthResult> {
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  })
  let data: any
  try {
    data = await res.json()
  } catch {
    throw new Error(res.statusText || `登录失败（${res.status}）`)
  }
  if (!res.ok) {
    const detail = data.detail
    if (typeof detail === 'string') throw new Error(detail)
    if (Array.isArray(detail)) throw new Error(detail.map((e: any) => e.msg || JSON.stringify(e)).join('; '))
    throw new Error('登录失败')
  }
  saveAuth(data.access_token, data.email || email)
  return data
}

/** 获取当前用户信息（验证 token 是否有效） */
export async function fetchUserInfo(): Promise<UserInfo | null> {
  const token = getToken()
  if (!token) return null

  const res = await fetch(`${API_BASE}/auth/me`, {
    headers: { Authorization: `Bearer ${token}` },
  })
  if (!res.ok) {
    clearAuth()
    return null
  }
  return res.json()
}

/** 退出登录 */
export function logout(): void {
  clearAuth()
}

/** 检查是否已登录 */
export function isLoggedIn(): boolean {
  return !!getToken()
}
