/**
 * 工作区面板 — 照搬 DSH 桌面端 WorkspaceBrowser 布局
 *
 * DSH 设计要点（WorkspaceBrowser.module.css + Rows.module.css）：
 * - sectionHeader: 标题 + 搜索胶囊 + 尾部操作（add 按钮）
 * - listArea: 唯一滚动区域，底部 fade 渐变遮罩
 * - ProjectRow: 34px 高，folder/chevron hover 切换（纯CSS），标题，hover 操作
 * - SessionNodeItem: 32px 高，icon + 标题 + size（hover→删除按钮）
 * - CSS 变量: --dsw-alias-label-* / --dsw-alias-interactive-bg-hover
 * - 所有 hover 切换都是纯 CSS，不用 JS state
 *
 * Web 适配：把桌面端的本地目录选择器替换为浏览器目录上传
 */

import { useState, useCallback, useRef, memo, useMemo, useEffect } from 'react'
import {
  FileText, FileCode, FileImage, FileSpreadsheet, File as FileIconLucide,
  Folder, FolderOpen, ChevronRight, Copy, Download, Trash2, ExternalLink,
  type LucideIcon,
} from 'lucide-react'
import { useWorkspace } from '@/hooks/useWorkspace'
import type { FileTreeNode } from '@/lib/workspace-api'
import { downloadWorkspace, createDirectory, readFile, listSnapshots, restoreSnapshot, fetchWorkspaceRawDataUrl, type SnapshotVersion } from '@/lib/workspace-api'
import { highlightCode, langFromName } from '@/lib/hljs-setup'
import { MarkdownRenderer } from '@/components/MarkdownRenderer'

// ════════════════════════════════════════
//  工具函数
// ════════════════════════════════════════

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// 与后端 workspace_service.py 配额一致的前置校验常量
const MAX_SINGLE_FILE = 50 * 1024 * 1024   // 单文件 50MB
const PREVIEW_MAX_BYTES = 1024 * 1024      // 预览截断阈值 1MB
const SEARCH_DEBOUNCE_MS = 200             // 搜索过滤防抖（输入框仍即时回显）

// 未登录空态的"登录"按钮用：面板自身不持有 AuthModal，派发事件请宿主页面弹窗
export const REQUEST_LOGIN_EVENT = 'workspace-request-login'

// #1 工具卡↔文件双向跳转的自定义事件：
// - LOCATE：对话工具卡点"打开此文件" → 宿主页面切到工作区 tab；本面板监听后展开祖先目录、
//   打开预览、滚动到该文件行并闪烁高亮。detail: { path }
export const WORKSPACE_LOCATE_EVENT = 'workspace-locate-file'
// - REFERENCE：工作区文件行点"引用到对话" → 宿主页面把路径插入聊天输入框。detail: { path }
export const WORKSPACE_REFERENCE_EVENT = 'workspace-reference-file'
// #6 工作区内部的 alert → toast：本面板不持有 toast，派发事件请宿主页面弹非阻塞提示。
// detail: { msg: string; icon?: 'warn'|'info'; title?: string }
export const WORKSPACE_TOAST_EVENT = 'workspace-toast'

/** #4 预览按类型分流：按扩展名决定渲染方式（图片/HTML/SVG/Markdown/代码高亮/纯文本） */
type PreviewKind = 'text' | 'code' | 'image' | 'md' | 'html' | 'svg'
function previewKindOf(name: string): PreviewKind {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico'].includes(ext)) return 'image'
  if (ext === 'html' || ext === 'htm') return 'html'
  if (ext === 'svg') return 'svg'
  if (ext === 'md' || ext === 'markdown') return 'md'
  if (langFromName(name)) return 'code'
  return 'text'
}

// #5 文件图标 lucide 化：按扩展名选图标（与 AI 页 lucide 化一致，专业感统一）
function iconForFile(name: string): LucideIcon {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  if (['md', 'markdown', 'txt', 'log'].includes(ext)) return FileText
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'svg'].includes(ext)) return FileImage
  if (['csv', 'xls', 'xlsx'].includes(ext)) return FileSpreadsheet
  if (['pdf'].includes(ext)) return FileText
  if (['json', 'yaml', 'yml', 'xml', 'html', 'htm', 'css', 'js', 'mjs', 'cjs',
       'ts', 'tsx', 'jsx', 'cts', 'py', 'rs', 'go', 'java', 'c', 'cpp', 'cc', 'cxx',
       'h', 'hpp', 'sql', 'sh', 'bash', 'zsh', 'vue'].includes(ext)) return FileCode
  return FileIconLucide
}

/** 文件行图标（统一 14px，与行高协调） */
function FileIcon({ name }: { name: string }) {
  const Icon = iconForFile(name)
  return <Icon size={14} strokeWidth={1.6} aria-hidden style={{ color: 'var(--text-tertiary)', verticalAlign: '-2px' }} />
}

// ════════════════════════════════════════
//  TreeNode — 对应 DSH ProjectRowItem + SessionNodeItem
//  ProjectRow: 34px, folder/chevron 纯CSS hover 切换, 标题, hover 操作
//  FileRow (leaf): 32px, icon + 标题 + size, hover→删除按钮
//  所有 hover 效果都是纯 CSS（.projectRow:hover .folder/.chevron/.rowActions）
// ════════════════════════════════════════

interface TreeNodeProps {
  node: FileTreeNode
  depth: number
  onDelete: (path: string, isDir: boolean) => void
  onPreview: (node: FileTreeNode) => void
  isPathExpanded: (path: string, depth: number) => boolean
  onTogglePath: (path: string, depth: number) => void
  // 本轮对话里被 AI 改动过的文件：在文件名前点一个 accent 圆点，无需展开树也能看到"刚改了啥"
  isTouched: (path: string) => boolean
  // #6 删除确认条：当前处于"待确认删除"的路径（同一路径再次点击 = 确认删除）
  deleteTarget: string | null
  // #5 右键菜单：文件/目录行右键唤出操作菜单（打开/复制路径/下载/删除）
  onContextMenu?: (node: FileTreeNode, x: number, y: number) => void
}

const INDENT_STEP = 22 // DSH: 16px slot + 6px gap = 22px indent step

