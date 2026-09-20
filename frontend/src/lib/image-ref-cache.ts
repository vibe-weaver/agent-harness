import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchWorkspaceRawDataUrl } from './workspace-api'

// ════════════════════════════════════════
//  聊天图片存档缩略图缓存（优化9）
// ════════════════════════════════════════
//
// 图片的 dataUrl 刻意不写进 localStorage（几百 KB 一条，几条就撑爆 shard），
// 刷新后气泡里只剩一个通用文件图标。存档到工作区之后，消息里保留一个 ~40 字符的
// 相对路径 ref，渲染时按需取回字节 —— 于是需要一层缓存来兜住三件事：
//   1. 同一张图被多条消息引用（重发/引用）→ in-flight Promise 去重，只请求一次；
//   2. 存档失败或 TTL 回收后 ref 悬空 → 负缓存，避免每次重渲染打一轮 404 风暴；
//   3. 长会话一屏几十张图 → IntersectionObserver 懒加载，只取进视口的。
//
// 存 data URL 而不是 blob URL：缩略图会被多个组件实例共享，blob URL 无法安全
// revoke（一个卡片卸载就弄坏其它卡片），data URL 交给 GC 自动回收。

const MISSING = 'missing' as const
type Entry = string | typeof MISSING

const MAX_ENTRIES = 40
// 张数上限挡不住极端情况：40 张 4MB 照片的 base64 约 213MB，足以崩掉移动端标签页
const MAX_CACHE_BYTES = 24 * 1024 * 1024

const cache = new Map<string, Entry>()
const inFlight = new Map<string, Promise<string | null>>()
let cacheBytes = 0

function put(path: string, value: Entry) {
  const prev = cache.get(path)
  if (prev !== undefined) cacheBytes -= prev.length
  cache.delete(path)
  cache.set(path, value)
  cacheBytes += value.length
  // Map 迭代顺序即插入顺序，队首就是最久未使用的那条
  while (cache.size > MAX_ENTRIES || cacheBytes > MAX_CACHE_BYTES) {
    const oldest = cache.keys().next().value as string | undefined
    if (oldest === undefined) break
    cacheBytes -= (cache.get(oldest)?.length ?? 0)
    cache.delete(oldest)
  }
}

/** 命中即移到最近使用端；未缓存与「已知不存在」都返回 undefined 之外的区分值 */
function peek(path: string): Entry | undefined {
  const value = cache.get(path)
  if (value === undefined) return undefined
  cache.delete(path)
  cache.set(path, value)
  return value
}

function load(path: string): Promise<string | null> {
  const pending = inFlight.get(path)
  if (pending) return pending
  const task = fetchWorkspaceRawDataUrl(path)
    .then((dataUrl) => {
      put(path, dataUrl)
      return dataUrl
    })
    .catch(() => {
      // 404/401/网络错误一律负缓存：ref 悬空是存档失败或 TTL 回收的正常结果，
      // 静默回落到文件图标即可，不值得每次重渲染都再试一遍
      put(path, MISSING)
      return null
    })
    .finally(() => {
      inFlight.delete(path)
    })
  inFlight.set(path, task)
  return task
}

/**
 * 存档成功后作废负缓存。
 *
 * 路径是内容寻址的：同一张图先前存档失败被负缓存过，用户重贴一次就成功了，
 * 不作废的话这一整个页面生命周期里它都只会显示图标。
 */
export function invalidateChatImageRef(path: string) {
  const prev = cache.get(path)
  if (prev !== undefined) cacheBytes -= prev.length
  cache.delete(path)
}

/**
 * 取回存档图片的 data URL；无 ref、未进视口或已确认不存在时返回 undefined。
 *
 * 返回的 containerRef 要挂到卡片容器上 —— 长会话里一屏可能有几十张图，
 * 不懒加载会在切进会话的瞬间拉十几 MB。
 */
export function useImageRef(path?: string): { src: string | undefined; containerRef: (el: HTMLElement | null) => void } {
  // 惰性初始化直接吃缓存：useEffect 在 paint 之后才跑，不这样的话切回会话时
  // 会先渲染一帧文件图标再闪成缩略图
  const [src, setSrc] = useState<string | undefined>(() => {
    const hit = path ? peek(path) : undefined
    return hit !== undefined && hit !== MISSING ? hit : undefined
  })
  const [visible, setVisible] = useState(false)
  const observerRef = useRef<IntersectionObserver | null>(null)

  const containerRef = useCallback((el: HTMLElement | null) => {
    observerRef.current?.disconnect()
    observerRef.current = null
    if (!el) return
    if (typeof IntersectionObserver === 'undefined') {
      setVisible(true)
      return
    }
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setVisible(true)
          io.disconnect()
          observerRef.current = null
        }
      },
      { rootMargin: '200px' },
    )
    io.observe(el)
    observerRef.current = io
  }, [])

  useEffect(() => () => { observerRef.current?.disconnect() }, [])

  useEffect(() => {
    if (!path) {
      setSrc(undefined)
      return
    }
    const hit = peek(path)
    if (hit !== undefined) {
      setSrc(hit === MISSING ? undefined : hit)
      return
    }
    if (!visible) return
    let cancelled = false
    load(path).then((dataUrl) => {
      // 卸载/切会话后不再 setState；请求本身继续跑完，好把结果留给共享缓存
      if (!cancelled && dataUrl) setSrc(dataUrl)
    })
    return () => { cancelled = true }
  }, [path, visible])

  return { src, containerRef }
}
