import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        // Agent 长任务（多轮工具调用）可能需要数分钟，设置足够长的超时
        timeout: 180000,
        proxyTimeout: 180000,
        // SSE 流式响应需要禁用缓冲
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            proxyRes.headers['cache-control'] = 'no-cache'
            proxyRes.headers['x-accel-buffering'] = 'no'
          })
        },
      },
      '/static': 'http://localhost:8000',
      '/admin': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        // 管理页面是后端渲染的原生 HTML，需跳过 Vite 的 SPA 处理
        configure: (proxy) => {
          proxy.on('proxyRes', (proxyRes) => {
            delete proxyRes.headers['content-type'];
            proxyRes.headers['content-type'] = 'text/html; charset=utf-8';
          });
        },
      },
    },
  },
})
