/**
 * 设计系统 — 第一期"去装饰化"的共享样式常量。
 *
 * 背景：AI 相关页面（AI.tsx / AIChat.tsx）原先把同一套"180deg 渐变 +
 * 3~5 层 box-shadow + 3px 底部立体棱 + 内高光"的配方内联手写了几十遍。
 * 大厂范式是**平面、单层阴影、内容优先**——本模块就是收敛后的配方：
 *
 *   SHADOW_SM / SHADOW_MD   全站唯二的阴影（卡片用 SM，浮层用 MD）
 *   card                    平面卡片底色（var(--card-bg-solid) + 1px line + SM）
 *   cardInteractive         可点卡片：hover 仅抬升 2px + 换 MD
 *   btnPrimary / btnGhost   主按钮（accent 实底）/ 次级按钮（幽灵线框）
 *   chip / chipActive       小胶囊（示例词、风格预设）
 *   iconBtn                 方形图标按钮
 *   skeleton                骨架屏 shimmer（加载占位，替代装饰性旋转/扫光动画）
 *
 * 刻意做成 JS 常量而不是 CSS 类：代码库本身就是内联样式风格，常量类型安全、
 * 可被 tree-shake，且与既有 `style={{...}}` 写法无缝衔接。
 * 颜色一律走 globals.css 的 CSS 变量，暗色模式自动跟随，不要在这里写死色值。
 */
import type { CSSProperties } from 'react'

export const SHADOW_SM = '0 1px 2px rgba(0,0,0,0.06)'
export const SHADOW_MD = '0 4px 16px rgba(0,0,0,0.10)'

/** 平面卡片：一层阴影、1px 边线、无渐变 */
export const card: CSSProperties = {
  background: 'var(--card-bg-solid)',
  border: '1px solid var(--line)',
  borderRadius: 14,
  boxShadow: SHADOW_SM,
}

/** 可交互卡片的 hover 抬升（配合 onMouseEnter/Leave 切 boxShadow/transform） */
export const cardHoverShadow = SHADOW_MD

export function btnPrimary(disabled: boolean): CSSProperties {
  return {
    padding: '10px 24px',
    borderRadius: 10,
    border: 'none',
    fontSize: 14,
    fontWeight: 600,
    fontFamily: 'inherit',
    cursor: disabled ? 'default' : 'pointer',
    background: disabled ? 'var(--line)' : 'var(--accent)',
    color: disabled ? 'var(--text-tertiary)' : '#fff',
    boxShadow: disabled ? 'none' : SHADOW_SM,
    transition: 'background 0.15s ease, transform 0.15s ease',
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
  }
}

export function btnGhost(disabled = false): CSSProperties {
  return {
    padding: '7px 14px',
    borderRadius: 8,
    border: '1px solid var(--line)',
    fontSize: 13,
    fontWeight: 500,
    fontFamily: 'inherit',
    cursor: disabled ? 'default' : 'pointer',
    background: 'var(--card-bg-solid)',
    color: disabled ? 'var(--text-tertiary)' : 'var(--text-secondary)',
    transition: 'border-color 0.15s ease, color 0.15s ease, background 0.15s ease',
    display: 'inline-flex',
    alignItems: 'center',
    gap: 5,
  }
}

export function chip(active: boolean): CSSProperties {
  return {
    padding: '4px 12px',
    fontSize: 12,
    borderRadius: 999,
    fontFamily: 'inherit',
    cursor: 'pointer',
    border: `1px solid ${active ? 'var(--accent)' : 'var(--line)'}`,
    background: active ? 'var(--accent)' : 'var(--card-bg-solid)',
    color: active ? '#fff' : 'var(--text-secondary)',
    transition: 'all 0.15s ease',
  }
}

/** 方形图标按钮（工具栏、关闭、复制等） */
export function iconBtn(disabled = false): CSSProperties {
  return {
    display: 'grid',
    placeItems: 'center',
    width: 30,
    height: 30,
    borderRadius: 8,
    border: '1px solid var(--line)',
    background: 'var(--card-bg-solid)',
    color: disabled ? 'var(--text-tertiary)' : 'var(--text-secondary)',
    cursor: disabled ? 'default' : 'pointer',
    transition: 'color 0.15s ease, border-color 0.15s ease',
    flex: 'none',
  }
}

/** 骨架屏占位（CSS keyframes 在 globals.css 的 skeleton-shimmer） */
export function skeleton(width: CSSProperties['width'], height: CSSProperties['height'], radius = 8): CSSProperties {
  return {
    width,
    height,
    borderRadius: radius,
    background: 'linear-gradient(90deg, var(--code-bg) 25%, var(--surface-light) 50%, var(--code-bg) 75%)',
    backgroundSize: '400% 100%',
    animation: 'skeleton-shimmer 1.4s ease infinite',
  }
}
