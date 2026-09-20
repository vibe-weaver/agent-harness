import { createRouter, createRoute, createRootRoute } from '@tanstack/react-router'
import { AIChat } from '@/pages/AIChat'

// 注意：本项目的页面是**全屏独立布局**，不共用博客那套 Layout/Navbar/Footer，
// 所以根路由不挂 component（原博客项目在这里挂了 Layout）。
const rootRoute = createRootRoute()

// 对话是核心功能，放在首页（生图页已移除，生图能力由 media-router 技能提供）
const chatRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  component: AIChat,
})

const routeTree = rootRoute.addChildren([chatRoute])

export const router = createRouter({ routeTree })

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router
  }
}
