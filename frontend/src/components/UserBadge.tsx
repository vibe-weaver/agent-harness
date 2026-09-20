import { useState, useRef, useEffect } from 'react'
import { useAuth } from '@/hooks/useAuth'
/**
 * 用户登录状态徽章 — 固定在页面左下角
 * 登录后显示账号邮箱 + 退出按钮
 * 3D 立体静态风格，与 AI 对话页整体水墨暖色设计语言一致
 */
export function UserBadge() {
  const { user, isLoggedIn, logout } = useAuth()
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  // 点击外部关闭
  useEffect(() => {
    if (!open) return
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onClick)
    return () => document.removeEventListener('mousedown', onClick)
  }, [open])

  if (!isLoggedIn || !user) return null

  // 取显示名：优先 username，其次 email
  const displayName = user.username || user.email || '已登录'
  // 头像首字母
  const initial = (user.username || user.email || '?')[0].toUpperCase()

  return (
    <div
      ref={ref}
      style={{
        position: 'fixed',
        bottom: '20px',
        left: '20px',
        zIndex: 200,
      }}
    >
      {/* ── 收起态：3D 立体圆角胶囊按钮 ── */}
      <button
        onClick={() => setOpen(v => !v)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: '8px',
          padding: '6px 16px 6px 6px',
          background: 'linear-gradient(180deg, color-mix(in srgb, var(--card-bg-solid) 96%, #fff) 0%, var(--card-bg-solid) 50%, color-mix(in srgb, var(--card-bg-solid) 88%, #000) 100%)',
          border: '1px solid color-mix(in srgb, var(--accent) 25%, var(--line))',
          borderRadius: '28px',
          cursor: 'pointer',
          fontFamily: 'inherit',
          fontSize: '13px',
          fontWeight: 500,
          color: 'var(--text-secondary)',
          boxShadow: [
            '0 2px 0 color-mix(in srgb, var(--accent) 15%, #000)',
            '0 4px 14px rgba(0,0,0,0.08)',
            'inset 0 1px 0 rgba(255,255,255,0.5)',
            'inset 0 -1px 0 rgba(0,0,0,0.03)',
          ].join(', '),
          backdropFilter: 'blur(12px)',
          WebkitBackdropFilter: 'blur(12px)',
          transition: 'transform 0.18s, box-shadow 0.18s, border-color 0.18s',
          transform: 'translateY(-1px)',
        }}
        onMouseEnter={e => {
          e.currentTarget.style.transform = 'translateY(-2px)'
          e.currentTarget.style.borderColor = 'color-mix(in srgb, var(--accent) 50%, var(--line))'
          e.currentTarget.style.boxShadow = [
            '0 3px 0 color-mix(in srgb, var(--accent) 20%, #000)',
            '0 6px 20px color-mix(in srgb, var(--accent) 18%, transparent)',
            'inset 0 1px 0 rgba(255,255,255,0.5)',
            'inset 0 -1px 0 rgba(0,0,0,0.03)',
          ].join(', ')
        }}
        onMouseLeave={e => {
          e.currentTarget.style.transform = 'translateY(-1px)'
          e.currentTarget.style.borderColor = 'color-mix(in srgb, var(--accent) 25%, var(--line))'
          e.currentTarget.style.boxShadow = [
            '0 2px 0 color-mix(in srgb, var(--accent) 15%, #000)',
            '0 4px 14px rgba(0,0,0,0.08)',
            'inset 0 1px 0 rgba(255,255,255,0.5)',
            'inset 0 -1px 0 rgba(0,0,0,0.03)',
          ].join(', ')
        }}
      >
        {/* 头像圆 — 3D 立体球体效果 */}
        <span style={{
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          width: '28px',
          height: '28px',
          borderRadius: '50%',
          background: 'linear-gradient(180deg, color-mix(in srgb, var(--accent) 80%, #fff) 0%, var(--accent) 50%, color-mix(in srgb, var(--accent) 70%, #000) 100%)',
          color: '#fff',
          fontSize: '13px',
          fontWeight: 700,
          flexShrink: 0,
          boxShadow: [
            'inset 0 1px 2px rgba(255,255,255,0.4)',
            'inset 0 -1px 2px rgba(0,0,0,0.15)',
            '0 2px 4px color-mix(in srgb, var(--accent) 30%, transparent)',
          ].join(', '),
          textShadow: '0 1px 1px rgba(0,0,0,0.2)',
        }}>{initial}</span>
        <span style={{
          maxWidth: '180px',
          overflow: 'hidden',
          textOverflow: 'ellipsis',
          whiteSpace: 'nowrap',
        }}>{displayName}</span>
        <svg width="10" height="10" viewBox="0 0 12 12" fill="none" style={{
          transition: 'transform 0.2s',
          transform: open ? 'rotate(180deg)' : 'none',
          opacity: 0.5,
          flexShrink: 0,
        }}>
          <path d="M2 4l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </button>

      {/* ── 展开态：3D 立体下拉面板 ── */}
      {open && (
        <div style={{
          position: 'absolute',
          bottom: 'calc(100% + 10px)',
          left: 0,
          minWidth: '220px',
          background: 'linear-gradient(180deg, color-mix(in srgb, var(--card-bg-solid) 97%, #fff) 0%, var(--card-bg-solid) 60%, color-mix(in srgb, var(--card-bg-solid) 92%, #000) 100%)',
          border: '1px solid color-mix(in srgb, var(--accent) 25%, var(--line))',
          borderRadius: '14px',
          boxShadow: [
            '0 2px 0 color-mix(in srgb, var(--accent) 12%, #000)',
            '0 12px 40px rgba(0,0,0,0.12)',
            '0 4px 12px rgba(0,0,0,0.06)',
            'inset 0 1px 0 rgba(255,255,255,0.5)',
          ].join(', '),
          backdropFilter: 'blur(16px)',
          WebkitBackdropFilter: 'blur(16px)',
          padding: '8px',
          display: 'flex',
          flexDirection: 'column',
          gap: '2px',
        }}>
          {/* 用户信息区 — 带渐变背景 */}
          <div style={{
            padding: '14px 16px',
            marginBottom: '6px',
            borderRadius: '10px',
            background: 'linear-gradient(135deg, color-mix(in srgb, var(--accent) 8%, transparent) 0%, color-mix(in srgb, var(--accent) 3%, transparent) 100%)',
            border: '1px solid color-mix(in srgb, var(--accent) 15%, transparent)',
            boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.3)',
          }}>
            <div style={{
              fontSize: '12px',
              color: 'var(--text-tertiary)',
              marginBottom: '6px',
              letterSpacing: '0.5px',
            }}>当前账号</div>
            <div style={{
              fontSize: '14px',
              fontWeight: 600,
              color: 'var(--text-primary)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}>{user.email || user.username}</div>
            {user.username && user.email && user.username !== user.email && (
              <div style={{
                fontSize: '12px',
                color: 'var(--text-tertiary)',
                marginTop: '3px',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}>{user.email}</div>
            )}
          </div>

          {/* 退出按钮 — 3D 立体风格 */}
          <button
            onClick={() => {
              logout()
              setOpen(false)
            }}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
              padding: '10px 14px',
              borderRadius: '10px',
              border: '1px solid transparent',
              background: 'linear-gradient(180deg, color-mix(in srgb, var(--card-bg-solid) 95%, #fff) 0%, var(--card-bg-solid) 100%)',
              cursor: 'pointer',
              fontFamily: 'inherit',
              fontSize: '14px',
              fontWeight: 500,
              color: 'var(--text-secondary)',
              textAlign: 'left',
              boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.3)',
              transition: 'all 0.15s',
            }}
            onMouseEnter={e => {
              e.currentTarget.style.background = 'linear-gradient(180deg, color-mix(in srgb, rgba(200,60,60,0.10) 80%, #fff) 0%, rgba(200,60,60,0.06) 100%)'
              e.currentTarget.style.color = '#c44'
              e.currentTarget.style.borderColor = 'rgba(200,60,60,0.25)'
              e.currentTarget.style.boxShadow = '0 1px 0 rgba(200,60,60,0.15), inset 0 1px 0 rgba(255,255,255,0.3)'
            }}
            onMouseLeave={e => {
              e.currentTarget.style.background = 'linear-gradient(180deg, color-mix(in srgb, var(--card-bg-solid) 95%, #fff) 0%, var(--card-bg-solid) 100%)'
              e.currentTarget.style.color = 'var(--text-secondary)'
              e.currentTarget.style.borderColor = 'transparent'
              e.currentTarget.style.boxShadow = 'inset 0 1px 0 rgba(255,255,255,0.3)'
            }}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M10 12L6 8L10 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
              <path d="M6 8H14M6 8V3C6 2.44772 5.55228 2 5 2H3C2.44772 2 2 2.44772 2 3V13C2 13.5523 2.44772 14 3 14H5C5.55228 14 6 13.5523 6 13V8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
            退出登录
          </button>
        </div>
      )}
    </div>
  )
}
