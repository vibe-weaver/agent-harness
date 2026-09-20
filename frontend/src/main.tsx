import { Component, StrictMode, type ReactNode } from 'react'
import { createRoot } from 'react-dom/client'
import './styles/globals.css'
import App from './App'

// 全局错误边界：任何渲染错误显示具体信息（不再白屏/卡死），便于定位问题
class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) {
    return { error }
  }
  render() {
    if (this.state.error) {
      const err = this.state.error
      return (
        <div style={{ padding: 40, fontFamily: 'system-ui, sans-serif', minHeight: '100vh' }}>
          <h2 style={{ color: '#c44', marginBottom: 12 }}>页面出错了（错误信息如下，可复制发给开发者）</h2>
          <p style={{ color: '#333', fontSize: 14, lineHeight: '22px' }}>{String(err.message || err)}</p>
          <pre style={{ background: '#f5f5f5', padding: 12, borderRadius: 8, fontSize: 12, overflow: 'auto', whiteSpace: 'pre-wrap', marginTop: 12 }}>{String(err.stack || '')}</pre>
          <button type="button" onClick={() => window.location.reload()} style={{ marginTop: 16, padding: '8px 20px', borderRadius: 8, border: 'none', background: '#2563eb', color: '#fff', cursor: 'pointer', fontSize: 14 }}>刷新页面</button>
        </div>
      )
    }
    return this.props.children
  }
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