const TreeNode = memo(function TreeNode({ node, depth, onDelete, onPreview, isPathExpanded, onTogglePath, isTouched, deleteTarget, onContextMenu }: TreeNodeProps) {
  const isDir = node.type === 'directory'
  const padLeft = 8 + depth * INDENT_STEP
  const pathKey = node.path || node.name
  // 每个节点用自己的路径计算展开状态（子目录可独立展开/折叠，
  // 修复：此前把父节点的 expanded 传给所有子节点，导致子层级不受控）
  const expanded = isPathExpanded(pathKey, depth)
  // #6 删除确认条：该节点是否处于"待确认删除"
  const armed = deleteTarget === pathKey
  const dirArmed = isDir && armed
  // #5 右键唤出操作菜单
  const handleCtx = useCallback((e: React.MouseEvent) => {
    if (onContextMenu) { e.preventDefault(); e.stopPropagation(); onContextMenu(node, e.clientX, e.clientY) }
  }, [onContextMenu, node])

  // ── Directory row: 对应 DSH ProjectRowItem (34px) ──
  if (isDir) {
    return (
      <div className="ws-group-section">
        <div
          className="ws-project-row"
          role="treeitem"
          aria-expanded={expanded}
          onClick={() => onTogglePath(pathKey, depth)}
          onContextMenu={handleCtx}
          style={{ paddingLeft: padLeft }}
        >
          {/* folder — 默认显示, hover 时 CSS 隐藏（.ws-slot 已自带 inline-flex 居中，勿加内联 display 否则顶掉 hover:none） */}
          <span className="ws-slot ws-folder">
            {expanded ? <FolderOpen size={14} strokeWidth={1.6} aria-hidden style={{ color: 'var(--accent)' }} /> : <Folder size={14} strokeWidth={1.6} aria-hidden style={{ color: 'var(--text-tertiary)' }} />}
          </span>
          {/* chevron — 默认 CSS 隐藏, hover 时显示；展开时旋转 90°（.ws-arrow 已带 transition） */}
          <span className="ws-slot ws-chevron">
            <ChevronRight size={12} strokeWidth={1.75} aria-hidden className="ws-arrow" style={{ transform: expanded ? 'rotate(90deg)' : 'none' }} />
          </span>
          <span className="ws-project-text">
            <span className="ws-title">{node.name}</span>
          </span>
          {/* hover 操作 — CSS: .ws-project-row:hover .ws-row-actions → display:inline-flex */}
          <span className="ws-row-actions">
            <button
              type="button"
              className="ws-icon-btn"
              aria-label={dirArmed ? '确认删除目录' : '删除目录'}
              title={dirArmed ? '再次点击确认删除' : '删除目录'}
              onClick={(e) => { e.stopPropagation(); onDelete(node.path || node.name, true) }}
              style={dirArmed ? { color: '#ef4444' } : undefined}
            >
              {dirArmed ? (
                <span style={{ fontSize: 11, fontWeight: 600 }}>确认?</span>
              ) : (
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 3.5h10M5 3.5V2h4v1.5M4 3.5v7h6v-7" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" /></svg>
              )}
            </button>
          </span>
        </div>
        {expanded && node.children && (
          <div className="ws-children">
            {node.children.map((child, i) => (
              <TreeNode key={`${child.path || child.name}-${i}`} node={child} depth={depth + 1} onDelete={onDelete} onPreview={onPreview} isPathExpanded={isPathExpanded} onTogglePath={onTogglePath} isTouched={isTouched} deleteTarget={deleteTarget} onContextMenu={onContextMenu} />
            ))}
          </div>
        )}
      </div>
    )
  }

  // ── File row: 对应 DSH SessionNodeItem (32px) ──
  const sizeStr = node.size !== undefined ? formatSize(node.size) : ''
  return (
    <div
      className="ws-session-row"
      role="treeitem"
      data-ws-path={node.path || node.name}
      onClick={() => onPreview(node)}
      onContextMenu={handleCtx}
      title={`点击预览 ${node.name}`}
      style={{ paddingLeft: padLeft }}
    >
      <span className="ws-slot">
        <FileIcon name={node.name} />
      </span>
      <span className="ws-title">{node.name}</span>
      {/* 本轮对话被 AI 改动过：文件名前点一个 accent 圆点，无需展开也能看到"刚改了啥" */}
      {node.path && isTouched(node.path) && (
        <span aria-hidden style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--accent)', flex: 'none', boxShadow: '0 0 4px color-mix(in srgb, var(--accent) 50%, transparent)' }} />
      )}
      {/* size — CSS: .ws-session-row:hover .ws-time → display:none */}
      {sizeStr && <span className="ws-time">{sizeStr}</span>}
      {/* hover 操作 — CSS: .ws-session-row:hover .ws-row-actions → display:inline-flex */}
      <span className="ws-row-actions">
        {/* #1 工具卡↔文件跳转（反向）：把文件路径插入对话输入框，接着问 AI */}
        {node.path && (
          <button
            type="button"
            className="ws-icon-btn"
            aria-label="引用到对话"
            title="引用到对话：把文件路径插入输入框"
            onClick={(e) => {
              e.stopPropagation()
              window.dispatchEvent(new CustomEvent(WORKSPACE_REFERENCE_EVENT, { detail: { path: node.path } }))
            }}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2.5 3h9v6.5H6l-2.5 2.5v-2.5h-1z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" /></svg>
          </button>
        )}
        <button
          type="button"
          className="ws-icon-btn"
          aria-label={armed ? '确认删除文件' : '删除文件'}
          title={armed ? '再次点击确认删除' : '删除文件'}
          onClick={(e) => { e.stopPropagation(); onDelete(node.path || node.name, false) }}
          style={armed ? { color: '#ef4444' } : undefined}
        >
          {armed ? (
            <span style={{ fontSize: 11, fontWeight: 600 }}>确认?</span>
          ) : (
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M2 3.5h10M5 3.5V2h4v1.5M4 3.5v7h6v-7" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" /></svg>
          )}
        </button>
      </span>
    </div>
  )
})

// ════════════════════════════════════════
//  WorkspacePanel — 对应 DSH WorkspaceBrowser
//  布局: sectionHeader(标题+搜索+添加) → listArea(滚动列表+底部fade)
// ════════════════════════════════════════

export interface WorkspacePanelProps {
  // 工作区上下文注入由会话模式决定（Agent 办公模式固定注入），不再提供独立开关
}

