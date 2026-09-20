import { useState, useEffect, type ReactNode } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { sendCode, register, login } from '../lib/auth-api'
import { useAuth } from '../hooks/useAuth'

interface AuthModalProps {
  open: boolean
  onClose: () => void
}

export function AuthModal({ open, onClose }: AuthModalProps) {
  const [mode, setMode] = useState<'login' | 'register'>('login')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [info, setInfo] = useState('')
  const [loading, setLoading] = useState(false)
  const [sendingCode, setSendingCode] = useState(false)
  const [countdown, setCountdown] = useState(0)
  const { refresh } = useAuth()

  // 倒计时
  useEffect(() => {
    if (countdown <= 0) return
    const t = setTimeout(() => setCountdown(c => c - 1), 1000)
    return () => clearTimeout(t)
  }, [countdown])

  // 重置状态
  useEffect(() => {
    if (open) {
      setError('')
      setInfo('')
    }
  }, [open, mode])

  const handleSendCode = async () => {
    setError('')
    setInfo('')

    if (!email.match(/^[1-9]\d{4,10}@qq\.com$/)) {
      setError('请输入正确的 QQ 邮箱（如 12345@qq.com）')
      return
    }

    setSendingCode(true)
    try {
      const res = await sendCode(email)
      setInfo(res.message)
      setCountdown(60)
    } catch (err) {
      setError(err instanceof Error ? err.message : '发送验证码失败')
    } finally {
      setSendingCode(false)
    }
  }

  const handleSubmit = async () => {
    setError('')
    setInfo('')

    // 注册要真实 QQ 邮箱收验证码；登录只做查库比对，不限域名
    //（初始管理员是 admin@local，强制 QQ 邮箱会让默认配置启动的实例登录不了）
    if (mode === 'register' && !email.match(/^[1-9]\d{4,10}@qq\.com$/)) {
      setError('请输入正确的 QQ 邮箱')
      return
    }
    if (password.length < 8) {
      setError('密码至少 8 位')
      return
    }

    setLoading(true)
    try {
      if (mode === 'register') {
        if (code.length !== 6) {
          setError('请输入 6 位验证码')
          setLoading(false)
          return
        }
        await register(email, password, code)
      } else {
        await login(email, password)
      }
      await refresh()
      onClose()
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败')
    } finally {
      setLoading(false)
    }
  }

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.2 }}
          style={{
            position: 'fixed',
            inset: 0,
            zIndex: 9999,
            background: 'rgba(0,0,0,0.5)',
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            padding: '20px',
          }}
          onClick={onClose}
        >
          <motion.div
            initial={{ scale: 0.9, y: 20 }}
            animate={{ scale: 1, y: 0 }}
            exit={{ scale: 0.9, y: 20 }}
            transition={{ type: 'spring', damping: 25, stiffness: 300 }}
            onClick={e => e.stopPropagation()}
            style={{
              background: 'var(--bg, #FCF9F2)',
              borderRadius: '20px',
              padding: '32px',
              maxWidth: '400px',
              width: '100%',
              boxShadow: '0 20px 60px rgba(0,0,0,0.3)',
              border: '1px solid var(--line, #EBE5DB)',
            }}
          >
            {/* 标题 */}
            <div style={{ textAlign: 'center', marginBottom: '24px' }}>
              <h2 style={{
                fontSize: '1.3em',
                fontWeight: 600,
                color: 'var(--text, #111)',
                marginBottom: '6px',
              }}>
                {mode === 'login' ? '登录' : '注册'}
              </h2>
              <p style={{ fontSize: '0.82em', color: 'var(--muted, #726960)' }}>
                {mode === 'login' ? '使用 QQ 邮箱登录' : '使用 QQ 邮箱注册账号'}
              </p>
            </div>

            {/* 模式切换 */}
            <div style={{
              display: 'flex',
              gap: '4px',
              padding: '4px',
              background: 'var(--line, #EBE5DB)',
              borderRadius: '12px',
              marginBottom: '20px',
            }}>
              <button
                onClick={() => setMode('login')}
                style={{
                  flex: 1,
                  padding: '8px 0',
                  border: 'none',
                  borderRadius: '8px',
                  fontSize: '0.85em',
                  fontWeight: 500,
                  cursor: 'pointer',
                  transition: 'all 0.2s',
                  background: mode === 'login' ? 'var(--accent, #986638)' : 'transparent',
                  color: mode === 'login' ? '#fff' : 'var(--muted, #726960)',
                }}
              >
                登录
              </button>
              <button
                onClick={() => setMode('register')}
                style={{
                  flex: 1,
                  padding: '8px 0',
                  border: 'none',
                  borderRadius: '8px',
                  fontSize: '0.85em',
                  fontWeight: 500,
                  cursor: 'pointer',
                  transition: 'all 0.2s',
                  background: mode === 'register' ? 'var(--accent, #986638)' : 'transparent',
                  color: mode === 'register' ? '#fff' : 'var(--muted, #726960)',
                }}
              >
                注册
              </button>
            </div>

            {/* 表单 */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: '14px' }}>
              {/* 邮箱 */}
              <div>
                <label style={{
                  display: 'block',
                  fontSize: '0.8em',
                  color: 'var(--muted, #726960)',
                  marginBottom: '6px',
                }}>
                  {mode === 'register' ? 'QQ 邮箱' : '邮箱'}
                </label>
                <input
                  type="email"
                  value={email}
                  onChange={e => setEmail(e.target.value)}
                  placeholder={mode === 'register' ? '12345@qq.com' : 'admin@local'}
                  style={inputStyle}
                />
              </div>

              {/* 验证码（仅注册） */}
              {mode === 'register' && (
                <div>
                  <label style={{
                    display: 'block',
                    fontSize: '0.8em',
                    color: 'var(--muted, #726960)',
                    marginBottom: '6px',
                  }}>
                    邮箱验证码
                  </label>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <input
                      type="text"
                      value={code}
                      onChange={e => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
                      placeholder="6 位验证码"
                      maxLength={6}
                      style={{ ...inputStyle, flex: 1 }}
                    />
                    <button
                      onClick={handleSendCode}
                      disabled={sendingCode || countdown > 0}
                      style={{
                        ...btnStyle,
                        background: countdown > 0 || sendingCode ? 'var(--line, #EBE5DB)' : 'var(--accent, #986638)',
                        color: countdown > 0 || sendingCode ? 'var(--muted, #726960)' : '#fff',
                        cursor: countdown > 0 || sendingCode ? 'not-allowed' : 'pointer',
                        whiteSpace: 'nowrap',
                        minWidth: '100px',
                      }}
                    >
                      {countdown > 0 ? `${countdown}s` : sendingCode ? '发送中…' : '发送验证码'}
                    </button>
                  </div>
                </div>
              )}

              {/* 密码 */}
              <div>
                <label style={{
                  display: 'block',
                  fontSize: '0.8em',
                  color: 'var(--muted, #726960)',
                  marginBottom: '6px',
                }}>
                  密码（8 位以上）
                </label>
                <input
                  type="password"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  placeholder="8 位以上密码"
                  style={inputStyle}
                />
              </div>

              {/* 错误/提示信息 */}
              {error && (
                <div style={{
                  fontSize: '0.82em',
                  color: '#c44',
                  background: 'rgba(200,60,60,0.08)',
                  padding: '8px 12px',
                  borderRadius: '8px',
                }}>
                  {error}
                </div>
              )}
              {info && (
                <div style={{
                  fontSize: '0.82em',
                  color: 'var(--accent, #986638)',
                  background: 'rgba(152,102,56,0.1)',
                  padding: '8px 12px',
                  borderRadius: '8px',
                }}>
                  {info}
                </div>
              )}

              {/* 提交按钮 */}
              <button
                onClick={handleSubmit}
                disabled={loading}
                style={{
                  ...btnStyle,
                  background: loading ? 'var(--line, #EBE5DB)' : 'var(--accent, #986638)',
                  color: loading ? 'var(--muted, #726960)' : '#fff',
                  cursor: loading ? 'not-allowed' : 'pointer',
                  marginTop: '4px',
                  padding: '12px 0',
                  fontSize: '0.95em',
                  fontWeight: 600,
                }}
              >
                {loading ? '处理中…' : mode === 'login' ? '登录' : '注册'}
              </button>
            </div>

            {/* 关闭按钮 */}
            <button
              onClick={onClose}
              style={{
                position: 'absolute',
                top: '12px',
                right: '12px',
                border: 'none',
                background: 'transparent',
                fontSize: '1.3em',
                color: 'var(--muted, #726960)',
                cursor: 'pointer',
                padding: '4px 8px',
                lineHeight: 1,
              }}
            >
              ✕
            </button>
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

const inputStyle: React.CSSProperties = {
  width: '100%',
  padding: '10px 14px',
  fontSize: '0.9em',
  border: '1px solid var(--line, #EBE5DB)',
  borderRadius: '10px',
  background: 'var(--card, #fff)',
  color: 'var(--text, #111)',
  outline: 'none',
  fontFamily: 'inherit',
  boxSizing: 'border-box' as const,
}

const btnStyle: React.CSSProperties = {
  border: 'none',
  borderRadius: '10px',
  padding: '10px 16px',
  fontSize: '0.85em',
  fontWeight: 500,
  fontFamily: 'inherit',
  transition: 'all 0.2s',
}
