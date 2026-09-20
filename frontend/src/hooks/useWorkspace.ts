import { useState, useCallback, useEffect, useRef } from 'react'
import {
  fetchFileTree,
  fetchWorkspaceStats,
  uploadFiles as apiUploadFiles,
  createDirectory as apiCreateDirectory,
  deleteFile as apiDeleteFile,
  type FileTreeNode,
  type WorkspaceStats,
} from '../lib/workspace-api'
import { getToken } from '../lib/auth-api'

// Agent 工具循环里 write_file / delete_file / run_python 每轮都会派发
// workspace-refresh，连发时每次都打 tree + stats 两个请求；合并到一个窗口里只刷一次。
const REFRESH_DEBOUNCE_MS = 400

/** 工作区 hook — 管理文件树、上传、删除等操作
 *  未登录时不加载数据，避免 401 错误
 */
export function useWorkspace() {
  const [tree, setTree] = useState<FileTreeNode | null>(null)
  const [stats, setStats] = useState<WorkspaceStats | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  // 登录态单独暴露：面板据此区分"未登录"与"工作区确实为空"，两者文案和操作都不同
  const [loggedIn, setLoggedIn] = useState(() => !!getToken())

  const inflightRef = useRef<Promise<void> | null>(null)
  const timerRef = useRef<number | null>(null)
  const pendingRef = useRef(false)
  const refreshSoonRef = useRef<() => void>(() => {})

  // 刷新文件树和统计 —— 永远发新请求（变更操作和手动刷新要用最新状态）
  const refresh = useCallback(async () => {
    // 未登录时跳过请求
    if (!getToken()) {
      setLoggedIn(false)
      setTree(null)
      setStats(null)
      setLoading(false)
      return
    }
    setLoggedIn(true)
    setLoading(true)
    setError(null)
    const run = (async () => {
      try {
        const [t, s] = await Promise.all([
          fetchFileTree(),
          fetchWorkspaceStats(),
        ])
        setTree(t)
        setStats(s)
      } catch (err) {
        const msg = err instanceof Error ? err.message : '加载工作区失败'
        setError(msg)
      } finally {
        setLoading(false)
        inflightRef.current = null
        // 飞行期间又来过的刷新请求，落地后补一次（此时才拿得到变更后的状态）
        if (pendingRef.current) {
          pendingRef.current = false
          refreshSoonRef.current()
        }
      }
    })()
    inflightRef.current = run
    return run
  }, [])

  // 合并刷新：窗口内多次调用只发一次请求；已有请求在飞时改为标记 pending，
  // 不复用那个（可能早于变更发出的）请求，避免刷完还是旧文件树。
  const refreshSoon = useCallback(() => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    timerRef.current = window.setTimeout(() => {
      timerRef.current = null
      if (inflightRef.current) { pendingRef.current = true; return }
      void refresh()
    }, REFRESH_DEBOUNCE_MS)
  }, [refresh])

  useEffect(() => { refreshSoonRef.current = refreshSoon }, [refreshSoon])

  // 首次加载 — 监听 token 变化，登录后自动刷新
  useEffect(() => {
    refresh()
  }, [refresh])

  // 卸载时清掉未触发的防抖定时器
  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
  }, [])

  // 监听登录/登出事件 + 工具执行后刷新事件
  useEffect(() => {
    // 登录态变化要立刻反映（不防抖），否则刚登录还显示未登录空态
    const onAuth = () => refresh()
    window.addEventListener('storage', onAuth)
    window.addEventListener('auth-change', onAuth)
    // 工具执行后的刷新走防抖合并
    window.addEventListener('workspace-refresh', refreshSoon)
    return () => {
      window.removeEventListener('storage', onAuth)
      window.removeEventListener('auth-change', onAuth)
      window.removeEventListener('workspace-refresh', refreshSoon)
    }
  }, [refresh, refreshSoon])

  // 上传文件
  const upload = useCallback(async (files: File[], basePath: string = '') => {
    if (!files.length) return
    if (!getToken()) {
      setError('请先登录后再上传文件')
      return
    }
    setUploading(true)
    setError(null)
    try {
      const result = await apiUploadFiles(files, basePath)
      // 如果部分文件上传失败，显示错误信息
      if (result.errors && result.errors.length > 0) {
        const successCount = result.uploaded.length
        const failCount = result.errors.length
        setError(`成功 ${successCount} 个，失败 ${failCount} 个: ${result.errors.slice(0, 3).join('; ')}${result.errors.length > 3 ? '...' : ''}`)
      }
      await refresh()
      return result
    } catch (err) {
      const msg = err instanceof Error ? err.message : '上传失败'
      setError(msg)
      throw err
    } finally {
      setUploading(false)
    }
  }, [refresh])

  // 创建目录
  const createDir = useCallback(async (path: string) => {
    setError(null)
    try {
      await apiCreateDirectory(path)
      await refresh()
    } catch (err) {
      const msg = err instanceof Error ? err.message : '创建目录失败'
      setError(msg)
      throw err
    }
  }, [refresh])

  // 删除文件/目录
  const remove = useCallback(async (path: string) => {
    setError(null)
    try {
      await apiDeleteFile(path)
      await refresh()
    } catch (err) {
      const msg = err instanceof Error ? err.message : '删除失败'
      setError(msg)
      throw err
    }
  }, [refresh])

  // 把面板内部被吞掉的失败送进统一错误条（目录上传失败、空目录创建失败等）
  const showError = useCallback((msg: string) => setError(msg), [])

  return {
    tree,
    stats,
    loading,
    uploading,
    error,
    loggedIn,
    refresh,
    refreshSoon,
    showError,
    upload,
    createDir,
    remove,
  }
}