// memo：本组件不接收 props，而父组件 AIChat 在流式输出期间每个 token 都会重渲染。
// 包一层 memo 后，父级重渲染不再传导到文件树（树的变化由内部 useWorkspace 状态驱动）。
export const WorkspacePanel = memo(function WorkspacePanel(_props: WorkspacePanelProps) {
  const { tree, stats, loading, uploading, error, loggedIn, upload, remove, refresh, showError } = useWorkspace()
  const dirInputRef = useRef<HTMLInputElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const addBtnRef = useRef<HTMLButtonElement>(null)
  const [dragOver, setDragOver] = useState(false)
  const [query, setQuery] = useState('')
  const [searchExpanded, setSearchExpanded] = useState(false)
  const [addMenuOpen, setAddMenuOpen] = useState(false)
  const [menuPos, setMenuPos] = useState({ top: 0, right: 0 })
  const searchInput = useRef<HTMLInputElement>(null)

  // #6 非阻塞提示：原 alert 改派发 toast 事件（由 AIChat 宿主弹 toast3D）
  const notify = useCallback((msg: string, icon: 'warn' | 'info' = 'warn', title = '操作提示') => {
    window.dispatchEvent(new CustomEvent(WORKSPACE_TOAST_EVENT, { detail: { msg, icon, title } }))
  }, [])
  // #6 删除确认条：同一文件再次点击 = 确认（取代 confirm 弹窗）；3 秒未确认自动取消
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null)
  // #5 右键菜单：文件/目录行右键唤出操作菜单
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; node: FileTreeNode } | null>(null)
  const openCtxMenu = useCallback((node: FileTreeNode, x: number, y: number) => {
    setCtxMenu({ x, y, node })
  }, [])
  const closeCtxMenu = useCallback(() => setCtxMenu(null), [])

  // #5 单文件下载（文本走 readFile→blob，图/SVG 走 raw data URL→blob；PDF/Word 等二进制文档不在此项，走"下载工作区"）
  const downloadSingle = useCallback(async (node: FileTreeNode) => {
    if (!node.path) return
    try {
      const ext = node.name.split('.').pop()?.toLowerCase() || ''
      const isImage = ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'svg'].includes(ext)
      let blob: Blob
      if (isImage) {
        const dataUrl = await fetchWorkspaceRawDataUrl(node.path)
        const res = await fetch(dataUrl)
        blob = await res.blob()
      } else {
        const res = await readFile(node.path)
        blob = new Blob([res.content], { type: 'text/plain;charset=utf-8' })
      }
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = node.name
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 100)
    } catch (err) {
      notify(err instanceof Error ? err.message : '下载失败')
    }
  }, [notify])

  // ── 目录树受控展开：双集合模型 ──
  // expandedPaths：显式展开的目录（含目录上传后自动展开的层级）
  // collapsedPaths：显式折叠的目录（顶层目录默认展开，除非被显式折叠）
  const [expandedPaths, setExpandedPaths] = useState<Set<string>>(() => new Set())
  const [collapsedPaths, setCollapsedPaths] = useState<Set<string>>(() => new Set())

  const isPathExpanded = useCallback((path: string, depth: number): boolean => {
    // 显式折叠优先：用户主动折叠过的目录保持折叠（即使曾被 expandDirs 加入展开集合）
    if (collapsedPaths.has(path)) return false
    if (expandedPaths.has(path)) return true
    // 顶层目录默认展开（除非被显式折叠）；子目录默认折叠（除非显式展开）
    if (depth < 1) return true
    return false
  }, [expandedPaths, collapsedPaths])

  const togglePath = useCallback((path: string, depth: number) => {
    const currentlyExpanded = isPathExpanded(path, depth)
    if (currentlyExpanded) {
      // 当前展开 → 折叠
      if (depth < 1) {
        setCollapsedPaths(prev => new Set(prev).add(path))
      } else {
        setExpandedPaths(prev => { const n = new Set(prev); n.delete(path); return n })
      }
    } else {
      // 当前折叠 → 展开
      if (depth < 1) {
        setCollapsedPaths(prev => { const n = new Set(prev); n.delete(path); return n })
      } else {
        setExpandedPaths(prev => new Set(prev).add(path))
      }
    }
  }, [isPathExpanded])

  /** 上传目录后展开其所有层级，让文件立即可见 */
  const expandDirs = useCallback((dirPaths: string[]) => {
    if (dirPaths.length === 0) return
    setExpandedPaths(prev => {
      const next = new Set(prev)
      dirPaths.forEach(p => next.add(p))
      return next
    })
    // 同时移除这些目录的折叠标记，确保展开
    setCollapsedPaths(prev => {
      const next = new Set(prev)
      dirPaths.forEach(p => next.delete(p))
      return next
    })
  }, [])

  /** 上传前前置校验（与后端配额一致，尽早拦截，后端仍会精确校验） */
  const validateUpload = useCallback((files: File[]): string | null => {
    if (files.length === 0) return null
    const oversized = files.find(f => f.size > MAX_SINGLE_FILE)
    if (oversized) {
      return `文件「${oversized.name}」超过单文件上限（50MB）`
    }
    if (stats) {
      if (files.length > stats.remaining_files) {
        return `文件数量超出剩余配额（还可上传 ${stats.remaining_files} 个；覆盖同名文件不占新配额）`
      }
      const total = files.reduce((s, f) => s + f.size, 0)
      if (total > stats.remaining_size) {
        return `总大小超出剩余配额（还可上传 ${formatSize(stats.remaining_size)}；覆盖同名文件不占新配额）`
      }
    }
    return null
  }, [stats])

  // 目录上传 input — 用 setAttribute 确保 webkitdirectory 可靠生效
  // （React JSX 属性传递对 webkit 前缀属性存在兼容差异，直接 setAttribute 最稳）
  useEffect(() => {
    if (dirInputRef.current) {
      dirInputRef.current.setAttribute('webkitdirectory', '')
      dirInputRef.current.setAttribute('directory', '')
    }
  }, [])

  // 过滤文件树 — 对应 DSH 的 deriveSearchResults
  // 用防抖后的 query：输入框即时回显，但递归过滤整棵树只在停手后做一次
  const [debouncedQuery, setDebouncedQuery] = useState('')
  useEffect(() => {
    const t = window.setTimeout(() => setDebouncedQuery(query), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(t)
  }, [query])

  const filteredTree = useMemo(() => {
    if (!debouncedQuery.trim() || !tree) return tree
    const q = debouncedQuery.toLowerCase()
    const filterNode = (node: FileTreeNode): FileTreeNode | null => {
      if (node.name.toLowerCase().includes(q)) return node
      if (node.children) {
        const kids = node.children.map(filterNode).filter(Boolean) as FileTreeNode[]
        if (kids.length > 0) return { ...node, children: kids }
      }
      return null
    }
    if (!tree.children) return tree
    return { ...tree, children: tree.children.map(filterNode).filter(Boolean) as FileTreeNode[] }
  }, [tree, debouncedQuery])

  // ── "最近编辑"分区：本轮对话里 AI 改动过的文件 ──
  // 思路：diff 连续两次树快照的 updated_at（后端 write/edit/rename 落库时更新）。
  // 变过/新增的路径累积进 touchedPaths；删掉的路径从集合里移除（已不在树里）。
  // 切换/新建会话时把 baseline 标脏：下一次刷新只重建 prevMap 不计触达，
  // 否则会把"切换到的会话已有的全部文件"误判成本轮改动。
  const fileIndex = useMemo(() => {
    const idx = new Map<string, FileTreeNode>()
    if (tree) {
      const walk = (n: FileTreeNode) => {
        if (n.type === 'file' && n.path) idx.set(n.path, n)
        if (n.children) n.children.forEach(walk)
      }
      tree.children?.forEach(walk)
    }
    return idx
  }, [tree])

  const prevFileMapRef = useRef<Map<string, string>>(new Map())
  // 首次加载 baseline 标脏：第一次拿到树时只建立 prevMap、不计触达，
  // 否则空 prevMap 会让"会话已有的全部文件"全被标记成本轮改动。
  const baselineDirtyRef = useRef(true)
  const [touchedPaths, setTouchedPaths] = useState<Set<string>>(() => new Set())

  useEffect(() => {
    if (!tree) return
    const cur = new Map<string, string>()
    fileIndex.forEach((node, path) => { cur.set(path, node.updated_at || '') })
    if (baselineDirtyRef.current) {
      // 会话切换/新建：重建 baseline，不计触达，清空旧集合
      prevFileMapRef.current = cur
      baselineDirtyRef.current = false
      setTouchedPaths(new Set())
      return
    }
    const prev = prevFileMapRef.current
    const next = new Set(touchedPaths)
    let changed = false
    // 新增 / 改动 → 加入
    cur.forEach((ts, path) => {
      if (!prev.has(path) || prev.get(path) !== ts) {
        if (!next.has(path)) { next.add(path); changed = true }
      }
    })
    // 删除 → 移除（文件不在树里了）
    next.forEach(path => { if (!cur.has(path)) { next.delete(path); changed = true } })
    prevFileMapRef.current = cur
    if (changed) setTouchedPaths(next)
  // 依赖 fileIndex（树快照），不能依赖 touchedPaths 否则自循环
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fileIndex])

  // 会话切换/新建 → baseline 标脏（监听 useChat 派发的 chat-session-changed）
  useEffect(() => {
    const onSessionChanged = () => { baselineDirtyRef.current = true }
    window.addEventListener('chat-session-changed', onSessionChanged)
    return () => window.removeEventListener('chat-session-changed', onSessionChanged)
  }, [])

  const isTouched = useCallback((path: string) => touchedPaths.has(path), [touchedPaths])
  const clearTouched = useCallback(() => setTouchedPaths(new Set()), [])
  // touched 列表：按 path 取节点（删了的已不在 fileIndex，自然不显示）
  const touchedNodes = useMemo(() => {
    if (touchedPaths.size === 0) return []
    const out: FileTreeNode[] = []
    touchedPaths.forEach(p => { const n = fileIndex.get(p); if (n) out.push(n) })
    // 最近的在前（updated_at 倒序；无 updated_at 的兜底排后）
    return out.sort((a, b) => (b.updated_at || '').localeCompare(a.updated_at || ''))
  }, [touchedPaths, fileIndex])


  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragOver(false)
    const files = e.dataTransfer.files
    if (files && files.length > 0) {
      const fileArr = Array.from(files)
      const errMsg = validateUpload(fileArr)
      if (errMsg) { notify(errMsg); return }
      upload(fileArr).catch(() => {})
    }
  }, [upload, validateUpload])

  // ── 选择目录上传：目录内所有文件（含子目录）一起上传，保留目录结构 ──
  const handleDirSelect = useCallback(async (e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    const fileArr = files && files.length > 0 ? Array.from(files) : []
    if (e.target) e.target.value = ''

    // 浏览器 webkitdirectory 不返回空目录中的文件；尝试从 value 提取目录名创建
    if (fileArr.length === 0) {
      const raw = e.target.value || ''
      const dirName = raw.replace(/\\/g, '/').split('/').filter(Boolean).pop()
      if (dirName) {
        // 空目录兜底：此前失败被静默吞掉，用户只看到"选了目录但什么都没发生"
        try {
          await createDirectory(dirName)
          refresh()
        } catch (err) {
          const msg = err instanceof Error ? err.message : String(err)
          console.error('[WorkspacePanel] 创建空目录失败:', msg)
          showError(msg)
        }
      }
      return
    }

    // 上传前校验：单文件大小 / 数量 / 总量（尽早拦截，后端仍会精确校验）
    const errMsg = validateUpload(fileArr)
    if (errMsg) { alert(errMsg); return }

    // 从第一个文件的 webkitRelativePath 提取目录层级（上传后自动展开）
    const firstFile = fileArr[0] as File & { webkitRelativePath?: string }
    const relPath = firstFile.webkitRelativePath || ''
    const dirPaths: string[] = []
    if (relPath) {
      const parts = relPath.split('/')
      for (let i = 1; i < parts.length; i++) {
        dirPaths.push(parts.slice(0, i).join('/'))
      }
      // 注意：不在这里 createDirectory —— 目录记录由后端 upload_files_batch
      // 的 _ensure_parent_dirs_batch 自动创建。此前此处循环未 await、与 upload
      // 并发，会撞数据库唯一约束导致整个上传事务回滚（文件记录丢失、目录打不开）。
      // 空目录（浏览器不上传空目录）仍由下方 upload 失败后的 createDirectory 分支处理。
    }

    try {
      await upload(fileArr)
      // 上传成功后展开新目录的所有层级，文件立即可见
      expandDirs(dirPaths)
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      console.error('[WorkspacePanel] 目录上传失败:', msg)
      showError(msg)
    }
  }, [upload, refresh, expandDirs, validateUpload, showError])

  // 隐藏的文件选择器（多选文件）
  const handleFileSelect = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (files && files.length > 0) {
      const fileArr = Array.from(files)
      const errMsg = validateUpload(fileArr)
      if (errMsg) { notify(errMsg); return }
      upload(fileArr).catch(() => {})
    }
    if (e.target) e.target.value = ''
  }, [upload, validateUpload])

  const openAddMenu = useCallback(() => {
    const btn = addBtnRef.current
    if (btn) {
      const rect = btn.getBoundingClientRect()
      setMenuPos({ top: rect.bottom + 4, right: window.innerWidth - rect.right })
    }
    setAddMenuOpen(true)
  }, [])

  const handleDelete = useCallback((path: string, _isDir: boolean) => {
    // #6 非阻塞删除确认：同一文件再次点击 = 确认删除（取代 confirm 弹窗）
    if (deleteTarget === path) {
      setDeleteTarget(null)
      remove(path).catch(() => {})
      return
    }
    setDeleteTarget(path)
    notify(`再次点击删除按钮以确认删除「${path}」`, 'info', '请确认删除')
    window.setTimeout(() => setDeleteTarget(prev => prev === path ? null : prev), 3000)
  }, [deleteTarget, remove, notify])

  const [downloading, setDownloading] = useState(false)

  const handleDownload = useCallback(async () => {
    if (downloading) return
    setDownloading(true)
    try {
      await downloadWorkspace()
    } catch (err) {
      notify(err instanceof Error ? err.message : '下载失败')
    } finally {
      setDownloading(false)
    }
  }, [downloading, notify])

  // ── 文件预览（#3 历史版本 + #4 按类型分流渲染） ──
  interface PreviewState {
    path: string
    name: string
    kind: PreviewKind
    content: string
    imgUrl: string | null
    loading: boolean
    truncated: boolean
    error: string | null
  }
  const [preview, setPreview] = useState<PreviewState | null>(null)

  const openPreview = useCallback(async (node: FileTreeNode) => {
    if (node.type !== 'file' || !node.path) return
    const kind = previewKindOf(node.name)
    // 切换文件 → 重置历史版本面板（不同文件快照数不同，按需重新拉取）
    setVersions(null)
    setVersionsOpen(false)
    setRestoreTarget(null)
    setPreview({ path: node.path, name: node.name, kind, content: '', imgUrl: null, loading: true, truncated: false, error: null })
    try {
      if (kind === 'image') {
        // 图片走原始字节 → data URL（后端按魔数嗅探真实格式）
        const url = await fetchWorkspaceRawDataUrl(node.path)
        setPreview(prev => prev && prev.path === node.path ? { ...prev, imgUrl: url, loading: false } : prev)
      } else {
        // 截断由服务端做：只传回开头 PREVIEW_MAX_BYTES，不再拉全文到浏览器里 slice
        const res = await readFile(node.path, PREVIEW_MAX_BYTES)
        setPreview(prev => prev && prev.path === node.path
          ? { ...prev, content: res.content, loading: false, truncated: !!res.truncated }
          : prev)
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      setPreview(prev => prev && prev.path === node.path ? { ...prev, loading: false, error: `读取失败：${msg}` } : prev)
    }
  }, [])
  const closePreview = useCallback(() => setPreview(null), [])

  // SVG 预览：内容转 Blob URL 由 <img> 渲染（<img> 不执行 SVG 内脚本，安全）
  const previewSvgUrl = useMemo(() => {
    if (!preview || preview.kind !== 'svg' || !preview.content) return null
    return URL.createObjectURL(new Blob([preview.content], { type: 'image/svg+xml' }))
  }, [preview?.kind, preview?.content])
  useEffect(() => () => { if (previewSvgUrl) URL.revokeObjectURL(previewSvgUrl) }, [previewSvgUrl])

  // ── #3 历史版本：列出快照 + 一键回退 ──
  const [versions, setVersions] = useState<SnapshotVersion[] | null>(null)  // null=未加载
  const [versionsOpen, setVersionsOpen] = useState(false)
  const [restoreTarget, setRestoreTarget] = useState<number | null>(null)   // 正在确认的 steps
  const [restoring, setRestoring] = useState(false)
  const toggleVersions = useCallback(async () => {
    if (!preview) return
    const next = !versionsOpen
    setVersionsOpen(next)
    if (next && versions === null) {
      try { setVersions(await listSnapshots(preview.path)) }
      catch { setVersions([]) }
    }
  }, [preview, versionsOpen, versions])
  const doRestore = useCallback(async (steps: number) => {
    if (!preview) return
    setRestoring(true)
    try {
      await restoreSnapshot(preview.path, steps)
      // 回退改了磁盘+DB：刷新文件树（updated_at/size 变）并重新读预览内容
      void refresh()
      setRestoreTarget(null)
      setVersionsOpen(false)
      setVersions(null)
      await openPreview({ name: preview.name, type: 'file', path: preview.path })
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err)
      window.dispatchEvent(new CustomEvent(WORKSPACE_TOAST_EVENT, { detail: { msg: `回退失败：${msg}`, icon: 'warn', title: '回退失败' } }))
    } finally {
      setRestoring(false)
    }
  }, [preview, refresh, openPreview])

  // ── #1 工具卡→文件定位：展开祖先目录 + 打开预览 + 滚动到文件行并闪烁高亮 ──
  const locateFile = useCallback((path: string) => {
    const target = path.trim()
    if (!target) return
    // 精确匹配优先；兜底按后缀/大小写匹配（agent 与树的路径风格偶有差异）
    let node = fileIndex.get(target)
    if (!node) {
      const lower = target.toLowerCase()
      for (const [p, n] of fileIndex) {
        if (p.toLowerCase() === lower || p.endsWith('/' + target)) { node = n; break }
      }
    }
    if (!node) return
    // 展开所有祖先目录：a/b/c.txt → 展开 a、a/b（并清掉显式折叠标记）
    const parts = (node.path || node.name).split('/')
    const ancestors: string[] = []
    for (let i = 1; i < parts.length; i++) ancestors.push(parts.slice(0, i).join('/'))
    if (ancestors.length > 0) {
      setExpandedPaths(prev => { const n = new Set(prev); ancestors.forEach(p => n.add(p)); return n })
      setCollapsedPaths(prev => { const n = new Set(prev); ancestors.forEach(p => n.delete(p)); return n })
    }
    openPreview(node)
    // 目录展开 → 文件行渲染出来后再滚动 + 闪烁（两帧 rAF 等待 React 提交 DOM）
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const row = document.querySelector(`[data-ws-path="${CSS.escape(node!.path || node!.name)}"]`)
        if (row) {
          row.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
          row.classList.add('ws-row-flash')
          window.setTimeout(() => row.classList.remove('ws-row-flash'), 1800)
        }
      })
    })
  }, [fileIndex, openPreview])

  useEffect(() => {
    const onLocate = (e: Event) => {
      const path = (e as CustomEvent<{ path?: string }>).detail?.path
      if (path) locateFile(path)
    }
    window.addEventListener(WORKSPACE_LOCATE_EVENT, onLocate)
    return () => window.removeEventListener(WORKSPACE_LOCATE_EVENT, onLocate)
  }, [locateFile])

  return (
    <div className="ws-root">
      {/* ══ sectionHeader — DSH: 标题 + 搜索胶囊 + 尾部操作 ══ */}
      <div className="ws-section-header">
        {/* 标题 — 搜索展开时 CSS 隐藏 */}
        <span className={`ws-section-label ${searchExpanded ? 'ws-section-label-hidden' : ''}`}>
          工作区
        </span>

        {/* 搜索胶囊 — DSH search capsule */}
        <div className={`ws-search-slot ${searchExpanded ? 'ws-search-slot-expanded' : ''}`}>
          <div
            className={`ws-search ${searchExpanded ? 'ws-search-expanded' : ''}`}
            onClick={() => { setSearchExpanded(true); searchInput.current?.focus() }}
          >
            <button
              type="button"
              className="ws-search-btn"
              aria-label="搜索文件"
              onClick={() => { setSearchExpanded(true); searchInput.current?.focus() }}
            >
              <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
                <circle cx="7" cy="7" r="5" stroke="currentColor" strokeWidth="1.5" />
                <path d="M11 11L14 14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </button>
            <input
              ref={searchInput}
              className="ws-search-input"
              type="text"
              placeholder="搜索文件..."
              value={query}
              tabIndex={searchExpanded ? 0 : -1}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Escape') { setQuery(''); setSearchExpanded(false) } }}
            />
            {searchExpanded && query && (
              <button
                type="button"
                className="ws-clear-btn"
                aria-label="清除"
                onClick={(e) => { e.stopPropagation(); setQuery(''); setSearchExpanded(false) }}
              >
                <svg width="12" height="12" viewBox="0 0 14 14" fill="none"><path d="M3 3L11 11M11 3L3 11" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
              </button>
            )}
          </div>
        </div>

        {/* 尾部操作 — 搜索展开时 CSS 隐藏 */}
        <div className={`ws-header-actions ${searchExpanded ? 'ws-header-actions-hidden' : ''}`}>
          {/* 添加按钮 — 3D 立体效果 */}
          <button
            type="button"
            className="ws-add-btn"
            aria-label="添加文件或目录"
            disabled={uploading}
            ref={addBtnRef}
            onClick={() => addMenuOpen ? setAddMenuOpen(false) : openAddMenu()}
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <path d="M2 4a1 1 0 0 1 1-1h3.5l1.5 1.5H13a1 1 0 0 1 1 1V12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V4z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" />
              <path d="M8 7v4M6 9h4" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      </div>

        {/* 隐藏的 directory input（选择目录上传） */}
      <input ref={dirInputRef} type="file" onChange={handleDirSelect} style={{ display: 'none' }} {...({ webkitdirectory: '', directory: '' } as any)} />
      {/* 隐藏的文件 input（多选文件） */}
      <input ref={fileInputRef} type="file" multiple onChange={handleFileSelect} style={{ display: 'none' }} />

      {/* 添加菜单 — fixed 定位避免被 overflow 裁剪 */}
      {addMenuOpen && (
        <>
          <div className="ws-menu-overlay" onClick={() => setAddMenuOpen(false)} />
          <div className="ws-add-menu" style={{ top: menuPos.top, right: menuPos.right }}>
            <button type="button" className="ws-menu-item" onClick={() => { setAddMenuOpen(false); dirInputRef.current?.click() }}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M2 4a1 1 0 0 1 1-1h3.5l1.5 1.5H13a1 1 0 0 1 1 1V12a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V4z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" /></svg>
              <span>选择目录上传</span>
            </button>
            <button type="button" className="ws-menu-item" onClick={() => { setAddMenuOpen(false); fileInputRef.current?.click() }}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M4 1.5h6l3 3V14a.5.5 0 0 1-.5.5h-9A.5.5 0 0 1 3 14V2a.5.5 0 0 1 .5-.5z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" /><path d="M9 1.5V5h4" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" /></svg>
              <span>上传文件</span>
            </button>
            <div className="ws-menu-divider" />
            <button type="button" className="ws-menu-item" onClick={() => { setAddMenuOpen(false); handleDownload() }} disabled={downloading}>
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M8 1.5v8M5 7l3 3 3-3M2.5 13h11" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" /></svg>
              <span>{downloading ? '打包中...' : '下载工作区'}</span>
            </button>
          </div>
        </>
      )}

      {/* 上下文注入已由会话模式控制（Agent 办公模式固定注入） */}

      {/* 错误提示 */}
      {error && <div className="ws-error">{error}</div>}

      {/* ══ listArea — DSH: 唯一滚动区域 + 底部 fade ══ */}
      <div
        className={`ws-list-area ${dragOver ? 'ws-drag-over' : ''}`}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
        onDragLeave={() => setDragOver(false)}
        onDrop={handleDrop}
      >
        <div className="ws-tree-body">
          <div className="ws-list" role="tree" aria-label="工作区文件">
            {loading ? (
              <div className="ws-empty">加载中...</div>
            ) : !filteredTree || !filteredTree.children || filteredTree.children.length === 0 ? (
              <div className="ws-empty">
                {debouncedQuery.trim() ? '无匹配文件' : !loggedIn ? (
                  <>
                    <div style={{ fontSize: 28, marginBottom: 8 }}>🔒</div>
                    <div>登录后可使用工作区</div>
                    <div className="ws-empty-hint">文件、目录与 Agent 产物按账号独立保存</div>
                    <button
                      type="button"
                      className="ws-empty-login"
                      onClick={() => window.dispatchEvent(new Event(REQUEST_LOGIN_EVENT))}
                    >
                      登录
                    </button>
                  </>
                ) : (
                  <>
                    <div style={{ fontSize: 28, marginBottom: 8 }}>📂</div>
                    <div>工作区为空</div>
                    <div className="ws-empty-hint">拖拽目录到此处或点击右上角 +</div>
                  </>
                )}
              </div>
            ) : (
              <>
                {/* ── 最近编辑：本轮对话里 AI 改动过的文件，置顶快捷区 ──
                    搜索时隐藏（结果里已能定位）；空集合不渲染。 */}
                {touchedNodes.length > 0 && !debouncedQuery.trim() && (
                  <div className="ws-recent-section">
                    <div className="ws-recent-header">
                      <span className="ws-recent-title">最近编辑 · {touchedNodes.length}</span>
                      <button type="button" className="ws-recent-clear" onClick={clearTouched} aria-label="清除最近编辑标记" title="清除标记">✕</button>
                    </div>
                    {touchedNodes.map((n, i) => (
                      <div
                        key={`touched-${n.path || n.name}-${i}`}
                        className="ws-session-row ws-recent-row"
                        role="treeitem"
                        onClick={() => openPreview(n)}
                        title={`点击预览 ${n.name}`}
                        style={{ paddingLeft: 8 + 1 * INDENT_STEP }}
                      >
                        <span className="ws-slot"><FileIcon name={n.name} /></span>
                        <span className="ws-title">{n.name}</span>
                        <span aria-hidden style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--accent)', flex: 'none', boxShadow: '0 0 4px color-mix(in srgb, var(--accent) 50%, transparent)' }} />
                      </div>
                    ))}
                    <div className="ws-recent-divider" />
                  </div>
                )}
                {filteredTree.children.map((child, i) => (
                  <TreeNode key={`${child.path || child.name}-${i}`} node={child} depth={0} onDelete={handleDelete} onPreview={openPreview} isPathExpanded={isPathExpanded} onTogglePath={togglePath} isTouched={isTouched} deleteTarget={deleteTarget} onContextMenu={openCtxMenu} />
                ))}
              </>
            )}
          </div>
          {/* 底部 fade — DSH: transparent→sidebar fill 渐变 */}
          <span className="ws-fade" />
        </div>
      </div>

      {/* ══ 底部统计 ══
          始终渲染：stats 为 null 说明加载失败或还没登录，此时刷新按钮就是唯一的重试入口，
          跟着 stats 一起消失等于把用户困在错误态里。 */}
      <div className="ws-stats-bar">
        {stats ? (
          <>
            <span>📄 {stats.file_count}/{stats.max_files}</span>
            <span>💾 {formatSize(stats.total_size)}/{formatSize(stats.max_size)}</span>
          </>
        ) : (
          <span>📄 --/--</span>
        )}
        <button type="button" className="ws-icon-btn" onClick={refresh} aria-label="刷新" title="刷新">
          <svg width="12" height="12" viewBox="0 0 12 12" fill="none"><path d="M2 6a4 4 0 0 1 7-2.5M10 6a4 4 0 0 1-7 2.5M8 1v3h3M4 11V8H1" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" /></svg>
        </button>
      </div>

      <style>{`
        /* ════════════════════════════════════════
         *  CSS — 照搬 DSH WorkspaceBrowser.module.css + Rows.module.css
         *  所有 hover 切换都是纯 CSS，和 DSH 一模一样
         * ════════════════════════════════════════ */

        .ws-root {
          --dsw-alias-label-primary: var(--text-primary);
          --dsw-alias-label-secondary: var(--text-secondary);
          --dsw-alias-label-tertiary: var(--text-tertiary);
          --dsw-alias-label-caption: var(--text-tertiary);
          --dsw-alias-interactive-bg-hover: var(--code-bg);
          --dsw-alias-state-business-primary: var(--accent);
          --dsw-alias-border-l2: var(--line);
          --dsw-specific-sidebar-fill: var(--card-bg-solid);
          --dsh-session-list-edge-inset: 12px;

          flex: 1;
          min-height: 0;
          display: flex;
          flex-direction: column;
          box-sizing: border-box;
          padding-right: var(--dsh-session-list-edge-inset);
        }

        /* ── sectionHeader — DSH: 标题 + 搜索 + 尾部操作 ── */
        .ws-section-header {
          flex: none;
          display: flex;
          align-items: center;
          justify-content: flex-end;
          gap: 4px;
          height: 36px;
          padding-left: 4px;
          margin-bottom: 4px;
          box-sizing: border-box;
          border-radius: 12px;
          overflow: hidden;
          color: var(--dsw-alias-label-tertiary);
        }

        .ws-section-label {
          flex: none;
          max-width: 45%;
          min-width: 0;
          overflow: hidden;
          white-space: nowrap;
          line-height: 20px;
          font-size: 13px;
          font-weight: 600;
          color: var(--dsw-alias-label-secondary);
          margin-right: auto;
          transition: max-width 180ms ease, opacity 120ms ease, visibility 0s;
        }
        .ws-section-label-hidden {
          max-width: 0;
          margin-right: -4px;
          opacity: 0;
          visibility: hidden;
          transition-delay: 0s, 0s, 180ms;
        }

        /* ── 搜索胶囊 — DSH search ── */
        .ws-search-slot {
          flex: 1;
          max-width: 28px;
          min-width: 0;
          display: flex;
          align-items: center;
          margin-left: auto;
          transition: max-width 180ms ease;
        }
        .ws-search-slot-expanded { max-width: 100%; }

        .ws-search {
          flex: none;
          display: flex;
          align-items: center;
          gap: 0;
          width: 100%;
          height: 28px;
          border: none;
          border-radius: 50%;
          background: transparent;
          cursor: text;
          color: var(--dsw-alias-label-secondary);
          overflow: hidden;
          transition: width 180ms ease, padding 180ms ease, border-color 180ms ease, background-color 180ms ease;
        }
        .ws-search-expanded {
          width: calc(100% + 4px);
          height: 30px;
          margin-inline: -2px;
          padding: 0 4px 0 0;
          border: 1px solid var(--dsw-alias-border-l2);
          border-radius: 10px;
          background: transparent;
        }

        .ws-search-btn {
          flex: none;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          width: 28px;
          height: 28px;
          border: none;
          border-radius: 50%;
          padding: 0;
          background: transparent;
          cursor: pointer;
          color: inherit;
        }
        .ws-search-btn:hover { background: var(--dsw-alias-interactive-bg-hover); }
        .ws-search-expanded .ws-search-btn { height: 30px; }
        .ws-search-expanded .ws-search-btn:hover { background: transparent; }

        .ws-search-input {
          flex: 1;
          width: 0;
          min-width: 0;
          border: none;
          outline: none;
          background: transparent;
          opacity: 0;
          pointer-events: none;
          font-size: 13px;
          line-height: 18px;
          color: var(--dsw-alias-label-primary);
          transition: opacity 120ms ease;
        }
        .ws-search-expanded .ws-search-input {
          margin-left: -2px;
          opacity: 1;
          pointer-events: auto;
        }
        .ws-search-input::placeholder { color: var(--dsw-alias-label-tertiary); }

        .ws-clear-btn {
          flex: none;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          width: 24px;
          height: 24px;
          border: none;
          border-radius: 50%;
          background: transparent;
          cursor: pointer;
          color: var(--dsw-alias-label-secondary);
        }
        .ws-clear-btn:hover { background: var(--dsw-alias-interactive-bg-hover); }

        /* ── 尾部操作 — DSH headerActions ── */
        .ws-header-actions {
          flex: none;
          display: flex;
          align-items: center;
          gap: 4px;
          max-width: 60px;
          opacity: 1;
          overflow: hidden;
          transition: max-width 180ms ease, opacity 120ms ease, visibility 0s;
        }
        .ws-header-actions-hidden {
          max-width: 0;
          opacity: 0;
          visibility: hidden;
          pointer-events: none;
          transition-delay: 0s, 0s, 180ms;
        }

        .ws-icon-btn-circle {
          flex: none;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          width: 28px;
          height: 28px;
          border: none;
          border-radius: 50%;
          padding: 0;
          background: transparent;
          cursor: pointer;
          color: var(--dsw-alias-label-secondary);
        }
        .ws-icon-btn-circle:hover { background: var(--dsw-alias-interactive-bg-hover); }
        .ws-icon-btn-circle:disabled { opacity: 0.4; cursor: default; }

        /* ── 3D 立体添加按钮 ── */
        .ws-add-btn {
          flex: none;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          width: 32px;
          height: 32px;
          border: none;
          border-radius: 10px;
          padding: 0;
          cursor: pointer;
          color: var(--dsw-alias-label-primary);
          background: linear-gradient(180deg, var(--dsw-alias-interactive-bg-hover) 0%, var(--dsw-specific-sidebar-fill) 100%);
          box-shadow:
            0 1px 2px rgba(0,0,0,0.08),
            0 2px 4px rgba(0,0,0,0.06),
            inset 0 1px 0 rgba(255,255,255,0.12);
          transition: all 0.12s ease;
          transform: translateZ(0);
        }
        .ws-add-btn:hover {
          background: linear-gradient(180deg, var(--dsw-alias-state-business-primary) 0%, var(--dsw-alias-interactive-bg-hover) 100%);
          box-shadow:
            0 2px 6px rgba(0,0,0,0.12),
            0 4px 12px rgba(0,0,0,0.08),
            inset 0 1px 0 rgba(255,255,255,0.2);
          transform: translateY(-1px);
        }
        .ws-add-btn:active {
          box-shadow:
            0 1px 2px rgba(0,0,0,0.06),
            inset 0 1px 3px rgba(0,0,0,0.1);
          transform: translateY(0);
        }
        .ws-add-btn:disabled {
          opacity: 0.4;
          cursor: default;
          transform: none;
          box-shadow: none;
        }

        /* ── 添加菜单 ── */
        .ws-menu-overlay {
          position: fixed;
          inset: 0;
          z-index: 9998;
          background: transparent;
        }
        .ws-add-menu {
          position: fixed;
          z-index: 9999;
          background: var(--dsw-specific-sidebar-fill);
          border: 1px solid var(--dsw-alias-border-l2);
          border-radius: 12px;
          box-shadow:
            0 4px 16px rgba(0,0,0,0.12),
            0 8px 32px rgba(0,0,0,0.08),
            inset 0 1px 0 rgba(255,255,255,0.08);
          overflow: hidden;
          min-width: 150px;
          padding: 4px;
          animation: ws-menu-in 0.12s ease;
        }
        @keyframes ws-menu-in {
          from { opacity: 0; transform: translateY(-4px) scale(0.96); }
          to { opacity: 1; transform: translateY(0) scale(1); }
        }
        .ws-menu-item {
          display: flex;
          align-items: center;
          gap: 8px;
          width: 100%;
          padding: 8px 12px;
          border: none;
          border-radius: 8px;
          background: transparent;
          color: var(--dsw-alias-label-primary);
          font-size: 13px;
          font-family: inherit;
          text-align: left;
          cursor: pointer;
          transition: background 0.1s;
        }
        .ws-menu-item:hover {
          background: var(--dsw-alias-interactive-bg-hover);
        }
        .ws-menu-item:active {
          background: var(--dsw-alias-state-business-primary);
        }
        .ws-menu-item:disabled {
          opacity: 0.4;
          cursor: default;
        }
        .ws-menu-hint {
          padding: 6px 12px 4px;
          font-size: 11px;
          line-height: 1.6;
          color: var(--dsw-alias-label-tertiary);
          text-align: left;
        }
        .ws-menu-divider {
          height: 1px;
          margin: 4px 8px;
          background: var(--dsw-alias-border-l2);
        }

        /* ── 上下文注入开关 ── */
        .ws-inject-toggle {
          flex: none;
          padding: 2px 4px 4px;
          font-size: 11px;
          color: var(--dsw-alias-label-tertiary);
        }
        .ws-inject-toggle label {
          display: flex;
          align-items: center;
          gap: 6px;
          cursor: pointer;
        }
        .ws-inject-toggle input { accent-color: var(--accent); }

        /* ── 错误 ── */
        .ws-error {
          margin: 0 4px 4px;
          padding: 4px 8px;
          font-size: 11px;
          color: #c44;
          background: rgba(200,60,60,0.06);
          border-radius: 6px;
        }

        /* ── listArea — DSH: 唯一滚动区域 ── */
        .ws-list-area {
          flex: 1;
          min-height: 0;
          display: flex;
          flex-direction: column;
          margin-left: -4px;
          margin-right: calc(-1 * var(--dsh-session-list-edge-inset));
          padding-left: 4px;
          overflow: visible;
          border-radius: 8px;
          border: 2px solid transparent;
          transition: border 0.15s;
        }
        .ws-drag-over { border-color: var(--accent); }

        /* ── treeBody — DSH: relative for fade ── */
        .ws-tree-body {
          flex: 1;
          min-height: 0;
          display: flex;
          flex-direction: column;
          position: relative;
        }

        .ws-list {
          flex: 1;
          min-height: 0;
          overflow-y: auto;
          margin-left: -4px;
          margin-right: 2px;
          padding-left: 4px;
          padding-right: 2px;
          padding-bottom: 16px;
          scrollbar-gutter: stable;
        }
        .ws-list > * + * { margin-top: 2px; }

        /* ── 底部 fade — DSH: transparent→fill 渐变 ── */
        .ws-fade {
          position: absolute;
          left: 0;
          right: var(--dsh-session-list-edge-inset);
          bottom: 0;
          height: 24px;
          background: linear-gradient(to bottom, transparent, var(--dsw-specific-sidebar-fill));
          pointer-events: none;
        }

        /* ── empty ── */
        .ws-empty {
          padding: 16px 12px;
          color: var(--dsw-alias-label-tertiary);
          font-size: 13px;
          text-align: center;
        }
        .ws-empty-hint {
          font-size: 11px;
          margin-top: 4px;
          color: var(--dsw-alias-label-caption);
        }

        .ws-empty-login {
          margin-top: 12px;
          padding: 6px 18px;
          font-size: 12px;
          font-family: inherit;
          color: var(--dsw-alias-label-primary);
          background: var(--dsw-alias-interactive-bg-hover);
          border: 1px solid var(--dsw-alias-border-l2);
          border-radius: 999px;
          cursor: pointer;
          transition: border-color 0.15s, background 0.15s;
        }
        .ws-empty-login:hover {
          border-color: var(--dsw-alias-state-business-primary);
        }

        /* ════════════════════════════════════════
         *  Rows — 照搬 DSH Rows.module.css
         *  关键：所有 hover 切换都是纯 CSS
         * ════════════════════════════════════════ */

        /* ── groupSection ── */
        .ws-group-section {
          position: relative;
        }
        .ws-group-section + .ws-group-section {
          margin-top: 4px;
        }
        .ws-group-section > * + * {
          margin-top: 2px;
        }

        /* ── ProjectRow (directory): 34px — DSH .projectRow ── */
        .ws-project-row {
          display: flex;
          align-items: center;
          gap: 6px;
          height: 34px;
          border-radius: 8px;
          padding: 0 8px;
          cursor: pointer;
          user-select: none;
          color: var(--dsw-alias-label-primary);
          box-sizing: border-box;
          transition: background 0.12s;
        }
        .ws-project-row:hover { background: var(--dsw-alias-interactive-bg-hover); }

        /* ── SessionRow (file): 32px — DSH .sessionRow ── */
        .ws-session-row {
          display: flex;
          align-items: center;
          gap: 0;
          height: 32px;
          border-radius: 8px;
          padding: 0 8px;
          cursor: pointer;
          user-select: none;
          color: var(--dsw-alias-label-primary);
          box-sizing: border-box;
          transition: background 0.12s;
        }
        .ws-session-row:hover { background: var(--dsw-alias-interactive-bg-hover); }

        /* ── 最近编辑分区（置顶快捷区）── */
        .ws-recent-section {
          flex: none;
          margin-bottom: 4px;
        }
        .ws-recent-header {
          display: flex;
          align-items: center;
          gap: 6px;
          height: 26px;
          padding: 0 8px;
        }
        .ws-recent-title {
          font-size: 11px;
          font-weight: 600;
          color: var(--accent);
          letter-spacing: 0.04em;
          flex: 1;
        }
        .ws-recent-clear {
          flex: none;
          display: grid;
          place-items: center;
          width: 18px;
          height: 18px;
          border: none;
          border-radius: 4px;
          background: transparent;
          color: var(--text-tertiary);
          cursor: pointer;
          font-size: 11px;
          transition: background 0.12s, color 0.12s;
        }
        .ws-recent-clear:hover { background: var(--code-bg); color: var(--text-secondary); }
        .ws-recent-row {
          background: color-mix(in srgb, var(--accent) 5%, transparent);
        }
        .ws-recent-divider {
          height: 1px;
          margin: 6px 8px 4px;
          background: var(--line);
          opacity: 0.6;
        }

        /* ── slot: 16px 图标位 — DSH .slot ── */
        .ws-slot {
          flex: none;
          width: 16px;
          height: 20px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          color: var(--dsw-alias-label-tertiary);
        }

        /* ════════════════════════════════════════
         *  纯 CSS hover 切换 — 和 DSH Rows.module.css 一模一样
         * ════════════════════════════════════════ */

        /* ProjectRow: folder 默认显示, chevron 默认隐藏
           hover 时: folder 隐藏, chevron 显示 — 纯 CSS */
        .ws-project-row .ws-chevron { display: none; }
        .ws-project-row:hover .ws-chevron { display: inline-flex; }
        .ws-project-row:hover .ws-folder { display: none; }

        /* Expand arrow: 默认右指, 展开时旋转 90° */
        .ws-arrow {
          transition: transform 150ms ease;
        }

        /* rowActions: 默认隐藏, hover 时显示 — 纯 CSS */
        .ws-row-actions {
          flex: none;
          display: none;
          align-items: center;
          gap: 12px;
        }
        .ws-project-row:hover .ws-row-actions,
        .ws-session-row:hover .ws-row-actions {
          display: inline-flex;
        }

        /* time: 默认显示, hover 时隐藏 — 纯 CSS */
        .ws-session-row:hover .ws-time {
          display: none;
        }

        /* ── chevron 颜色 — DSH: caption grey ── */
        .ws-chevron {
          color: var(--dsw-alias-label-caption);
        }

        /* ── 标题 — DSH .title ── */
        .ws-project-text {
          flex: 1;
          min-width: 0;
          display: flex;
          flex-direction: column;
          gap: 2px;
        }
        .ws-title {
          flex: 1;
          min-width: 0;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          font-size: 14px;
          line-height: 20px;
          margin: 0 6px 0 4px;
        }

        /* ── time/size — DSH .time ── */
        .ws-time {
          flex: none;
          font-size: 12px;
          line-height: 20px;
          color: var(--dsw-alias-label-tertiary);
        }

        /* ── iconButton — DSH .iconButton ── */
        .ws-icon-btn {
          flex: none;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          width: 16px;
          height: 16px;
          border: none;
          border-radius: 4px;
          padding: 0;
          background: transparent;
          cursor: pointer;
          color: var(--dsw-alias-label-tertiary);
        }
        .ws-icon-btn:hover { color: var(--dsw-alias-label-primary); }

        /* ── 底部统计 ── */
        .ws-stats-bar {
          flex: none;
          padding: 6px 4px;
          border-top: 1px solid var(--line);
          display: flex;
          justify-content: space-between;
          align-items: center;
          font-size: 11px;
          color: var(--dsw-alias-label-tertiary);
        }
        .ws-stats-bar .ws-icon-btn { width: 20px; height: 20px; }

        /* #1 定位跳转：文件行短暂闪烁，让"跳到这个文件"有明确的落点 */
        @keyframes ws-row-flash-anim {
          0% { background: color-mix(in srgb, var(--accent) 26%, transparent); }
          100% { background: transparent; }
        }
        .ws-row-flash { animation: ws-row-flash-anim 1.8s ease-out; border-radius: 8px; }

        @media (prefers-reduced-motion: reduce) {
          .ws-search, .ws-section-label, .ws-search-slot, .ws-search-input, .ws-header-actions, .ws-arrow {
            transition: none;
          }
        }
      `}</style>

      {/* #5 右键操作菜单（打开/复制路径/下载单个/删除） */}
      {ctxMenu && (
        <>
          <div className="ws-menu-overlay" onClick={closeCtxMenu} onContextMenu={(e) => { e.preventDefault(); closeCtxMenu() }} />
          <div className="ws-add-menu" style={{ top: Math.min(ctxMenu.y, window.innerHeight - 240), left: Math.min(ctxMenu.x, window.innerWidth - 200), right: 'auto' }}>
            <button type="button" className="ws-menu-item" onClick={() => { const n = ctxMenu.node; closeCtxMenu(); if (n.type === 'file') openPreview(n) }}>
              <ExternalLink size={16} strokeWidth={1.6} aria-hidden />
              <span>{ctxMenu.node.type === 'file' ? '打开预览' : '打开目录'}</span>
            </button>
            <button type="button" className="ws-menu-item" onClick={() => { const p = ctxMenu.node.path || ''; closeCtxMenu(); void navigator.clipboard.writeText(p).then(() => notify('已复制路径', 'info', '复制成功')) }}>
              <Copy size={16} strokeWidth={1.6} aria-hidden />
              <span>复制路径</span>
            </button>
            {ctxMenu.node.type === 'file' && !['pdf', 'doc', 'docx', 'xls', 'xlsx'].includes(ctxMenu.node.name.split('.').pop()?.toLowerCase() || '') && (
              <button type="button" className="ws-menu-item" onClick={() => { const n = ctxMenu.node; closeCtxMenu(); void downloadSingle(n) }}>
                <Download size={16} strokeWidth={1.6} aria-hidden />
                <span>下载单个</span>
              </button>
            )}
            <div className="ws-menu-divider" />
            <button type="button" className="ws-menu-item" onClick={() => { const n = ctxMenu.node; closeCtxMenu(); handleDelete(n.path || n.name, n.type === 'directory') }}>
              <Trash2 size={16} strokeWidth={1.6} aria-hidden />
              <span>删除</span>
            </button>
          </div>
        </>
      )}

      {/* 文件预览弹窗（#3 历史版本抽屉 + #4 按类型分流渲染） */}
      {preview && (
        <div
          style={{ position: 'fixed', inset: 0, zIndex: 10000, background: 'rgba(0,0,0,0.35)', display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24 }}
          onClick={closePreview}
        >
          <div
            onClick={e => e.stopPropagation()}
            style={{ width: 'min(720px, 100%)', maxHeight: '85vh', display: 'flex', flexDirection: 'column', background: 'var(--card-bg-solid)', border: '1px solid var(--line)', borderRadius: 14, boxShadow: '0 16px 48px rgba(0,0,0,0.2)', overflow: 'hidden' }}
          >
            {/* 头部：文件名 + 路径 + 历史版本开关 + 关闭 */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '12px 16px', borderBottom: '1px solid var(--line)' }}>
              <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}>📄 {preview.name}</span>
              <span style={{ fontSize: 12, color: 'var(--text-tertiary)', flexShrink: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', maxWidth: 220 }}>{preview.path}</span>
              {/* #3 历史版本开关（仅文本类产物有意义；二进制图无快照） */}
              {preview.kind !== 'image' && (
                <button
                  type="button"
                  onClick={() => void toggleVersions()}
                  title="历史版本"
                  style={{
                    flexShrink: 0, display: 'inline-flex', alignItems: 'center', gap: 4,
                    height: 26, padding: '0 8px', border: '1px solid', borderRadius: 6,
                    borderColor: versionsOpen ? 'var(--accent)' : 'var(--line)',
                    background: versionsOpen ? 'var(--code-bg)' : 'transparent',
                    color: versionsOpen ? 'var(--accent)' : 'var(--text-tertiary)',
                    cursor: 'pointer', fontFamily: 'inherit', fontSize: 12, transition: 'all 0.15s',
                  }}
                >
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="none" aria-hidden>
                    <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="1.5" />
                    <path d="M8 4.5V8l2.5 1.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  历史{versions && versions.length > 0 ? ` · ${versions.length}` : ''}
                </button>
              )}
              <button
                type="button"
                onClick={closePreview}
                aria-label="关闭预览"
                style={{ flexShrink: 0, display: 'grid', placeItems: 'center', width: 26, height: 26, border: 'none', borderRadius: 6, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer', fontSize: 14, transition: 'background 0.15s, color 0.15s' }}
                onMouseEnter={e => { e.currentTarget.style.background = 'var(--code-bg)'; e.currentTarget.style.color = 'var(--text-primary)' }}
                onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--text-tertiary)' }}
              >✕</button>
            </div>

            {/* #3 历史版本抽屉 */}
            {versionsOpen && (
              <div style={{ borderBottom: '1px solid var(--line)', padding: '10px 16px', background: 'var(--card-bg-solid)', maxHeight: 220, overflowY: 'auto' }}>
                <div style={{ fontSize: 12, color: 'var(--text-tertiary)', marginBottom: 8 }}>
                  历史版本（write/edit 覆写前自动留存的快照；回退不改写历史，可连续回退）
                </div>
                {versions === null ? (
                  <div style={{ fontSize: 13, color: 'var(--text-tertiary)' }}>加载中...</div>
                ) : versions.length === 0 ? (
                  <div style={{ fontSize: 12.5, color: 'var(--text-tertiary)' }}>无历史版本（仅被 write_file / edit_file 覆写过的文件才有快照）</div>
                ) : (
                  versions.map(v => (
                    <div key={v.steps} style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '7px 4px', borderBottom: '1px solid var(--line)' }}>
                      <span style={{ flex: 1, fontSize: 12.5, color: 'var(--text-secondary)' }}>
                        <span style={{ color: 'var(--accent)', fontWeight: 600 }}>第 {v.steps} 版</span>
                        {' · '}{new Date(v.ts).toLocaleString()}{' · '}{formatSize(v.size)}
                      </span>
                      {restoreTarget === v.steps ? (
                        <>
                          <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>回退到这一版？</span>
                          <button type="button" onClick={() => void doRestore(v.steps)} disabled={restoring}
                            style={{ padding: '3px 10px', fontSize: 12, border: 'none', borderRadius: 5, background: '#ef4444', color: '#fff', cursor: 'pointer', opacity: restoring ? 0.6 : 1 }}>
                            {restoring ? '回退中...' : '确认'}
                          </button>
                          <button type="button" onClick={() => setRestoreTarget(null)} disabled={restoring}
                            style={{ padding: '3px 10px', fontSize: 12, border: '1px solid var(--line)', borderRadius: 5, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer' }}>
                            取消
                          </button>
                        </>
                      ) : (
                        <button type="button" onClick={() => setRestoreTarget(v.steps)} disabled={restoring}
                          style={{ padding: '3px 10px', fontSize: 12, border: '1px solid var(--line)', borderRadius: 5, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer' }}>
                          {v.steps === 1 ? '回退到最近一版' : '回退'}
                        </button>
                      )}
                    </div>
                  ))
                )}
              </div>
            )}

            {/* #4 内容体：按类型分流 */}
            <div style={{ flex: 1, minHeight: 0, overflow: 'auto', padding: 14, background: 'var(--bg)' }}>
              {preview.loading ? (
                <div style={{ color: 'var(--text-tertiary)', fontSize: 13, padding: 20, textAlign: 'center' }}>加载中...</div>
              ) : preview.error ? (
                <div style={{ padding: 12, fontSize: 12.5, color: '#ef4444', lineHeight: 1.6 }}>{preview.error}</div>
              ) : (
                <>
                  {preview.truncated && (
                    <div style={{ marginBottom: 8, padding: '6px 10px', borderRadius: 6, background: 'rgba(200,60,60,0.08)', border: '1px solid rgba(200,60,60,0.2)', color: 'var(--text-secondary)', fontSize: 12 }}>
                      ⚠️ 文件较大，仅显示开头部分（完整内容请下载后查看）
                    </div>
                  )}
                  {preview.kind === 'image' ? (
                    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 120 }}>
                      {preview.imgUrl
                        ? <img src={preview.imgUrl} alt={preview.name} style={{ maxWidth: '100%', maxHeight: '70vh', borderRadius: 6 }} />
                        : <span style={{ color: 'var(--text-tertiary)', fontSize: 13 }}>无法预览此图片</span>}
                    </div>
                  ) : preview.kind === 'html' ? (
                    <iframe srcDoc={preview.content} title={`预览 ${preview.name}`} sandbox="allow-scripts" referrerPolicy="no-referrer" loading="lazy"
                      style={{ display: 'block', width: '100%', height: '60vh', border: 'none', borderRadius: 8, background: '#fff' }} />
                  ) : preview.kind === 'svg' ? (
                    <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', padding: 8, background: '#fff', borderRadius: 8, border: '1px solid var(--line)' }}>
                      <img src={previewSvgUrl ?? undefined} alt={`预览 ${preview.name}`} style={{ maxWidth: '100%', maxHeight: '70vh' }} />
                    </div>
                  ) : preview.kind === 'md' ? (
                    <div style={{ fontSize: 14, lineHeight: 1.7, color: 'var(--text-primary)' }}>
                      <MarkdownRenderer content={preview.content} />
                    </div>
                  ) : preview.kind === 'code' ? (
                    <pre style={{ margin: 0, fontSize: 12.5, lineHeight: 1.6, fontFamily: 'var(--font-mono, monospace)', color: 'var(--text-secondary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                      <code dangerouslySetInnerHTML={{ __html: highlightCode(preview.content, langFromName(preview.name)) }} />
                    </pre>
                  ) : (
                    <pre style={{ margin: 0, fontSize: 12.5, lineHeight: 1.6, fontFamily: 'var(--font-mono, monospace)', color: 'var(--text-secondary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{preview.content}</pre>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
})
