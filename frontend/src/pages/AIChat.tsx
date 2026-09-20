import { useState, useEffect, useLayoutEffect, useRef, useCallback, useMemo, memo } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
// hljs 实例 + 语言注册抽到共享模块：AIChat 代码块与工作区预览面板复用同一份配置
import hljs from '@/lib/hljs-setup'
// 代码高亮配色在 globals.css 自定义（浅色主题适配，深色模式跟随）
import { useChat } from '@/hooks/useChat'
import type { ChatMessage, ChatAttachment, ChatFile, ContextPressure, ToolInvocation, AgentPhase } from '@/hooks/useChat'
import type { ChatSession, ChatSkillInfo, WorkspaceInjectMode } from '@/lib/chat-api'
import { fetchChatConfig, fetchChatSkills } from '@/lib/chat-api'
import { extractDocumentText, isDocumentFile, DOCUMENT_EXTENSIONS, MAX_DOCUMENT_SIZE } from '@/lib/doc-extract'
import { convertToBlob, needsConversion } from '@/lib/doc-generate'
import { listMemories, deleteMemory, clearAllMemories, type MemoryItem } from '@/lib/chat-api'
import { completeStreamingMarkdown } from '@/lib/stream-markdown'
import { useAuth } from '@/hooks/useAuth'
import { AuthModal } from '@/components/AuthModal'
import { UserBadge } from '@/components/UserBadge'
import { WorkspacePanel, REQUEST_LOGIN_EVENT, WORKSPACE_LOCATE_EVENT, WORKSPACE_REFERENCE_EVENT, WORKSPACE_TOAST_EVENT } from '@/components/WorkspacePanel'
import { fetchGeneratedFile, archiveChatImages } from '@/lib/workspace-api'
import { useImageRef, invalidateChatImageRef } from '@/lib/image-ref-cache'
// lucide 图标（第一期：emoji 图标全部替换为 SVG，暗色模式颜色自动一致）
import {
  FileText, Pencil, Undo2, FolderOpen, Trash2, Zap, Puzzle, BookOpen, Wrench,
  Hourglass, Brain, Cog, PenLine, Eye, EyeOff, AlertTriangle, Info, Code2, Languages,
  BarChart3, type LucideIcon,
} from 'lucide-react'
// 共享平面样式常量（第一期：重复的内联配方收敛到这里）
import { SHADOW_SM, SHADOW_MD } from '@/styles/ui'

// ============================================================
// DSH 风格对话页面 — 仿 DeepSeek Harness 桌面端
// 布局: sidebar + main(header + scrollBody + composer)
// ============================================================

// ── 已选文件类型 ──
interface AttachedFile {
  name: string
  size: number
  content: string
  dataUrl?: string   // 图片文件的 base64 数据 URL（多模态用）
  isImage?: boolean  // 是否为图片
  imageXml?: string  // 图片型 PDF 的预构造 <image> 标签串（多页扫描件）
  hash?: string      // 内容指纹（优化12）：同内容重复附加时直接跳过
  ref?: string       // 图片的工作区存档路径（优化9）：刷新后据此取回缩略图。只进渲染层，
                     // 不写进发给模型的消息正文 —— 历史图片仍一律是占位符
}

// 支持读取的文本文件扩展名
const TEXT_EXTENSIONS = new Set([
  'txt', 'md', 'markdown', 'json', 'yaml', 'yml', 'xml', 'html', 'htm',
  'css', 'scss', 'less', 'js', 'jsx', 'ts', 'tsx', 'py', 'java', 'go',
  'rs', 'c', 'cpp', 'h', 'hpp', 'cs', 'rb', 'php', 'swift', 'kt', 'dart',
  'sql', 'sh', 'bash', 'zsh', 'bat', 'ps1', 'ini', 'cfg', 'conf', 'toml',
  'env', 'csv', 'tsv', 'log', 'svg', 'vue', 'svelte', 'lua', 'r', 'scala',
  'clj', 'ex', 'exs', 'erl', 'hs', 'ml', 'nim', 'v', 'zig', 'gradle',
  'properties', 'gitignore', 'dockerfile', 'makefile', 'lock',
])

// 图片文件扩展名
const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico'])

// 图片体积上限：MAX_IMAGE_SIZE 是**降采样之后**的上限，与后端 chat.py 的 _MAX_IMAGE_BYTES 对齐；
// MAX_IMAGE_UPLOAD 是原始文件上限 —— 有了客户端压缩，一张 6MB 手机照片能压到几百 KB，
// 再用 4MB 卡原始文件纯属误伤。20MB base64 后约 26.7MB，仍在后端 32MB body 上限内。
const MAX_IMAGE_SIZE = 4 * 1024 * 1024
const MAX_IMAGE_UPLOAD = 20 * 1024 * 1024

// 最大文件大小 (512 KB) — 防止 token 爆炸
const MAX_FILE_SIZE = 512 * 1024

/** 读取文件文本内容 */
function readFileContent(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = reader.result
      if (typeof result === 'string') {
        resolve(result)
      } else {
        reject(new Error('无法读取文件内容'))
      }
    }
    reader.onerror = () => reject(new Error('文件读取失败'))
    reader.readAsText(file, 'utf-8')
  })
}

/** 读取图片文件为 base64 数据 URL */
function readImageAsDataURL(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = reader.result
      if (typeof result === 'string') {
        resolve(result)
      } else {
        reject(new Error('无法读取图片内容'))
      }
    }
    reader.onerror = () => reject(new Error('图片读取失败'))
    reader.readAsDataURL(file)
  })
}

// ── 图片降采样 / 转码（图片视觉优化4+6）──
// provider 一律先把图缩到 ~1-2MP 再按 tile 计费，超过 1568px 的像素是白传；
// 而 bmp/ico 在 OpenAI/Anthropic 普遍直接 400，前端白名单却放行 —— 一并转码掉。
const MAX_IMAGE_EDGE = 1568               // 主流 provider 高分档的单边上限
const COMPRESS_ABOVE_BYTES = 1024 * 1024  // 小于此体积且格式合法就不动，免得 OCR 场景无谓掉精度
// 注意：跳过判断只看体积、不看像素尺寸 —— 判断尺寸必须先完整解码（实测 ico/大图要 ~1s），
// 而 1MB 以内的图无论多少像素，传输与 vision token 成本都可忽略，不值得付这个解码代价。
const JPEG_QUALITY = 0.85
const TRANSCODE_EXTENSIONS = new Set(['bmp', 'ico'])
const TRANSCODE_MIMES = new Set([
  'image/bmp', 'image/x-ms-bmp', 'image/x-icon', 'image/vnd.microsoft.icon',
])
// gif 一律不重编码：createImageBitmap 只解首帧，压完动画就静默没了
const KEEP_AS_IS_MIMES = new Set(['image/gif'])

function blobToDataURL(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      if (typeof reader.result === 'string') resolve(reader.result)
      else reject(new Error('图片编码结果无法读取'))
    }
    reader.onerror = () => reject(new Error('图片编码失败'))
    reader.readAsDataURL(blob)
  })
}

/**
 * 读取图片，必要时降采样 + 转码。
 *
 * 返回的 size 是**实际会发出去的字节数**，调用方必须用它而不是 file.size，
 * 否则 `<image size="...">` 与后端单图 4MB 校验拿到的都是假数字。
 * 返回的 blob 是**实际会发出去的那份字节**（回退分支即原始 file），与 dataUrl 严格对应；
 * 工作区存档（优化9）必须存它，否则缩略图与消息里标注的 size 会对不上。
 * 解码/编码任一步失败都回退原始文件：压缩只是优化，不该变成上传失败的原因
 * （旧浏览器没有 OffscreenCanvas 时，行为自动退化为改动前）。
 */
async function prepareImageFile(file: File): Promise<{ dataUrl: string; size: number; blob: Blob }> {
  const ext = file.name.split('.').pop()?.toLowerCase() || ''
  const needsTranscode = TRANSCODE_EXTENSIONS.has(ext) || TRANSCODE_MIMES.has(file.type)
  if (!needsTranscode && (KEEP_AS_IS_MIMES.has(file.type) || ext === 'gif' || file.size <= COMPRESS_ABOVE_BYTES)) {
    return { dataUrl: await readImageAsDataURL(file), size: file.size, blob: file }
  }
  try {
    // imageOrientation:'from-image' 让浏览器按 EXIF 应用旋转 —— 画布重绘会丢掉 EXIF，
    // 不显式声明的话手机竖拍照片发给模型时是横躺的。
    const bitmap = await createImageBitmap(file, { imageOrientation: 'from-image' })
    try {
      const scale = Math.min(1, MAX_IMAGE_EDGE / Math.max(bitmap.width, bitmap.height))
      const w = Math.max(1, Math.round(bitmap.width * scale))
      const h = Math.max(1, Math.round(bitmap.height * scale))
      const canvas = new OffscreenCanvas(w, h)
      const ctx = canvas.getContext('2d')
      if (!ctx) throw new Error('无法取得 canvas 2d 上下文')
      ctx.drawImage(bitmap, 0, 0, w, h)
      // 输出格式：PNG 源保留 PNG（多为截图/文档，JPEG 会让文字发糊、OCR 掉精度）；
      // 其余按是否真有透明通道决定 —— 不透明照片走 JPEG，体积能差一个数量级。
      const srcIsPng = file.type === 'image/png' || ext === 'png'
      let hasAlpha = false
      if (!srcIsPng) {
        const px = ctx.getImageData(0, 0, w, h).data
        for (let i = 3; i < px.length; i += 4) {
          if (px[i] < 255) { hasAlpha = true; break }
        }
      }
      // JPEG 没有 alpha，透明区会渲染成黑 —— 用 destination-over 在图像背后铺白底
      const flattenOntoWhite = () => {
        ctx.globalCompositeOperation = 'destination-over'
        ctx.fillStyle = '#ffffff'
        ctx.fillRect(0, 0, w, h)
        ctx.globalCompositeOperation = 'source-over'
      }
      let blob: Blob
      if (!srcIsPng && !hasAlpha) {
        flattenOntoWhite()
        blob = await canvas.convertToBlob({ type: 'image/jpeg', quality: JPEG_QUALITY })
      } else {
        blob = await canvas.convertToBlob({ type: 'image/png' })
        // 高熵透明图（带 alpha 的照片/噪点图）PNG 压不到发送上限以内。与其拒收用户的
        // 文件，不如丢掉透明背景降级成 JPEG —— 内容能发出去比保住 alpha 更重要。
        if (blob.size > MAX_IMAGE_SIZE) {
          flattenOntoWhite()
          const flat = await canvas.convertToBlob({ type: 'image/jpeg', quality: JPEG_QUALITY })
          if (flat.size < blob.size) blob = flat
        }
      }
      // 压完反而更大就保留原图（同尺寸 PNG→PNG 有可能），但 bmp/ico 必须转码，不在此列
      if (!needsTranscode && blob.size >= file.size) {
        return { dataUrl: await readImageAsDataURL(file), size: file.size, blob: file }
      }
      return { dataUrl: await blobToDataURL(blob), size: blob.size, blob }
    } finally {
      bitmap.close()
    }
  } catch {
    return { dataUrl: await readImageAsDataURL(file), size: file.size, blob: file }
  }
}

// ══ 附件摄取（优化11 并行+进度+取消，优化12 内容去重）══

// 并发上限 3：max_files_per_message 最高 20，全并行的话 20 张 1568² 位图同时驻留
// 约 200MB 画布后备存储，笔记本/手机上足以崩掉标签页。
const INGEST_CONCURRENCY = 3

/** 保序并发池：结果按入参数组下标返回，不受完成先后影响 */
async function mapWithConcurrency<T, R>(
  items: T[],
  limit: number,
  fn: (item: T, index: number) => Promise<R>,
): Promise<R[]> {
  const out = new Array<R>(items.length)
  let next = 0
  // next++ 与越界判断之间没有 await，JS 单线程下不会出现两个 worker 抢到同一下标
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    for (;;) {
      const i = next++
      if (i >= items.length) return
      out[i] = await fn(items[i], i)
    }
  })
  await Promise.all(workers)
  return out
}

/**
 * 附件内容指纹（优化12）：同一张图选两次会产生两份完全相同的 base64，
 * 体积与 vision token 直接翻倍，所以按内容去重，命中就连压缩都省掉。
 * crypto.subtle 只在安全上下文（https / localhost）可用；不可用时退化成
 * name+size+lastModified —— 对「重选同一个文件」仍然有效，只是认不出改过名的相同内容。
 */
async function fileFingerprint(file: File): Promise<string> {
  const subtle = globalThis.crypto?.subtle
  if (subtle) {
    try {
      const digest = await subtle.digest('SHA-256', await file.arrayBuffer())
      return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('')
    } catch { /* 落到指纹兜底 */ }
  }
  return `f:${file.name}|${file.size}|${file.lastModified}`
}

// ── 工作区存档路径（优化9）──
// 与后端 workspace_service 的 CHAT_IMAGE_DIR / CHAT_ARCHIVE_PATH_RE 逐字对齐。
const CHAT_IMAGE_DIR = '聊天图片'
const MIME_TO_EXT: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
  'image/bmp': 'bmp',
  'image/x-icon': 'ico',
  'image/vnd.microsoft.icon': 'ico',
}

/**
 * 对**压缩后的 blob** 重算 SHA-256。
 *
 * 不能复用 fileFingerprint：它哈希的是原始 File，而存档上传的是压缩后的 blob，
 * 且 prepareImageFile 有「压完更大就保留原图」的回退分支 —— 同一个文件在不同
 * 浏览器版本会产出不同字节却拿到同一个 hash，后写的那份会覆盖前一份，缩略图
 * 与消息里标注的 size 就此对不上。
 *
 * 返回 null 而不是降级兜底：路径前 16 位是内容地址，后端会重算 sha256 校验，
 * 编不出真哈希就没有合法路径可拼 —— 此时不存档，行为退回改动前（显示文件图标）。
 */
async function blobFingerprint(blob: Blob): Promise<string | null> {
  const subtle = globalThis.crypto?.subtle
  if (!subtle) return null
  try {
    const digest = await subtle.digest('SHA-256', await blob.arrayBuffer())
    return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('')
  } catch {
    return null
  }
}

/**
 * 拼出内容寻址的存档路径 `聊天图片/<sha256前16位>_<安全名>.<ext>`。
 *
 * 扩展名由 **blob.type** 推导而非 file.name：bmp/ico 会被转码成 PNG/JPEG，
 * 沿用原扩展名会让存档的 Content-Type 与 data URL 的 media_type 双双错位。
 * 内容寻址带来两个免费收益：前端**发请求前就知道 ref**（无需等响应回填，
 * 存档因此可以完全不阻塞发送），以及重复贴同一张图命中后端配额检查的
 * existing 早退，不占文件数/总量名额。
 */
async function buildChatArchiveRef(blob: Blob, fileName: string): Promise<string | null> {
  const ext = MIME_TO_EXT[(blob.type || '').toLowerCase()]
  if (!ext) return null
  const fp = await blobFingerprint(blob)
  if (!fp) return null
  // 后端字符集排除 / \ : * ? " ' < > | 与空白（" 会打破 <image name="..."> 的解析）
  const base = fileName.replace(/\.[^.]*$/, '')
  const safe = base.replace(/[/\s"'<>\\:*?|]+/g, '_').replace(/^_+/, '').slice(0, 40) || 'image'
  return `${CHAT_IMAGE_DIR}/${fp.slice(0, 16)}_${safe}.${ext}`
}

/** 单个附件的摄取状态 */
interface IngestItem {
  key: string          // `${batchId}:${index}`，跨批次并发时用于精确 patch
  name: string
  phase: 'queued' | 'working' | 'done' | 'skipped'
  detail?: string
}

interface IngestState {
  items: IngestItem[]
}

/** 格式化文件大小 */
function formatFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

// ── 文件上传按钮 ── 超限时直接阻断点击并内联提示；非多模态模型阻断图片上传
const FileUploadButton = memo(function FileUploadButton({ onFiles, disabled, maxFiles, currentCount, supportsVision }: {
  onFiles: (files: File[]) => void
  disabled?: boolean
  maxFiles: number
  currentCount: number
  supportsVision: boolean
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [blocked, setBlocked] = useState(false)
  const [blockMsg, setBlockMsg] = useState('')
  const blockTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // 清理定时器
  useEffect(() => () => { if (blockTimer.current) clearTimeout(blockTimer.current) }, [])

  const showBlock = useCallback((msg: string) => {
    setBlockMsg(msg)
    setBlocked(true)
    if (blockTimer.current) clearTimeout(blockTimer.current)
    blockTimer.current = setTimeout(() => setBlocked(false), 2500)
  }, [])

  const handleClick = useCallback(() => {
    // 超限阻断：不打开文件对话框，显示内联提示
    if (maxFiles > 0 && currentCount >= maxFiles) {
      showBlock(`已达上限 ${maxFiles} 个文件`)
      return
    }
    inputRef.current?.click()
  }, [maxFiles, currentCount, showBlock])

  const handleChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    const files = e.target.files
    if (files && files.length > 0) {
      const arr = Array.from(files)
      // 非多模态模型阻断图片上传
      if (!supportsVision) {
        const hasImage = arr.some(f => f.type.startsWith('image/'))
        if (hasImage) {
          showBlock('当前模型不支持图片，请选择多模态模型')
          if (inputRef.current) inputRef.current.value = ''
          return
        }
      }
      onFiles(arr)
    }
    if (inputRef.current) inputRef.current.value = ''
  }, [onFiles, supportsVision, showBlock])

  // 按钮是否禁用：全局禁用 或 管理员禁止上传
  const isDisabled = disabled || maxFiles <= 0
  // 剩余可上传数
  const remaining = maxFiles > 0 ? maxFiles - currentCount : 0

  return (
    <div style={{ position: 'relative', flexShrink: 0 }}>
      <input ref={inputRef} type="file" multiple onChange={handleChange} style={{ display: 'none' }} accept={supportsVision ? undefined : 'text/*,.txt,.md,.json,.yaml,.yml,.xml,.html,.css,.js,.ts,.py,.go,.rs,.c,.cpp,.java,.sh,.sql,.vue,.svg,.pdf,.docx'} />
      <button type="button" onClick={handleClick} disabled={isDisabled}
        title={maxFiles <= 0 ? '管理员已禁用文件上传' : !supportsVision ? '上传文件（当前模型不支持图片）' : remaining > 0 ? `上传文件 (还可添加 ${remaining} 个)` : `已达上限 ${maxFiles} 个文件`}
        aria-label="上传文件"
        style={{
          display: 'grid', placeItems: 'center', width: 28, height: 28, border: 'none', borderRadius: 999,
          background: blocked ? 'rgba(200,60,60,0.12)' : 'transparent',
          color: blocked ? '#c44' : isDisabled ? 'var(--text-tertiary)' : 'var(--text-tertiary)',
          cursor: isDisabled ? 'default' : 'pointer',
          transition: 'background 0.15s, color 0.15s, transform 0.15s',
          opacity: isDisabled ? 0.35 : 1,
          transform: blocked ? 'scale(0.92)' : 'scale(1)',
        }}
        onMouseEnter={e => { if (!isDisabled && !blocked) { e.currentTarget.style.background = 'var(--code-bg)'; e.currentTarget.style.color = 'var(--accent)' } }}
        onMouseLeave={e => { if (!blocked) { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--text-tertiary)' } }}>
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
          <path d="M11.5 4.5L6 10c-.83.83-2.17.83-3 0s-.83-2.17 0-3l5.5-5.5c1.38-1.38 3.62-1.38 5 0s1.38 3.62 0 5L8 12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none" />
        </svg>
      </button>
      {/* 内联阻断提示气泡（平面） */}
      <AnimatePresence>
        {blocked && (
          <motion.div
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 4 }}
            transition={{ duration: 0.18 }}
            style={{
              position: 'absolute', bottom: 'calc(100% + 8px)', left: '50%',
              transform: 'translateX(-50%)',
              whiteSpace: 'nowrap', padding: '6px 12px 6px 8px', borderRadius: 8,
              display: 'flex', alignItems: 'center', gap: 6,
              background: 'var(--card-bg-solid)',
              color: '#c44',
              fontSize: 11, fontWeight: 600, lineHeight: '16px',
              border: '1px solid rgba(200,60,60,0.3)',
              boxShadow: SHADOW_SM,
              pointerEvents: 'none', zIndex: 50,
            }}>
            <AlertTriangle size={12} strokeWidth={2} aria-hidden />
            {blockMsg}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
})

// ── 文件类型 → 颜色映射 ──
const FILE_TYPE_COLORS: Record<string, { color: string; label: string }> = {
  js: { color: '#f7df1e', label: 'JS' }, jsx: { color: '#61dafb', label: 'JSX' },
  ts: { color: '#3178c6', label: 'TS' }, tsx: { color: '#3178c6', label: 'TSX' },
  py: { color: '#3776ab', label: 'PY' }, java: { color: '#ed8b00', label: 'JV' },
  go: { color: '#00add8', label: 'GO' }, rs: { color: '#dea584', label: 'RS' },
  c: { color: '#a8b9cc', label: 'C' }, cpp: { color: '#00599c', label: 'C++' },
  html: { color: '#e34c26', label: 'HTML' }, css: { color: '#1572b6', label: 'CSS' },
  json: { color: '#cbcb41', label: 'JSON' }, yaml: { color: '#cb171e', label: 'YML' },
  yml: { color: '#cb171e', label: 'YML' }, md: { color: '#986638', label: 'MD' },
  sh: { color: '#4eaa25', label: 'SH' }, sql: { color: '#e38c00', label: 'SQL' },
  vue: { color: '#41b883', label: 'VUE' }, svg: { color: '#ffb13b', label: 'SVG' },
  txt: { color: '#999999', label: 'TXT' }, xml: { color: '#e3791a', label: 'XML' },
  pdf: { color: '#e53935', label: 'PDF' }, docx: { color: '#2b579a', label: 'DOC' },
}
const DEFAULT_FILE_COLOR = { color: 'var(--accent)', label: 'FILE' }

function getFileTypeInfo(name: string) {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  return FILE_TYPE_COLORS[ext] || DEFAULT_FILE_COLOR
}

// ── 已选文件列表 ── 平面卡片 + 文件类型色标
const AttachedFilesList = memo(function AttachedFilesList({ files, onRemove }: {
  files: AttachedFile[]
  onRemove: (index: number) => void
}) {
  if (files.length === 0) return null
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, padding: '4px 12px 0 16px' }}>
      {files.map((f, i) => {
        const typeInfo = getFileTypeInfo(f.name)
        return (
          <div key={i} style={{
            display: 'inline-flex', alignItems: 'center', gap: 8,
            padding: '5px 8px 5px 5px',
            borderRadius: 10,
            background: 'var(--card-bg-solid)',
            border: '1px solid var(--line)',
            boxShadow: SHADOW_SM,
            fontSize: 12, lineHeight: '18px', color: 'var(--text-secondary)',
            maxWidth: 260,
            transition: 'border-color 0.15s, box-shadow 0.15s',
          }}
            onMouseEnter={e => { e.currentTarget.style.borderColor = typeInfo.color }}
            onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--line)' }}>
            {/* 文件类型色标徽章 / 图片缩略图 */}
            {f.isImage && f.dataUrl ? (
              <div style={{
                width: 28, height: 28, flexShrink: 0, borderRadius: 7,
                overflow: 'hidden', position: 'relative',
                border: '1px solid var(--line)',
              }}>
                <img src={f.dataUrl} alt={f.name} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
              </div>
            ) : (
              <div style={{
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                width: 28, height: 28, flexShrink: 0,
                borderRadius: 7,
                background: typeInfo.color,
                color: '#fff', fontSize: 9, fontWeight: 700, letterSpacing: '0.02em',
                fontFamily: 'monospace',
              }}>
                {typeInfo.label}
              </div>
            )}
            {/* 文件名 + 大小 */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 0, minWidth: 0, flex: 1 }}>
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--text-primary)', fontWeight: 500, fontSize: 12 }}>{f.name}</span>
              <span style={{ fontSize: 10, color: 'var(--text-tertiary)', letterSpacing: '0.02em' }}>{formatFileSize(f.size)}</span>
            </div>
            {/* 移除按钮 */}
            <button type="button" onClick={() => onRemove(i)} aria-label="移除文件"
              style={{
                display: 'grid', placeItems: 'center', width: 20, height: 20, border: 'none',
                borderRadius: 6, background: 'transparent', color: 'var(--text-tertiary)',
                cursor: 'pointer', flexShrink: 0, padding: 0, transition: 'all 0.15s',
              }}
              onMouseEnter={e => { e.currentTarget.style.background = 'rgba(200,60,60,0.1)'; e.currentTarget.style.color = '#c44' }}
              onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--text-tertiary)' }}>
              <svg width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden><path d="M3.5 3.5l5 5M8.5 3.5l-5 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
            </button>
          </div>
        )
      })}
    </div>
  )
})

// ── 附件摄取进度（优化11）──
// 多图并行压缩时单张可达 1~2.5s，原先串行处理全程零反馈也无法中止。
// HeroPage 与 ActiveInputCard 共用同一份面板，取代只能显示单个文件名的 parsingDocName。
const IngestProgress = memo(function IngestProgress({ ingest, onCancel }: {
  ingest: IngestState
  onCancel: () => void
}) {
  const total = ingest.items.length
  const done = ingest.items.filter(i => i.phase === 'done' || i.phase === 'skipped').length
  if (total === 0) return null
  return (
    <div style={{ width: '100%', maxWidth: COMPOSER_CARD_MAX_WIDTH, marginBottom: 6, padding: '5px 8px', borderRadius: 8, background: 'var(--code-bg)', color: 'var(--text-secondary)', fontSize: 12, lineHeight: '18px', display: 'flex', flexDirection: 'column', gap: 5 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{ width: 6, height: 6, borderRadius: '50%', flexShrink: 0, background: 'var(--accent)', animation: 'dsh-pulse-soft 2s ease-in-out infinite' }} />
        <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
          正在处理附件 {done}/{total}…
        </span>
        <button type="button" onClick={onCancel} aria-label="取消附件处理"
          style={{ flexShrink: 0, padding: '0 7px', height: 20, border: '1px solid var(--line)', borderRadius: 6, background: 'var(--card-bg-solid)', color: 'var(--text-secondary)', fontSize: 11, lineHeight: '18px', fontFamily: 'inherit', cursor: 'pointer' }}>
          取消
        </button>
      </div>
      <div style={{ height: 3, borderRadius: 2, background: 'var(--line)', overflow: 'hidden' }}>
        <div style={{ height: '100%', width: `${(done / total) * 100}%`, background: 'var(--accent)', transition: 'width 0.2s ease' }} />
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
        {ingest.items.map(it => (
          <div key={it.key} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: it.phase === 'skipped' ? 'var(--text-tertiary)' : 'var(--text-secondary)' }}>
            <span aria-hidden style={{ width: 10, flexShrink: 0, textAlign: 'center', color: it.phase === 'done' ? 'var(--accent)' : 'var(--text-tertiary)' }}>
              {it.phase === 'done' ? '✓' : it.phase === 'skipped' ? '×' : it.phase === 'working' ? '…' : '·'}
            </span>
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{it.name}</span>
            {it.detail && <span style={{ flexShrink: 0, marginLeft: 'auto', color: 'var(--text-tertiary)' }}>{it.detail}</span>}
          </div>
        ))}
      </div>
    </div>
  )
})

const CHAT_CONTENT_WIDTH = 770
const COMPOSER_CARD_MAX_WIDTH = CHAT_CONTENT_WIDTH + 32
const COMPOSER_SIDE_CLEARANCE = 16
const FOLLOW_THRESHOLD = 24
const SIDEBAR_WIDTH = 320
// 稳定引用：loadedSkills 在"无会话/该会话没加载技能"时若写 `?? []` 会每轮渲染产生新数组，
// SessionSidebar 的 memo 就白包了
const EMPTY_LOADED_SKILLS: string[] = []

// ── token 格式化 (仿 DSH formatTokens) ──
function formatTokens(n: number): string {
  const scaled = (v: number): string =>
    v >= 100 ? String(Math.round(v)) : String(Math.round(v * 10) / 10)
  if (n < 1_000) return String(n)
  if (n < 1_000_000) return `${scaled(n / 1_000)}K`
  return `${scaled(n / 1_000_000)}M`
}

// ── ContextMeter — 上下文占用百分比环 ──
const RING_RADIUS = 5.5
const RING_CIRCUMFERENCE = 2 * Math.PI * RING_RADIUS

function ContextMeter({ pressure }: { pressure: ContextPressure | null }) {
  const [open, setOpen] = useState(false)
  const rootRef = useRef<HTMLSpanElement | null>(null)

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: PointerEvent) => {
      if (e.target instanceof Node && rootRef.current?.contains(e.target) === true) return
      setOpen(false)
    }
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  // 即使没有上下文数据，也显示一个初始的空环（让用户知道这个功能存在）
  if (!pressure || pressure.contextWindow === 0) {
    return (
      <button type="button" disabled aria-label="上下文未使用"
        style={{ display: 'grid', placeItems: 'center', width: 28, height: 28, border: 'none', borderRadius: 999, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'default', opacity: 0.5 }}>
        <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden>
          <circle cx="7" cy="7" r={RING_RADIUS} fill="none" stroke="var(--line)" strokeWidth="2" />
        </svg>
      </button>
    )
  }
  const percent = pressure.percent
  const reading = `${percent}%`
  const ROWS = [
    { key: 'systemTokens', label: '系统', color: '#6b8eab', tokens: pressure.systemTokens },
    { key: 'toolsTokens', label: '工具', color: '#a78bfa', tokens: pressure.toolsTokens },
    { key: 'messageTokens', label: '对话', color: '#3b82f6', tokens: pressure.messageTokens },
  ] as const
  const breakdownTotal = ROWS.reduce((s, r) => s + r.tokens, 0)
  const segments = breakdownTotal === 0
    ? [{ key: 'total', color: undefined as string | undefined, width: percent }]
    : ROWS.map(r => ({ key: r.key, color: r.color, width: percent * r.tokens / breakdownTotal })).filter(s => s.width > 0)

  // DSH 风格: 智能选取信息
  const hasSelectionInfo = pressure.historyTotal !== undefined && pressure.historyTotal > 0
  const dropped = pressure.historyDropped ?? 0
  const selected = pressure.historySelected ?? 0
  const total = pressure.historyTotal ?? 0
  // DSH 风格: provider 校准 vs 估算
  const hasPressureTokens = pressure.pressureTokens != null

  return (
    <span ref={rootRef} style={{ position: 'relative', display: 'inline-flex' }}>
      <button type="button" onClick={() => setOpen(!open)} aria-label={`上下文已用 ${reading}`}
        style={{ display: 'grid', placeItems: 'center', width: 28, height: 28, border: 'none', borderRadius: 999, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer', transition: 'background 0.15s' }}
        onMouseEnter={e => { e.currentTarget.style.background = 'var(--code-bg)' }}
        onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}>
        <svg viewBox="0 0 14 14" width="14" height="14" aria-hidden>
          <circle cx="7" cy="7" r={RING_RADIUS} fill="none" stroke="var(--line)" strokeWidth="2" />
          <circle cx="7" cy="7" r={RING_RADIUS} fill="none" stroke={percent > 80 ? '#c44' : 'var(--accent)'} strokeWidth="2" strokeLinecap="round"
            strokeDasharray={`${RING_CIRCUMFERENCE * percent / 100} ${RING_CIRCUMFERENCE}`} transform="rotate(-90 7 7)" style={{ transition: 'stroke-dasharray 0.3s ease' }} />
        </svg>
      </button>
      <AnimatePresence>
        {open && (
          <motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 4 }} transition={{ duration: 0.15 }} role="dialog"
            style={{ position: 'absolute', bottom: 'calc(100% + 8px)', right: 0, zIndex: 100, boxSizing: 'border-box', width: 264, padding: 12, border: '1px solid var(--line)', borderRadius: 12, background: 'var(--card-bg-solid)', boxShadow: '0 8px 32px rgba(0,0,0,0.12)', fontSize: 12, lineHeight: '20px', color: 'var(--text-secondary)', cursor: 'default' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{ color: 'var(--text-tertiary)' }}>上下文已用</span>
              <span style={{ fontWeight: 500, color: 'var(--text-primary)' }}>{reading}</span>
              <span style={{ marginLeft: 'auto', fontWeight: 500, fontVariantNumeric: 'tabular-nums', color: 'var(--text-primary)' }}>~{formatTokens(pressure.usedTokens)} / {formatTokens(pressure.contextWindow)}</span>
            </div>
            <div style={{ display: 'flex', gap: 1, margin: '10px 0 12px', height: 4, borderRadius: 999, background: 'var(--code-bg)', overflow: 'hidden' }}>
              {segments.map(seg => (<div key={seg.key} style={{ flex: 'none', minWidth: 2, height: '100%', borderRadius: 1, width: `${seg.width}%`, background: seg.color ?? 'var(--accent)' }} />))}
            </div>
            {breakdownTotal > 0 && (
              <dl style={{ margin: '6px 0 0', display: 'grid', gap: 2 }}>
                {ROWS.map(row => (
                  <div key={row.key} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '2px 0' }}>
                    <dt style={{ display: 'flex', alignItems: 'center', gap: 6, color: 'var(--text-secondary)' }}>
                      <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 2, background: row.color }} />{row.label}
                    </dt>
                    <dd style={{ margin: 0, fontVariantNumeric: 'tabular-nums', color: 'var(--text-primary)' }}>~{formatTokens(row.tokens)}</dd>
                  </div>
                ))}
              </dl>
            )}
            {/* DSH 风格: provider 校准信息 */}
            {hasPressureTokens && (
              <div style={{ marginTop: 8, paddingTop: 8, borderTop: '1px solid var(--line)', fontSize: 11, color: 'var(--text-tertiary)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <span>Provider 校准</span>
                  <span style={{ fontVariantNumeric: 'tabular-nums', color: 'var(--text-secondary)' }}>{formatTokens(pressure.pressureTokens!)} tokens</span>
                </div>
                {pressure.projectedTokens != null && (
                  <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 2 }}>
                    <span>下次请求预估</span>
                    <span style={{ fontVariantNumeric: 'tabular-nums', color: 'var(--text-secondary)' }}>~{formatTokens(pressure.projectedTokens)} tokens</span>
                  </div>
                )}
              </div>
            )}
            {/* DSH 风格: 智能选取信息 */}
            {hasSelectionInfo && (
              <div style={{ marginTop: 8, paddingTop: 8, borderTop: '1px solid var(--line)', fontSize: 11, color: 'var(--text-tertiary)' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between' }}>
                  <span>历史消息</span>
                  <span style={{ color: 'var(--text-secondary)' }}>
                    {selected}/{total} 条{dropped > 0 ? ` (丢弃 ${dropped})` : ''}
                  </span>
                </div>
                {dropped > 0 && (
                  <div style={{ marginTop: 4, color: 'var(--text-tertiary)' }}>
                    已自动丢弃较早的 {dropped} 条消息以保持在上下文窗口内
                  </div>
                )}
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </span>
  )
}

// ── 模型选择器 ──
// ── 模型选择器 ── 3D 立体自定义下拉菜单
// ── 多模态标记（平面）── 眼睛图标 + 文字，表示支持图片输入
function VisionBadge3D({ style }: { style?: React.CSSProperties }) {
  return (
    <span title="多模态模型 — 支持图片输入"
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 3,
        height: 18, padding: '0 6px', borderRadius: 999,
        flexShrink: 0,
        background: 'rgba(86,156,210,0.10)',
        border: '1px solid rgba(86,156,210,0.25)',
        color: 'rgba(86,156,210,0.95)',
        ...style,
      }}>
      <Eye size={11} strokeWidth={1.75} aria-hidden />
      <span style={{ fontSize: 9, fontWeight: 600, letterSpacing: '0.02em', lineHeight: 1 }}>多模态</span>
    </span>
  )
}

function ModelSelector({ models, selectedModelId, setSelectedModelId, pendingHasImage }: {
  models: ReturnType<typeof useChat>['models']
  selectedModelId: number | undefined
  setSelectedModelId: ReturnType<typeof useChat>['setSelectedModelId']
  pendingHasImage: boolean
}) {
  const [open, setOpen] = useState(false)
  const [hoveredId, setHoveredId] = useState<number | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)

  // 点击外部关闭
  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  // ESC 关闭
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('keydown', handler)
    return () => document.removeEventListener('keydown', handler)
  }, [open])

  if (models.length === 0) return null
  const selected = models.find(m => m.id === selectedModelId) || models.find(m => m.is_default) || models[0]

  return (
    <div ref={wrapRef} style={{ position: 'relative', flexShrink: 0 }}>
      {/* 触发按钮（平面胶囊） */}
      <button type="button" onClick={() => setOpen(v => !v)}
        style={{
          display: 'flex', alignItems: 'center', gap: 6, height: 30,
          padding: '0 8px 0 6px', border: '1px solid var(--line)', borderRadius: 10,
          background: open ? 'var(--code-bg)' : 'var(--card-bg-solid)',
          cursor: 'pointer', fontFamily: 'inherit', fontSize: 12, fontWeight: 500,
          color: 'var(--text-secondary)', whiteSpace: 'nowrap',
          boxShadow: SHADOW_SM,
          transition: 'border-color 0.15s, box-shadow 0.15s, background 0.15s',
        }}
        onMouseEnter={e => { if (!open) { e.currentTarget.style.borderColor = 'var(--accent)' } }}
        onMouseLeave={e => { if (!open) { e.currentTarget.style.borderColor = 'var(--line)' } }}>
        {/* 模型图标（平面） */}
        <span style={{
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          width: 18, height: 18, flexShrink: 0, borderRadius: 5,
          background: 'var(--accent)', color: '#fff',
        }}>
          <Zap size={11} strokeWidth={2} aria-hidden />
        </span>
        {/* 模型名称 — 不截断，自动撑开 */}
        <span style={{ overflow: 'visible', textOverflow: 'clip', maxWidth: 'none' }}>{selected?.display_name}</span>
        {selected?.is_default && <span style={{ color: 'var(--accent)', fontSize: 10, flexShrink: 0 }}>★</span>}
        {/* 多模态 3D 标记 */}
        {selected?.supports_vision && <VisionBadge3D />}
        {/* 下拉箭头 — 旋转动画 */}
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden
          style={{ flexShrink: 0, transition: 'transform 0.2s', transform: open ? 'rotate(180deg)' : 'rotate(0deg)' }}>
          <path d="M2.5 3.5L5 6L7.5 3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none" />
        </svg>
      </button>

      {/* 下拉面板（平面浮层） */}
      {open && (
        <div style={{
          position: 'absolute', bottom: 'calc(100% + 6px)', right: 0, zIndex: 100,
          minWidth: '100%', maxWidth: 280,
          padding: 4, borderRadius: 12,
          background: 'var(--card-bg-solid)',
          border: '1px solid var(--line)',
          boxShadow: SHADOW_MD,
          display: 'flex', flexDirection: 'column', gap: 2,
          maxHeight: 320, overflowY: 'auto',
        }}>
          {models.map(m => {
            const isActive = m.id === selected?.id
            const isHovered = m.id === hoveredId
            return (
              <div key={m.id}
                onClick={() => {
                  // ── 本轮多模态门禁 ──
                  // 只有本轮待发附件里真的有图片时才要求 vision 模型。历史里的图片已被
                  // slimHistoryAttachments 换成占位符，纯文本模型读得懂，不构成切换限制。
                  if (pendingHasImage && !m.supports_vision) return
                  setSelectedModelId(m.id); setOpen(false)
                }}
                onMouseEnter={() => setHoveredId(m.id)} onMouseLeave={() => setHoveredId(null)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 8, padding: '7px 8px', borderRadius: 8,
                  cursor: pendingHasImage && !m.supports_vision ? 'not-allowed' : 'pointer', whiteSpace: 'nowrap',
                  background: isActive ? 'var(--code-bg)' : isHovered ? 'var(--surface-bg)' : 'transparent',
                  border: isActive ? '1px solid var(--accent)' : '1px solid transparent',
                  opacity: pendingHasImage && !m.supports_vision ? 0.4 : 1,
                  transition: 'background 0.1s, border 0.1s, opacity 0.15s',
                }}
                title={pendingHasImage && !m.supports_vision ? '本轮已附加图片，需先移除图片或改用支持多模态的模型' : undefined}>
                {/* 模型类型小图标（平面） */}
                <div style={{
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  width: 16, height: 16, flexShrink: 0, borderRadius: 4,
                  background: isActive ? 'var(--accent)' : 'var(--code-bg)',
                  transition: 'background 0.15s',
                }}>
                  <Zap size={9} strokeWidth={2} aria-hidden
                    style={{ color: isActive ? '#fff' : 'var(--text-tertiary)' }} />
                </div>
                {/* 模型名称 — 完整显示不截断 */}
                <span style={{
                  fontSize: 12, fontWeight: isActive ? 600 : 500,
                  color: isActive ? 'var(--accent)' : 'var(--text-secondary)',
                  overflow: 'visible', textOverflow: 'clip',
                }}>{m.display_name}</span>
                {m.is_default && <span style={{ color: 'var(--accent)', fontSize: 10, flexShrink: 0, marginLeft: 'auto' }}>默认</span>}
                {m.supports_vision && <VisionBadge3D style={{ marginLeft: m.is_default ? 6 : 'auto' }} />}
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}

// ── 会话侧边栏 ──
interface SidebarProps {
  sessions: ChatSession[]
  activeSessionId: string | undefined
  collapsed: boolean
  onToggle: () => void
  onNewSession: () => void
  onSwitch: (sessionId: string) => void
  onDelete: (sessionPk: number) => void
  onRename: (sessionPk: number, title: string) => void
  sidebarTab: 'sessions' | 'workspace' | 'skills'
  onTabChange: (tab: 'sessions' | 'workspace' | 'skills') => void
  mode: 'chat' | 'agent'   // 当前会话模式（纯聊天模式隐藏工作区/技能 tab）
  skills: ChatSkillInfo[]
  loadedSkills: string[]
  onUseSkill: (name: string) => void       // 点击技能 = 往输入框填入 /技能名（无需先创建会话）
  onUnloadSkill: (name: string) => void
  skillBudgetChars: number                 // 已加载技能全文的字符预算（当前模型 context_length × 0.5）
}

// memo：AIChat 在流式输出期间每个 token 都会重渲染，侧边栏（会话列表 + 技能 + 工作区面板）
// 不该跟着整棵重算。配合调用点把 onToggle 提成 useCallback，props 才能稳定。
const SessionSidebar = memo(function SessionSidebar({ sessions, activeSessionId, collapsed, onToggle, onNewSession, onSwitch, onDelete, onRename, sidebarTab, onTabChange, mode, skills, loadedSkills, onUseSkill, onUnloadSkill, skillBudgetChars }: SidebarProps) {
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editText, setEditText] = useState('')
  const [hoveredId, setHoveredId] = useState<number | null>(null)

  if (collapsed) return null

  const submitRename = (pk: number) => {
    if (editText.trim()) onRename(pk, editText.trim())
    setEditingId(null)
  }

  return (
    <motion.div
      initial={{ width: 0, opacity: 0 }} animate={{ width: SIDEBAR_WIDTH, opacity: 1 }} exit={{ width: 0, opacity: 0 }} transition={{ duration: 0.2 }}
      style={{ flex: 'none', width: SIDEBAR_WIDTH, height: '100%', display: 'flex', flexDirection: 'column', borderRight: '1px solid var(--line)', background: 'var(--card-bg-solid)', overflow: 'hidden' }}>
      {/* 侧边栏头部 — 3D 立体 logo + 标题 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '18px 18px 14px', flex: 'none' }}>
        <SidebarLogo />
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: 1, minWidth: 0 }}>
          <span style={{ fontSize: 17, fontWeight: 700, color: 'var(--text-primary)', fontFamily: 'var(--font-serif)', letterSpacing: '0.04em' }}>智语知你</span>
          <span style={{ fontSize: 12, color: 'var(--text-tertiary)', letterSpacing: '0.06em' }}>智能助理 · 使命必达</span>
        </div>
        <button type="button" onClick={onToggle} aria-label="折叠侧边栏"
          style={{ display: 'grid', placeItems: 'center', width: 32, height: 32, border: 'none', borderRadius: 8, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer', transition: 'background 0.15s', flexShrink: 0 }}
          onMouseEnter={e => { e.currentTarget.style.background = 'var(--code-bg)' }} onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}>
          <svg width="18" height="18" viewBox="0 0 16 16" fill="none" aria-hidden><rect x="2" y="3" width="12" height="10" rx="2" stroke="currentColor" strokeWidth="1.5" fill="none" /><line x1="6" y1="3" x2="6" y2="13" stroke="currentColor" strokeWidth="1.5" /></svg>
        </button>
      </div>
      {/* Tab 切换 — 会话 / 工作区（纯聊天模式隐藏工作区 tab） */}
      <div style={{ display: 'flex', padding: '0 14px 10px', gap: 6, flex: 'none' }}>
        <button type="button" onClick={() => onTabChange('sessions')}
          style={{ flex: 1, padding: '7px 10px', fontSize: 13, fontWeight: 500, border: '1px solid', borderColor: sidebarTab === 'sessions' ? 'var(--accent)' : 'var(--line)', borderRadius: 8, background: sidebarTab === 'sessions' ? 'var(--code-bg)' : 'transparent', color: sidebarTab === 'sessions' ? 'var(--accent)' : 'var(--text-tertiary)', cursor: 'pointer', fontFamily: 'inherit', transition: 'all 0.15s' }}>
          💬 会话
        </button>
        {mode === 'agent' && (
          <>
            <button type="button" onClick={() => onTabChange('workspace')}
              style={{ flex: 1, padding: '7px 10px', fontSize: 13, fontWeight: 500, border: '1px solid', borderColor: sidebarTab === 'workspace' ? 'var(--accent)' : 'var(--line)', borderRadius: 8, background: sidebarTab === 'workspace' ? 'var(--code-bg)' : 'transparent', color: sidebarTab === 'workspace' ? 'var(--accent)' : 'var(--text-tertiary)', cursor: 'pointer', fontFamily: 'inherit', transition: 'all 0.15s', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 5 }}>
              <FolderOpen size={13} strokeWidth={1.75} aria-hidden /> 工作区
            </button>
            <button type="button" onClick={() => onTabChange('skills')}
              style={{ flex: 1, padding: '7px 10px', fontSize: 13, fontWeight: 500, border: '1px solid', borderColor: sidebarTab === 'skills' ? 'var(--accent)' : 'var(--line)', borderRadius: 8, background: sidebarTab === 'skills' ? 'var(--code-bg)' : 'transparent', color: sidebarTab === 'skills' ? 'var(--accent)' : 'var(--text-tertiary)', cursor: 'pointer', fontFamily: 'inherit', transition: 'all 0.15s', display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 5 }}>
              <Puzzle size={13} strokeWidth={1.75} aria-hidden /> 技能
            </button>
          </>
        )}
      </div>
      {sidebarTab === 'sessions' || mode === 'chat' ? (
        <>
          <div style={{ padding: '0 14px 10px', flex: 'none' }}>
            <button type="button" onClick={onNewSession}
              style={{
                display: 'flex', alignItems: 'center', gap: 10, width: '100%',
                padding: '10px 16px', fontSize: 14, fontWeight: 600,
                border: 'none', borderRadius: 10,
                background: 'var(--accent)',
                color: '#fff', cursor: 'pointer', fontFamily: 'inherit',
                boxShadow: SHADOW_SM,
                transition: 'filter 0.15s',
              }}
              onMouseEnter={e => { e.currentTarget.style.filter = 'brightness(1.08)' }}
              onMouseLeave={e => { e.currentTarget.style.filter = 'none' }}>
              <svg width="16" height="16" viewBox="0 0 14 14" fill="none" aria-hidden><path d="M7 2v10M2 7h10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /></svg>
              新建对话
            </button>
          </div>
          {/* 启用 Agent 工具开关已由"模式"概念取代：模式徽章在主界面切换，见 ModeBadge */}
          <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: '4px 10px 10px', scrollbarGutter: 'stable' }}>
            {sessions.length === 0 ? (
              <div style={{ textAlign: 'center', padding: '30px 12px', color: 'var(--text-tertiary)', fontSize: 14 }}>暂无对话</div>
            ) : sessions.map(s => {
              const isActive = s.session_id === activeSessionId
              const isHovered = s.id === hoveredId
              const isEditing = editingId === s.id
              return (
                <div key={s.id} onClick={() => !isEditing && onSwitch(s.session_id)} onMouseEnter={() => setHoveredId(s.id)} onMouseLeave={() => setHoveredId(null)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 8,
                    padding: '10px 12px', marginBottom: 4, borderRadius: 10,
                    cursor: isEditing ? 'default' : 'pointer',
                    background: isActive
                      ? 'var(--code-bg)'
                      : isHovered ? 'var(--surface-bg)' : 'transparent',
                    border: isActive ? '1px solid color-mix(in srgb, var(--accent) 30%, transparent)' : '1px solid transparent',
                    transition: 'background 0.12s, border 0.12s',
                  }}>
                  {isEditing ? (
                    <input autoFocus value={editText} onChange={e => setEditText(e.target.value)}
                      onKeyDown={e => { if (e.key === 'Enter') submitRename(s.id); if (e.key === 'Escape') setEditingId(null) }}
                      onBlur={() => submitRename(s.id)} onClick={e => e.stopPropagation()}
                      style={{ flex: 1, padding: '3px 8px', border: '1px solid var(--accent)', borderRadius: 4, fontSize: 14, fontFamily: 'inherit', background: 'var(--bg)', color: 'var(--text-primary)', outline: 'none' }} />
                  ) : (
                    <>
                      <span style={{ flex: 1, fontSize: 14, lineHeight: '22px', color: isActive ? 'var(--text-primary)' : 'var(--text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.title || '新对话'}</span>
                    </>
                  )}
                  {isHovered && !isEditing && (
                    <div style={{ display: 'flex', gap: 2, flex: 'none' }}>
                      <button type="button" onClick={e => { e.stopPropagation(); setEditingId(s.id); setEditText(s.title) }} aria-label="重命名"
                        style={{ display: 'grid', placeItems: 'center', width: 26, height: 26, border: 'none', borderRadius: 4, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer' }}>
                        <svg width="14" height="14" viewBox="0 0 12 12" fill="none" aria-hidden><path d="M8.5 2L10 3.5L4 9.5L2 10l.5-2L8.5 2z" stroke="currentColor" strokeWidth="1.2" strokeLinejoin="round" /></svg>
                      </button>
                      <button type="button" onClick={e => { e.stopPropagation(); if (confirm('确定删除此对话？')) onDelete(s.id) }} aria-label="删除"
                        style={{ display: 'grid', placeItems: 'center', width: 26, height: 26, border: 'none', borderRadius: 4, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer' }}>
                        <svg width="14" height="14" viewBox="0 0 12 12" fill="none" aria-hidden><path d="M2 3.5h8M4.5 3.5V2h3v1.5M3.5 3.5v6h5v-6" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" /></svg>
                      </button>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </>
      ) : sidebarTab === 'skills' ? (
        <SkillsPanel skills={skills} loadedSkills={loadedSkills} onUseSkill={onUseSkill} onUnloadSkill={onUnloadSkill} budgetChars={skillBudgetChars} />
      ) : (
        <WorkspacePanel />
      )}
    </motion.div>
  )
})

// ── 技能斜杠命令工具函数 ──
// 点击技能 = 往输入框填入 `/技能名 `，模型据此调用 skill 工具加载完整指令。
// 一条消息只允许一个技能：填入时替换已有技能 token，发送前再兜底校验一次。
const SKILL_TOKEN_RE = /(^|\s)\/([A-Za-z0-9_-]+)/g

/** 文本中命中已知技能名的 `/xxx` token（按出现顺序，含重复） */
function findSkillTokens(text: string, skillNames: Set<string>): string[] {
  const hits: string[] = []
  for (const m of text.matchAll(SKILL_TOKEN_RE)) {
    if (skillNames.has(m[2])) hits.push(m[2])
  }
  return hits
}

/** 移除文本中所有已知技能 token，保留其余内容（填入新技能前先清掉旧的） */
function stripSkillTokens(text: string, skillNames: Set<string>): string {
  return text
    .replace(SKILL_TOKEN_RE, (whole, lead: string, name: string) => (skillNames.has(name) ? lead : whole))
    .replace(/[ \t]{2,}/g, ' ')
    .trim()
}

/** 字符数紧凑显示（技能体积提示用） */
function formatChars(n: number): string {
  if (n >= 10000) return `${(n / 10000).toFixed(1)} 万`
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`
  return String(n)
}

// ── 技能面板 — 分类下拉 + 搜索 + 体积/预算提示 + 点击填入斜杠命令 ──
// Agent 模式侧边栏"技能"tab。点击「使用技能」把 `/技能名 ` 填进输入框，
// 不需要先创建会话；发送后模型调用 skill 工具加载完整指令并写入会话 loadedSkills。
// 已加载技能常驻会话 system prompt，所以对已加载技能只提供「取消加载」。
function SkillsPanel({ skills, loadedSkills, onUseSkill, onUnloadSkill, budgetChars }: {
  skills: ChatSkillInfo[]
  loadedSkills: string[]
  onUseSkill: (name: string) => void
  onUnloadSkill: (name: string) => void
  budgetChars: number
}) {
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('')   // '' = 全部分类，'__none__' = 未分类
  const loaded = new Set(loadedSkills)

  const catOf = (s: ChatSkillInfo) => (s.category || '').trim()
  const categories = Array.from(new Set(skills.map(catOf).filter(Boolean))).sort()
  const uncategorizedCount = skills.filter(s => !catOf(s)).length

  const q = query.trim().toLowerCase()
  const visible = skills
    .filter(s => {
      if (category === '__none__') { if (catOf(s)) return false }
      else if (category && catOf(s) !== category) return false
      if (!q) return true
      return s.name.toLowerCase().includes(q) || (s.description || '').toLowerCase().includes(q)
    })
    // 已加载置顶，其余按名称稳定排序（优化1）
    .sort((a, b) => {
      const la = loaded.has(a.name) ? 0 : 1
      const lb = loaded.has(b.name) ? 0 : 1
      if (la !== lb) return la - lb
      return a.name.localeCompare(b.name)
    })

  // 预算占用（优化2）：后端 LOADED_SKILLS_BUDGET_RATIO=0.5，
  // 超预算时技能正文会被按边界截断——这里把它变成可见状态而不是静默发生。
  const loadedChars = skills.filter(s => loaded.has(s.name)).reduce((sum, s) => sum + (s.content_chars || 0), 0)
  const pct = budgetChars > 0 ? Math.round(loadedChars / budgetChars * 100) : 0
  const overBudget = budgetChars > 0 && loadedChars > budgetChars
  const barColor = overBudget ? '#e5484d' : pct >= 80 ? '#e0a33e' : 'var(--accent)'

  return (
    <div style={{ flex: 1, overflowY: 'auto', overflowX: 'hidden', padding: '10px 12px', scrollbarGutter: 'stable' }}>
      <div style={{ fontSize: 12, color: 'var(--text-tertiary)', lineHeight: '18px', marginBottom: 10 }}>
        点击「使用技能」会把 <span style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: 11.5, color: 'var(--text-secondary)' }}>/技能名</span> 填入输入框，补充需求后发送即可。一条消息只能使用一个技能。
      </div>

      {/* 预算占用条 — 仅在已有技能加载时出现 */}
      {loadedSkills.length > 0 && (
        <div style={{ marginBottom: 10, padding: '8px 10px', borderRadius: 8, background: 'var(--code-bg)', border: '1px solid var(--line)' }}>
          <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 8, fontSize: 11.5, color: 'var(--text-secondary)', marginBottom: 6 }}>
            <span>已加载 {loadedSkills.length} 个技能</span>
            <span style={{ fontVariantNumeric: 'tabular-nums' }}>
              约 {formatChars(loadedChars)}{budgetChars > 0 ? ` / ${formatChars(budgetChars)} 字符 (${pct}%)` : ' 字符'}
            </span>
          </div>
          <div style={{ height: 4, borderRadius: 999, background: 'var(--line)', overflow: 'hidden' }}>
            <div style={{ height: '100%', width: `${Math.min(100, pct)}%`, background: barColor, borderRadius: 999, transition: 'width 0.2s, background 0.2s' }} />
          </div>
          {overBudget && (
            <div style={{ marginTop: 6, fontSize: 11, color: '#e5484d', lineHeight: '16px' }}>
              已超出注入预算，超出部分的技能正文会被截断为按需读取。建议取消加载暂时不用的技能。
            </div>
          )}
        </div>
      )}

      {/* 搜索（优化1） */}
      <div style={{ position: 'relative', marginBottom: 8 }}>
        <input
          type="text" value={query} onChange={e => setQuery(e.target.value)}
          placeholder="搜索技能名称或描述..." aria-label="搜索技能"
          style={{ width: '100%', boxSizing: 'border-box', padding: '7px 28px 7px 10px', fontSize: 12.5, fontFamily: 'inherit', color: 'var(--text-primary)', background: 'var(--card-bg-solid)', border: '1px solid var(--line)', borderRadius: 8, outline: 'none' }}
        />
        {query && (
          <button type="button" onClick={() => setQuery('')} aria-label="清空搜索"
            style={{ position: 'absolute', right: 6, top: '50%', transform: 'translateY(-50%)', width: 18, height: 18, display: 'grid', placeItems: 'center', border: 'none', borderRadius: 999, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer', fontSize: 14, lineHeight: 1, padding: 0 }}>×</button>
        )}
      </div>

      {/* 分类下拉 */}
      <select value={category} onChange={e => setCategory(e.target.value)} aria-label="按分类筛选技能"
        style={{ width: '100%', boxSizing: 'border-box', padding: '7px 10px', marginBottom: 10, fontSize: 12.5, fontFamily: 'inherit', color: 'var(--text-primary)', background: 'var(--card-bg-solid)', border: '1px solid var(--line)', borderRadius: 8, outline: 'none', cursor: 'pointer' }}>
        <option value="">全部分类（{skills.length}）</option>
        {categories.map(c => (
          <option key={c} value={c}>{c}（{skills.filter(s => catOf(s) === c).length}）</option>
        ))}
        {uncategorizedCount > 0 && <option value="__none__">未分类（{uncategorizedCount}）</option>}
      </select>

      {skills.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '30px 12px', color: 'var(--text-tertiary)', fontSize: 14 }}>
          <div style={{ fontSize: 28, marginBottom: 8 }}>🧩</div>
          暂无可用技能（可在管理端添加）
        </div>
      ) : visible.length === 0 ? (
        <div style={{ textAlign: 'center', padding: '24px 12px', color: 'var(--text-tertiary)', fontSize: 13 }}>
          <div style={{ marginBottom: 8 }}>没有匹配的技能</div>
          <button type="button" onClick={() => { setQuery(''); setCategory('') }}
            style={{ padding: '5px 12px', border: '1px solid var(--line)', borderRadius: 6, background: 'var(--card-bg-solid)', color: 'var(--text-secondary)', fontSize: 12, cursor: 'pointer', fontFamily: 'inherit' }}>清空筛选</button>
        </div>
      ) : visible.map(s => {
        const isLoaded = loaded.has(s.name)
        const cat = catOf(s)
        return (
          <div key={s.name} style={{ padding: '10px 12px', marginBottom: 8, borderRadius: 10, border: '1px solid', borderColor: isLoaded ? 'var(--accent)' : 'var(--line)', background: isLoaded ? 'var(--code-bg)' : 'var(--card-bg-solid)' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
              <span style={{ fontWeight: 600, fontSize: 13, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {s.name}{s.has_resources ? ' 📁' : ''}
              </span>
              {cat && (
                <span style={{ fontSize: 10, padding: '1px 6px', borderRadius: 999, background: 'rgba(70,110,200,0.1)', color: '#4a6ec8', border: '1px solid rgba(70,110,200,0.22)', flexShrink: 0 }}>{cat}</span>
              )}
              {isLoaded && (
                <span style={{ fontSize: 10.5, padding: '2px 6px', borderRadius: 999, background: 'rgba(74,136,72,0.12)', color: '#4a8', border: '1px solid rgba(74,136,72,0.3)', flexShrink: 0, marginLeft: 'auto' }}>已加载</span>
              )}
            </div>
            <div style={{ fontSize: 12, color: 'var(--text-secondary)', lineHeight: '18px', margin: '4px 0 6px' }}>{s.description}</div>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
              {/* 体积提示（优化2）：加载前就知道这个技能要吃多少上下文 */}
              <span style={{ fontSize: 11, color: 'var(--text-tertiary)', fontVariantNumeric: 'tabular-nums' }}>
                {s.content_chars ? `正文约 ${formatChars(s.content_chars)} 字符` : '体积未知'}
              </span>
              {isLoaded ? (
                <button type="button" onClick={() => onUnloadSkill(s.name)}
                  style={{ padding: '4px 12px', border: 'none', borderRadius: 6, background: 'var(--line)', color: 'var(--text-tertiary)', fontSize: 12, cursor: 'pointer', fontFamily: 'inherit', flexShrink: 0 }}>取消加载</button>
              ) : (
                <button type="button" onClick={() => onUseSkill(s.name)}
                  style={{ padding: '4px 12px', border: 'none', borderRadius: 6, background: 'var(--accent)', color: '#fff', fontSize: 12, cursor: 'pointer', fontFamily: 'inherit', flexShrink: 0 }}>使用技能</button>
              )}
            </div>
          </div>
        )
      })}
    </div>
  )
}

// ── 折叠态侧边栏 ──
function SidebarRail({ onToggle }: { onToggle: () => void }) {
  return (
    <motion.div initial={{ width: 0 }} animate={{ width: 48 }} exit={{ width: 0 }} transition={{ duration: 0.2 }}
      style={{ flex: 'none', width: 48, height: '100%', display: 'flex', flexDirection: 'column', alignItems: 'center', padding: '14px 0 10px', borderRight: '1px solid var(--line)', background: 'var(--card-bg-solid)', overflow: 'hidden' }}>
      <button type="button" onClick={onToggle} aria-label="展开侧边栏"
        style={{ display: 'grid', placeItems: 'center', width: 32, height: 32, border: 'none', borderRadius: 8, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer', transition: 'background 0.15s' }}
        onMouseEnter={e => { e.currentTarget.style.background = 'var(--code-bg)' }} onMouseLeave={e => { e.currentTarget.style.background = 'transparent' }}>
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden><rect x="2" y="3" width="12" height="10" rx="2" stroke="currentColor" strokeWidth="1.5" fill="none" /><line x1="6" y1="3" x2="6" y2="13" stroke="currentColor" strokeWidth="1.5" /></svg>
      </button>
    </motion.div>
  )
}

// ── 「自动带入文件内容」三档文案（注入开关优化5：改名 + 明确不影响工具能力）──
// 侧边栏 ModeSwitch 与首页 HeroModeBar 共用，避免两处文案漂移。
// 旧名"工作区上下文"像总闸，用户会误以为关掉后 AI 就碰不到工作区文件了。
const INJECT_MODE_LABEL: Record<WorkspaceInjectMode, string> = {
  full: '全文',
  tree: '仅目录',
  off: '关',
}
const INJECT_MODE_TITLE =
  '控制对话前是否把工作区文件内容自动带给 AI。不影响 AI 读写工作区的能力——工具在 Agent 模式下始终可用。\n'
  + '全文：文件树 + 文件内容，AI 最了解你的文件，最耗 token\n'
  + '仅目录：只给文件树，内容由 AI 按需读取，省 token（推荐）\n'
  + '关：什么都不带，AI 需自行 list_files / read_file，最省、响应最快\n'
  + '点击切换档位'
/** 关闭态副文案：打消"关了 AI 就不能动我文件"的误解 */
const INJECT_MODE_OFF_HINT = '工具仍可用，AI 会自行读取文件'

// ── 模式切换器（纯聊天 / Agent 办公）— 分段式大按钮，主界面始终可见 ──
// 两个选项一目了然：用户随时知道当前模式、并能一键切到另一个
// 首次对话后模式锁定（P1 配套）：切换另一模式时由上层弹窗提示，不在此处拦截
// Agent 模式时显示"自动带入文件内容"三档开关（P4 + 注入开关优化3：全文/仅目录/关）
function ModeSwitch({ mode, onSelect, injectMode, onCycleInjectMode }: {
  mode: 'chat' | 'agent'
  onSelect: (m: 'chat' | 'agent') => void
  injectMode?: WorkspaceInjectMode
  onCycleInjectMode?: () => void
}) {
  const seg = (active: boolean): React.CSSProperties => ({
    display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
    height: 34, padding: '0 18px', fontSize: 13.5, fontWeight: 600,
    fontFamily: 'inherit', cursor: 'pointer', border: 'none', borderRadius: 999,
    background: active ? 'var(--accent)' : 'transparent',
    color: active ? '#fff' : 'var(--text-secondary)',
    transition: 'background 0.15s, color 0.15s',
  })
  const subBtn = (active: boolean): React.CSSProperties => ({
    display: 'inline-flex', alignItems: 'center', gap: 6, padding: '2px 12px', fontSize: 11.5, lineHeight: '20px',
    border: '1px solid var(--line)', borderRadius: 999,
    background: active ? 'var(--code-bg)' : 'transparent',
    color: active ? 'var(--accent)' : 'var(--text-tertiary)',
    cursor: 'pointer', fontFamily: 'inherit', transition: 'all 0.15s',
  })
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6, padding: '0 0 10px', flex: 'none' }}>
      <div role="group" aria-label="对话模式"
        style={{ display: 'inline-flex', padding: 3, gap: 2, borderRadius: 999, border: '1px solid var(--line)', background: 'var(--card-bg-solid)', boxShadow: '0 1px 4px rgba(0,0,0,0.08)' }}>
        <button type="button" onClick={() => onSelect('chat')} style={seg(mode === 'chat')}
          title="纯聊天：AI 直接回答，不调用任何工具，响应更快">
          💬 纯聊天
        </button>
        <button type="button" onClick={() => onSelect('agent')} style={seg(mode === 'agent')}
          title="Agent 办公：AI 可读写工作区文件、执行代码、加载技能">
          🤖 Agent 办公
        </button>
      </div>
      {/* 注入开关优化3+5：三档「自动带入文件内容」— 点击循环 全文 → 仅目录 → 关 */}
      {mode === 'agent' && onCycleInjectMode && (
        <>
          <button type="button" onClick={onCycleInjectMode}
            title={INJECT_MODE_TITLE}
            aria-label={`自动带入文件内容：${INJECT_MODE_LABEL[injectMode ?? 'full']}，点击切换档位`}
            style={subBtn(injectMode !== 'off')}>
            <span style={{ opacity: injectMode === 'off' ? 0.45 : 1 }}>📁 自动带入文件内容</span>
            <span style={{ fontWeight: 600 }}>{INJECT_MODE_LABEL[injectMode ?? 'full']}</span>
          </button>
          {injectMode === 'off' && (
            <span style={{ fontSize: 11, lineHeight: '16px', color: 'var(--text-tertiary)', textAlign: 'center' }}>
              {INJECT_MODE_OFF_HINT}
            </span>
          )}
        </>
      )}
    </div>
  )
}

// ── 回到底部 ──
function ScrollToBottom({ onClick }: { onClick: () => void }) {  return (
    <div style={{ position: 'sticky', bottom: 'calc(var(--composer-height, 152px) + 16px)', height: 0, display: 'flex', justifyContent: 'flex-end', paddingRight: `max(0px, calc((100% - ${CHAT_CONTENT_WIDTH}px) / 2))`, pointerEvents: 'none', zIndex: 8 }}>
      <button type="button" onClick={onClick} aria-label="回到底部"
        style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', width: 34, height: 34, marginTop: -34, padding: 0, border: '1px solid var(--line)', borderRadius: 100, color: 'var(--text-primary)', background: 'var(--card-bg-solid)', boxShadow: '0 2px 8px rgba(0,0,0,0.08)', cursor: 'pointer', pointerEvents: 'auto', transition: 'background 0.15s' }}
        onMouseEnter={e => { e.currentTarget.style.background = 'var(--code-bg)' }} onMouseLeave={e => { e.currentTarget.style.background = 'var(--card-bg-solid)' }}>
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden><path d="M7 3v8M3 7l4 4 4-4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
      </button>
    </div>
  )
}

// ── Turn 状态 — S1 阶段感知状态机 + S6 轻量动画 ──
// 根据 SSE 事件实时显示 AI 当前阶段：等待/思考/调用工具/执行/回答，
// 长时间无输出时也有"活着"的反馈，避免用户误判卡住
const TURN_PHASE_CONFIG: Record<string, { Icon: LucideIcon; label: string }> = {
  waiting:   { Icon: Hourglass, label: '等待模型响应' },
  thinking:  { Icon: Brain,     label: '深度思考中' },
  tool_call: { Icon: Wrench,    label: '调用工具' },
  tool_exec: { Icon: Cog,       label: '工具执行中' },
  answering: { Icon: PenLine,   label: '生成回答中' },
}

function TurnStatus({ phase, toolName }: { phase: AgentPhase; toolName?: string }) {
  const [mountedAt] = useState(() => Date.now())
  const [elapsedMs, setElapsedMs] = useState(0)
  useEffect(() => { const tick = () => setElapsedMs(Math.max(0, Date.now() - mountedAt)); tick(); const id = setInterval(tick, 1000); return () => clearInterval(id) }, [mountedAt])
  const showClock = elapsedMs >= 15_000
  const seconds = Math.floor(elapsedMs / 1000)
  const cfg = TURN_PHASE_CONFIG[phase] ?? TURN_PHASE_CONFIG.waiting
  const label = cfg.label
  // 工具相关阶段拼接工具名，让用户知道具体在调用/执行哪个工具
  const text = (phase === 'tool_call' || phase === 'tool_exec') && toolName
    ? `${label} · ${toolName}`
    : label
  return (
    <div role="status" aria-live="polite" style={{
      alignSelf: 'flex-start', display: 'inline-flex', alignItems: 'center', gap: 10, height: 26, whiteSpace: 'nowrap', fontSize: 14, fontWeight: 600,
      color: 'var(--accent)',
    }}>
      {/* S6：呼吸脉冲动画（CSS opacity，非持续 GPU 渲染） */}
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8, animation: 'dsh-pulse-soft 1.6s ease-in-out infinite' }}>
        <cfg.Icon size={15} strokeWidth={1.75} aria-hidden style={{ lineHeight: 1, flex: 'none' }} />
        <span>{text}</span>
      </span>
      {showClock && <span aria-hidden style={{ marginLeft: 4, fontSize: 13, fontWeight: 400, fontVariantNumeric: 'tabular-nums', color: 'var(--text-tertiary)' }}>{seconds}s</span>}
    </div>
  )
}

// ── Hero Glow ── 静态光晕（不再持续动画，减少 GPU 占用）
function HeroGlow() {
  return (
    <div style={{ position: 'absolute', left: '50%', top: '35%', zIndex: -1, transform: 'translate(-50%, -50%)', pointerEvents: 'none' }}>
      <div style={{
        width: 300, height: 300, borderRadius: '50%',
        background: 'radial-gradient(circle, color-mix(in srgb, var(--accent) 8%, transparent) 0%, transparent 62%)',
        filter: 'blur(24px)',
        position: 'absolute', left: '50%', top: '50%', transform: 'translate(-50%, -50%)',
      }} />
      <div style={{
        width: 170, height: 170, borderRadius: '50%',
        background: 'radial-gradient(circle, color-mix(in srgb, var(--accent) 13%, transparent) 0%, transparent 55%)',
        filter: 'blur(12px)',
        position: 'absolute', left: '50%', top: '50%', transform: 'translate(-50%, -50%)',
      }} />
    </div>
  )
}

// ── 3D 立体静态 Logo ── 多层 SVG 渐变实现立体感
function Hero3DIcon() {
  return (
    <svg width="100" height="100" viewBox="0 0 100 100" fill="none"
      style={{ display: 'block', overflow: 'visible' }}
      aria-hidden>
      <defs>
        {/* 气泡主体渐变：左上亮 → 右下暗 */}
        <linearGradient id="logo-bubble" x1="15%" y1="5%" x2="85%" y2="95%">
          <stop offset="0%" stopColor="color-mix(in srgb, var(--accent) 15%, #fff)" />
          <stop offset="40%" stopColor="color-mix(in srgb, var(--accent) 35%, #fff)" />
          <stop offset="100%" stopColor="var(--accent)" />
        </linearGradient>
        {/* 气泡顶部高光 */}
        <radialGradient id="logo-shine" cx="35%" cy="20%" r="32%">
          <stop offset="0%" stopColor="rgba(255,255,255,0.65)" />
          <stop offset="100%" stopColor="rgba(255,255,255,0)" />
        </radialGradient>
        {/* 气泡底部环境反射 */}
        <radialGradient id="logo-bottom-glow" cx="50%" cy="100%" r="42%">
          <stop offset="0%" stopColor="color-mix(in srgb, var(--accent) 25%, #fff)" stopOpacity="0.4" />
          <stop offset="100%" stopColor="transparent" />
        </radialGradient>
        {/* 心形渐变：上深下浅 */}
        <linearGradient id="logo-heart" x1="50%" y1="0%" x2="50%" y2="100%">
          <stop offset="0%" stopColor="color-mix(in srgb, var(--accent) 55%, #000)" />
          <stop offset="100%" stopColor="var(--accent)" />
        </linearGradient>
        {/* 心形高光 */}
        <radialGradient id="logo-heart-shine" cx="35%" cy="28%" r="30%">
          <stop offset="0%" stopColor="rgba(255,255,255,0.5)" />
          <stop offset="100%" stopColor="rgba(255,255,255,0)" />
        </radialGradient>
        {/* 阴影层渐变（背面偏移阴影，制造厚度） */}
        <linearGradient id="logo-shadow" x1="15%" y1="5%" x2="85%" y2="95%">
          <stop offset="0%" stopColor="color-mix(in srgb, var(--accent) 50%, #000)" />
          <stop offset="100%" stopColor="color-mix(in srgb, var(--accent) 30%, #000)" />
        </linearGradient>
      </defs>

      {/* 底部模糊投影 */}
      <ellipse cx="50" cy="80" rx="28" ry="3.5" fill="rgba(0,0,0,0.05)" filter="blur(3px)" />

      {/* ── 阴影偏移层（制造 3D 厚度） ── */}
      <rect x="22" y="20" width="56" height="44" rx="14"
        fill="url(#logo-shadow)" opacity="0.25"
        transform="translate(2.5, 3)" />

      {/* ── 气泡主体 ── */}
      <rect x="22" y="20" width="56" height="44" rx="14"
        fill="url(#logo-bubble)" stroke="var(--accent)" strokeWidth="1.4" />
      {/* 顶部高光 */}
      <rect x="22" y="20" width="56" height="44" rx="14"
        fill="url(#logo-shine)" />
      {/* 底部反射 */}
      <rect x="22" y="20" width="56" height="44" rx="14"
        fill="url(#logo-bottom-glow)" />

      {/* ── 对话尖角 ── */}
      <path d="M38 63 L42 74 L48 63 Z"
        fill="url(#logo-bubble)" stroke="var(--accent)" strokeWidth="1.1" strokeLinejoin="round" />

      {/* ── 心形（知你 — 懂你） ── */}
      <path d="M50 55 C45 51 39 47.5 39 41.5 C39 38.5 41.2 36.2 44 36.2 C46 36.2 47.8 37.3 49 39.2 C50.2 37.3 52 36.2 54 36.2 C56.8 36.2 59 38.5 59 41.5 C59 47.5 53 51 50 55 Z"
        fill="url(#logo-heart)" stroke="var(--accent)" strokeWidth="0.7" strokeLinejoin="round" />
      {/* 心形高光 */}
      <path d="M50 55 C45 51 39 47.5 39 41.5 C39 38.5 41.2 36.2 44 36.2 C46 36.2 47.8 37.3 49 39.2 C50.2 37.3 52 36.2 54 36.2 C56.8 36.2 59 38.5 59 41.5 C59 47.5 53 51 50 55 Z"
        fill="url(#logo-heart-shine)" />

      {/* ── 对话点 ── */}
      <circle cx="31" cy="40" r="2.2" fill="color-mix(in srgb, var(--accent) 65%, #fff)" />
      <circle cx="69" cy="40" r="2.2" fill="color-mix(in srgb, var(--accent) 65%, #fff)" />

      {/* ── 顶部弧形高光线（立体边缘反射） ── */}
      <path d="M31 25 Q50 19 69 25" stroke="rgba(255,255,255,0.55)" strokeWidth="1.8" strokeLinecap="round" fill="none" />

      {/* ── 浮动墨点 ── */}
      <circle cx="82" cy="14" r="2" fill="var(--accent)" opacity="0.4" />
      <circle cx="82" cy="14" r="0.8" fill="var(--accent)" opacity="0.7" />
      <circle cx="16" cy="18" r="1.6" fill="var(--accent)" opacity="0.3" />
      <circle cx="14" cy="74" r="1.2" fill="var(--accent)" opacity="0.2" />
      <circle cx="88" cy="76" r="1" fill="var(--accent)" opacity="0.18" />
    </svg>
  )
}

// ── 侧边栏 mini logo ── Hero3DIcon 的缩小版，静态 3D 立体对话气泡
function SidebarLogo() {
  return (
    <svg width="36" height="36" viewBox="0 0 100 100" fill="none"
      style={{ flexShrink: 0, display: 'block', overflow: 'visible' }}
      aria-hidden>
      <defs>
        <linearGradient id="side-logo-bubble" x1="15%" y1="5%" x2="85%" y2="95%">
          <stop offset="0%" stopColor="color-mix(in srgb, var(--accent) 15%, #fff)" />
          <stop offset="40%" stopColor="color-mix(in srgb, var(--accent) 35%, #fff)" />
          <stop offset="100%" stopColor="var(--accent)" />
        </linearGradient>
        <radialGradient id="side-logo-shine" cx="35%" cy="20%" r="32%">
          <stop offset="0%" stopColor="rgba(255,255,255,0.65)" />
          <stop offset="100%" stopColor="rgba(255,255,255,0)" />
        </radialGradient>
        <radialGradient id="side-logo-bottom" cx="50%" cy="100%" r="42%">
          <stop offset="0%" stopColor="color-mix(in srgb, var(--accent) 25%, #fff)" stopOpacity="0.4" />
          <stop offset="100%" stopColor="transparent" />
        </radialGradient>
        <linearGradient id="side-logo-heart" x1="50%" y1="0%" x2="50%" y2="100%">
          <stop offset="0%" stopColor="color-mix(in srgb, var(--accent) 55%, #000)" />
          <stop offset="100%" stopColor="var(--accent)" />
        </linearGradient>
        <radialGradient id="side-logo-heart-shine" cx="35%" cy="28%" r="30%">
          <stop offset="0%" stopColor="rgba(255,255,255,0.5)" />
          <stop offset="100%" stopColor="rgba(255,255,255,0)" />
        </radialGradient>
        <linearGradient id="side-logo-shadow" x1="15%" y1="5%" x2="85%" y2="95%">
          <stop offset="0%" stopColor="color-mix(in srgb, var(--accent) 50%, #000)" />
          <stop offset="100%" stopColor="color-mix(in srgb, var(--accent) 30%, #000)" />
        </linearGradient>
      </defs>

      {/* 底部模糊投影 */}
      <ellipse cx="50" cy="80" rx="28" ry="3.5" fill="rgba(0,0,0,0.05)" filter="blur(3px)" />

      {/* 阴影偏移层 */}
      <rect x="22" y="20" width="56" height="44" rx="14"
        fill="url(#side-logo-shadow)" opacity="0.25"
        transform="translate(2.5, 3)" />

      {/* 气泡主体 */}
      <rect x="22" y="20" width="56" height="44" rx="14"
        fill="url(#side-logo-bubble)" stroke="var(--accent)" strokeWidth="1.4" />
      <rect x="22" y="20" width="56" height="44" rx="14"
        fill="url(#side-logo-shine)" />
      <rect x="22" y="20" width="56" height="44" rx="14"
        fill="url(#side-logo-bottom)" />

      {/* 对话尖角 */}
      <path d="M38 63 L42 74 L48 63 Z"
        fill="url(#side-logo-bubble)" stroke="var(--accent)" strokeWidth="1.1" strokeLinejoin="round" />

      {/* 心形 */}
      <path d="M50 55 C45 51 39 47.5 39 41.5 C39 38.5 41.2 36.2 44 36.2 C46 36.2 47.8 37.3 49 39.2 C50.2 37.3 52 36.2 54 36.2 C56.8 36.2 59 38.5 59 41.5 C59 47.5 53 51 50 55 Z"
        fill="url(#side-logo-heart)" stroke="var(--accent)" strokeWidth="0.7" strokeLinejoin="round" />
      <path d="M50 55 C45 51 39 47.5 39 41.5 C39 38.5 41.2 36.2 44 36.2 C46 36.2 47.8 37.3 49 39.2 C50.2 37.3 52 36.2 54 36.2 C56.8 36.2 59 38.5 59 41.5 C59 47.5 53 51 50 55 Z"
        fill="url(#side-logo-heart-shine)" />

      {/* 对话点 */}
      <circle cx="31" cy="40" r="2.2" fill="color-mix(in srgb, var(--accent) 65%, #fff)" />
      <circle cx="69" cy="40" r="2.2" fill="color-mix(in srgb, var(--accent) 65%, #fff)" />

      {/* 顶部弧形高光 */}
      <path d="M31 25 Q50 19 69 25" stroke="rgba(255,255,255,0.55)" strokeWidth="1.8" strokeLinecap="round" fill="none" />

      {/* 浮动墨点 */}
      <circle cx="82" cy="14" r="2" fill="var(--accent)" opacity="0.4" />
      <circle cx="82" cy="14" r="0.8" fill="var(--accent)" opacity="0.7" />
      <circle cx="16" cy="18" r="1.6" fill="var(--accent)" opacity="0.3" />
      <circle cx="14" cy="74" r="1.2" fill="var(--accent)" opacity="0.2" />
      <circle cx="88" cy="76" r="1" fill="var(--accent)" opacity="0.18" />
    </svg>
  )
}

// ── Markdown 渲染 ──
// memo 化：只有 content 变化时才重新渲染（流式追加时仅在 rAF 刷新后触发）

/** Markdown → 纯文本（粗略剥离语法，复制用） */
function markdownToPlainText(md: string): string {
  return md
    // 代码块：去掉 ```lang 围栏，保留内容
    .replace(/```[a-zA-Z0-9_-]*\n?([\s\S]*?)```/g, '$1')
    // 行内代码 / 加粗 / 斜体 / 删除线 / 下划线
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*\*([^*]+)\*\*/g, '$1')
    .replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, '$1$2')
    .replace(/__([^_]+)__/g, '$1')
    .replace(/~~([^~]+)~~/g, '$1')
    // 图片 / 链接 → 显示文本
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    // 标题、引用、列表符号、表格分隔线
    .replace(/^#{1,6}\s+/gm, '')
    .replace(/^\s*>\s?/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\d+\.\s+/gm, '')
    .replace(/^\s*\|?[\s:|-]+\|?\s*$/gm, '')
    .trim()
}

// ── 代码块组件：语法高亮 + 语言徽标 + 复制按钮 ──
// 流式期间（streaming=true）不高亮：每次 token 全量高亮浪费 CPU，等流结束再高亮
// 短内容（单行 ≤40 字符，如 .py/.js 这类后缀/片段）渲染成紧凑行内标签，
// 不套"徽标+复制+大背景"的重型卡片——避免截图那种"一个后缀一张大卡片"的丑态
// ── 语言徽章配色（A：彩色药丸徽标，替代原灰字模糊标签）──
const LANG_COLORS: Record<string, { bg: string; fg: string }> = {
  python:   { bg: '#3776AB', fg: '#fff' },
  py:       { bg: '#3776AB', fg: '#fff' },
  javascript: { bg: '#D97706', fg: '#fff' },
  js:       { bg: '#D97706', fg: '#fff' },
  typescript: { bg: '#3178C6', fg: '#fff' },
  ts:       { bg: '#3178C6', fg: '#fff' },
  json:     { bg: '#15803D', fg: '#fff' },
  yaml:     { bg: '#0369A1', fg: '#fff' },
  yml:      { bg: '#0369A1', fg: '#fff' },
  xml:      { bg: '#B45309', fg: '#fff' },
  html:     { bg: '#B45309', fg: '#fff' },
  css:      { bg: '#7C3AED', fg: '#fff' },
  bash:     { bg: '#334155', fg: '#fff' },
  shell:    { bg: '#334155', fg: '#fff' },
  sh:       { bg: '#334155', fg: '#fff' },
  sql:      { bg: '#0E7490', fg: '#fff' },
  markdown: { bg: '#57534E', fg: '#fff' },
  md:       { bg: '#57534E', fg: '#fff' },
  java:     { bg: '#B45309', fg: '#fff' },
  cpp:      { bg: '#00599C', fg: '#fff' },
  'c++':    { bg: '#00599C', fg: '#fff' },
  c:        { bg: '#555555', fg: '#fff' },
  go:       { bg: '#00ADD8', fg: '#0b3d4d' },
  golang:   { bg: '#00ADD8', fg: '#0b3d4d' },
  rust:     { bg: '#B7410E', fg: '#fff' },
  rs:       { bg: '#B7410E', fg: '#fff' },
}
const LANG_DEFAULT = { bg: '#986638', fg: '#fff' }

const CodeBlock = memo(function CodeBlock({ className, code, streaming }: { className?: string; code: string; streaming?: boolean }) {
  const [copied, setCopied] = useState(false)
  const lang = (className?.match(/language-([\w+-]+)/)?.[1] ?? '').toLowerCase()
  const isShort = !streaming && code && !code.includes('\n') && code.trim().length <= 40
  const onCopy = useCallback(() => {
    void navigator.clipboard.writeText(code).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    })
  }, [code])
  const highlighted = useMemo(() => {
    if (streaming || isShort || !code) return null
    try {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(code, { language: lang }).value
      }
      return hljs.highlightAuto(code).value
    } catch { return null }
  }, [code, lang, streaming, isShort])

  // ── 短内容：紧凑行内标签，无徽标/复制/外框 ──
  if (isShort) {
    return (
      <div style={{ margin: '6px 0' }}>
        <code style={{
          display: 'inline-block', background: 'var(--code-bg)', color: 'var(--text-secondary)',
          padding: '2px 8px', borderRadius: 6, fontFamily: 'var(--font-mono, monospace)', fontSize: 13.5, lineHeight: '20px',
        }}>{code}</code>
      </div>
    )
  }

  // ── 长内容：卡片结构（B：macOS 三色点标题栏 + A：彩色语言徽章 + F：阴影层次）──
  const badge = lang ? (LANG_COLORS[lang] ?? LANG_DEFAULT) : null
  return (
    <div style={{
      margin: '12px 0',
      border: '1px solid color-mix(in srgb, var(--line) 80%, transparent)',
      borderRadius: 12,
      overflow: 'hidden',
      background: 'var(--card-bg-solid)',
      boxShadow: '0 2px 10px rgba(0,0,0,0.05), 0 1px 2px rgba(0,0,0,0.03)',
    }}>
      {/* 标题栏 — macOS 三色点 + 语言徽章 + 复制 */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '8px 12px',
        borderBottom: '1px solid color-mix(in srgb, var(--line) 70%, transparent)',
        background: 'color-mix(in srgb, var(--code-bg) 60%, transparent)',
      }}>
        {/* 三色点 */}
        <span style={{ display: 'inline-flex', alignItems: 'center', gap: 5, flex: 'none' }}>
          <span style={{ width: 10, height: 10, borderRadius: '50%', background: '#FF5F57' }} />
          <span style={{ width: 10, height: 10, borderRadius: '50%', background: '#FEBC2E' }} />
          <span style={{ width: 10, height: 10, borderRadius: '50%', background: '#28C840' }} />
        </span>
        {/* 语言徽章 */}
        {badge && (
          <span style={{
            display: 'inline-flex', alignItems: 'center',
            padding: '1px 9px', borderRadius: 999,
            background: badge.bg, color: badge.fg,
            fontSize: 11, fontWeight: 600, letterSpacing: '0.04em',
            fontFamily: 'var(--font-mono, monospace)',
            lineHeight: '17px',
          }}>{lang.toUpperCase()}</span>
        )}
        {/* 复制按钮 */}
        <button type="button" onClick={onCopy} aria-label="复制代码"
          style={{
            marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 5,
            padding: '3px 10px', border: '1px solid var(--line)', borderRadius: 7,
            background: 'var(--card-bg-solid)', color: copied ? '#16a34a' : 'var(--text-tertiary)',
            fontSize: 11.5, cursor: 'pointer', fontFamily: 'inherit',
            transition: 'color 0.15s, border-color 0.15s, background 0.15s',
            opacity: 1,
          }}
          onMouseEnter={e => {
            e.currentTarget.style.borderColor = 'var(--accent)'
            e.currentTarget.style.color = copied ? '#16a34a' : 'var(--accent)'
          }}
          onMouseLeave={e => {
            e.currentTarget.style.borderColor = 'var(--line)'
            e.currentTarget.style.color = copied ? '#16a34a' : 'var(--text-tertiary)'
          }}>
          {copied ? '✓ 已复制' : (
            <>
              <svg width="11" height="11" viewBox="0 0 16 16" fill="none" aria-hidden>
                <rect x="5" y="3" width="8" height="10" rx="2" stroke="currentColor" strokeWidth="1.5" fill="none" />
                <path d="M3 5.5v7a2 2 0 002 2h5" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" />
              </svg>
              复制
            </>
          )}
        </button>
      </div>
      <pre style={{
        margin: 0, padding: '12px 16px', overflow: 'auto',
        fontSize: 13.5, fontFamily: 'var(--font-mono, monospace)', lineHeight: 1.6,
        background: 'transparent', color: 'var(--text-primary)',
      }}>
        {highlighted
          ? <code dangerouslySetInnerHTML={{ __html: highlighted }} />
          : <code>{code}</code>}
      </pre>
    </div>
  )
})

// ── Markdown 渲染 ──
// 排版对齐主流 AI 网页端：正文 15.5px/26px 紧凑节奏；标题分级、分隔线、任务列表、图片适配
const MarkdownRenderer = memo(function MarkdownRenderer({ content, streaming }: { content: string; streaming?: boolean }) {
  // 流式期间先补全尾部未闭合语法（``` / ** / 行内代码 / 链接），避免原始符号闪烁
  const text = useMemo(
    () => (streaming ? completeStreamingMarkdown(content) : content),
    [content, streaming],
  )
  return (
    <div className="dsh-chat-md" style={{ fontSize: 15.5, lineHeight: '26px', color: 'var(--text-primary)' }}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
        // react-markdown v9+ 移除了 code 组件的 inline prop（永远为 undefined），
        // 必须改用 mdast 节点类型（node.type）区分行内/块级代码：
        // - 行内代码节点 type === 'inlineCode'
        // - 围栏代码块节点 type === 'code'（带 language-* className；裸 fence 无 className）
        code: ({ children, className, node, ...props }: any) => {
          const isBlock = node?.type === 'code' || (!!className && String(className).startsWith('language-'))
          return isBlock
            ? <CodeBlock className={className} code={String(children ?? '').replace(/\n$/, '')} streaming={streaming} />
            : <code {...props} style={{ background: 'var(--code-bg)', color: 'var(--text-primary)', padding: '2px 6px', borderRadius: 4, fontSize: '14.5px', fontFamily: 'var(--font-mono, monospace)' }}>{children}</code>
        },
        p: ({ children }) => <p style={{ margin: '0 0 8px' }}>{children}</p>,
        h1: ({ children }) => <h1 style={{ margin: '18px 0 10px', paddingBottom: 6, borderBottom: '1px solid var(--line)', fontSize: 22, lineHeight: '30px', fontWeight: 700, color: 'var(--text-primary)' }}>{children}</h1>,
        h2: ({ children }) => <h2 style={{ margin: '16px 0 8px', paddingBottom: 5, borderBottom: '1px solid var(--line)', fontSize: 19, lineHeight: '28px', fontWeight: 700, color: 'var(--text-primary)' }}>{children}</h2>,
        h3: ({ children }) => <h3 style={{ margin: '14px 0 8px', fontSize: 17, lineHeight: '26px', fontWeight: 600, color: 'var(--text-primary)' }}>{children}</h3>,
        h4: ({ children }) => <h4 style={{ margin: '10px 0 6px', fontSize: 15.5, lineHeight: '24px', fontWeight: 600, color: 'var(--text-primary)' }}>{children}</h4>,
        h5: ({ children }) => <h5 style={{ margin: '10px 0 4px', fontSize: 14.5, lineHeight: '22px', fontWeight: 600, color: 'var(--text-secondary)' }}>{children}</h5>,
        h6: ({ children }) => <h6 style={{ margin: '10px 0 4px', fontSize: 13.5, lineHeight: '20px', fontWeight: 600, color: 'var(--text-secondary)' }}>{children}</h6>,
        hr: () => <hr style={{ border: 'none', borderTop: '1px solid var(--line)', margin: '14px 0' }} />,
        ul: ({ children }) => <ul style={{ margin: '4px 0 8px', paddingLeft: 24 }}>{children}</ul>,
        ol: ({ children }) => <ol style={{ margin: '4px 0 8px', paddingLeft: 24 }}>{children}</ol>,
        li: ({ children }) => <li style={{ marginBottom: 3 }}>{children}</li>,
        input: (props: any) => <input {...props} style={{ accentColor: 'var(--accent)', width: 14, height: 14, verticalAlign: '-2px', marginRight: 6 }} />,
        strong: ({ children }) => <strong style={{ fontWeight: 600, color: 'var(--text-primary)' }}>{children}</strong>,
        blockquote: ({ children }) => <blockquote style={{ borderLeft: '3px solid var(--accent)', padding: '6px 14px', margin: '10px 0', background: 'var(--code-bg)', borderRadius: '0 6px 6px 0', color: 'var(--text-secondary)' }}>{children}</blockquote>,
        table: ({ children }) => (
          <div style={{ overflowX: 'auto', margin: '10px 0' }}>
            <table style={{ borderCollapse: 'collapse', minWidth: '100%', fontSize: 14.5 }}>{children}</table>
          </div>
        ),
        th: ({ children }) => <th style={{ padding: '8px 12px', textAlign: 'left', fontWeight: 600 }}>{children}</th>,
        td: ({ children }) => <td style={{ padding: '8px 12px' }}>{children}</td>,
        a: ({ children, href }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
        img: ({ src, alt }) => <img src={src} alt={alt ?? ''} style={{ maxWidth: '100%', borderRadius: 8, margin: '6px 0' }} />,
      }}>{text}</ReactMarkdown>
    </div>
  )
})

// ── 压缩摘要折叠块（checkpoint 消息可展开查看）──
const CompressedSummaryBlock = memo(function CompressedSummaryBlock({ summary }: { summary: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div style={{ width: '100%', maxWidth: 'min(525px, 82%)', border: '1px dashed var(--line)', borderRadius: 10, background: 'var(--code-bg)', overflow: 'hidden' }}>
      <button type="button" onClick={() => setOpen(!open)}
        style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', padding: '8px 12px', border: 'none', background: 'transparent', cursor: 'pointer', fontFamily: 'inherit', color: 'var(--text-tertiary)', fontSize: 12 }}>
        <span style={{ fontSize: 13 }}>📋</span>
        <span style={{ fontWeight: 500 }}>{open ? '压缩摘要' : '上下文已压缩 — 查看摘要'}</span>
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none" style={{ marginLeft: 'auto', transition: 'transform 0.2s', transform: open ? 'rotate(180deg)' : 'none' }} aria-hidden>
          <path d="M2.5 3.5L5 6L7.5 3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none" />
        </svg>
      </button>
      {open && (
        <pre style={{ margin: 0, padding: '0 12px 10px', fontSize: 12, lineHeight: 1.6, fontFamily: 'var(--font-mono, monospace)', color: 'var(--text-tertiary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: 240, overflowY: 'auto' }}>{summary}</pre>
      )}
    </div>
  )
})

// ── 用户消息 ──
// memo 化：用户消息内容不变时不需要重渲染
// ── 用户消息中的文件附件卡片 ──
const AttachmentCard = memo(function AttachmentCard({ file }: { file: ChatAttachment }) {
  // 优化9：dataUrl 刻意不持久化，刷新后靠工作区存档 ref 取回缩略图。
  // ref 悬空（存档失败、配额超限、被 6h TTL 回收）时静默回落到下面的文件图标 —— 
  // 缩略图是锦上添花，不该报错也不该重试。
  const { src, containerRef } = useImageRef(file.ref)
  const thumb = file.dataUrl ?? src
  return (
    <div ref={containerRef} style={{
      display: 'flex', alignItems: 'center', gap: 12,
      padding: '12px 16px 12px 14px', borderRadius: 12,
      background: 'var(--card-bg-solid)',
      border: '1px solid var(--line)',
      width: '100%', minWidth: 0,
      boxShadow: SHADOW_SM,
    }}>
      {/* 文件图标 / 图片缩略图（平面） */}
      {thumb ? (
        <div style={{ width: 40, height: 40, flexShrink: 0, borderRadius: 8, overflow: 'hidden', border: '1px solid var(--line)' }}>
          <img src={thumb} alt={file.name} style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
        </div>
      ) : (
        <span style={{
          display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          width: 40, height: 40, flexShrink: 0, borderRadius: 8,
          background: 'var(--code-bg)', color: 'var(--accent)',
        }}>
          <FileText size={20} strokeWidth={1.5} aria-hidden />
        </span>
      )}
      {/* 文件名+大小 */}
      <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0, flex: 1 }}>
        <span style={{ fontSize: 14, fontWeight: 500, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{file.name}</span>
        <span style={{ fontSize: 12, color: 'var(--text-tertiary)' }}>{formatFileSize(file.size)}</span>
      </div>
    </div>
  )
})

const UserMessage = memo(function UserMessage({ msg, index }: { msg: ChatMessage; index: number }) {
  const hasAttachments = msg.attachments && msg.attachments.length > 0
  const [copied, setCopied] = useState(false)
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const onCopy = useCallback(() => {
    if (copied || !msg.content) return
    void navigator.clipboard.writeText(msg.content).then(() => { setCopied(true); copyTimer.current = setTimeout(() => setCopied(false), 1500) })
  }, [copied, msg.content])
  useEffect(() => () => { if (copyTimer.current) clearTimeout(copyTimer.current) }, [])
  return (
    <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25, delay: Math.min(index * 0.02, 0.1) }}
      style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6 }}>
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 8, minWidth: 0, width: '100%', maxWidth: hasAttachments ? 'min(280px, 42%)' : 'min(525px, 82%)' }}>
        {/* 文本气泡（只有有内容时才显示） */}
        {msg.content && (
          <div className="user-bubble" style={{ maxWidth: '100%', background: 'var(--accent)', borderRadius: 22, padding: '12px 18px', fontSize: 15.5, lineHeight: '26px', color: '#fff', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>{msg.content}</div>
        )}
        {/* 压缩摘要折叠块（上下文压缩的 checkpoint 消息） */}
        {msg.compressedSummary && <CompressedSummaryBlock summary={msg.compressedSummary} />}
        {/* 文件附件卡片 */}
        {hasAttachments && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, width: '100%' }}>
            {msg.attachments!.map((f, i) => (
              <AttachmentCard key={i} file={f} />
            ))}
          </div>
        )}
      </div>
      {/* 复制按钮（平面） */}
      {msg.content && (
        <div style={{ marginTop: 2 }}>
          <button type="button" onClick={onCopy} aria-label={copied ? '已复制' : '复制'}
            style={{
              display: 'grid', placeItems: 'center', width: 30, height: 30, border: '1px solid var(--line)', borderRadius: 8,
              background: copied ? 'rgba(34, 197, 94, 0.10)' : 'var(--card-bg-solid)',
              color: copied ? '#22c55e' : 'var(--text-tertiary)', cursor: 'pointer',
              transition: 'background 0.15s, color 0.15s, border-color 0.15s',
            }}
            onMouseEnter={e => { if (!copied) { e.currentTarget.style.background = 'var(--surface-bg)'; e.currentTarget.style.color = 'var(--text-secondary)' } }}
            onMouseLeave={e => { e.currentTarget.style.background = copied ? 'rgba(34, 197, 94, 0.10)' : 'var(--card-bg-solid)'; e.currentTarget.style.color = copied ? '#22c55e' : 'var(--text-tertiary)' }}>
            {copied ? (
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
                <path d="M3.5 8.5l3 3 6-6.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            ) : (
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden>
                <rect x="5" y="3" width="8" height="10" rx="2" stroke="currentColor" strokeWidth="1.5" fill="none" />
                <path d="M3 5.5v7a2 2 0 002 2h5" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" />
              </svg>
            )}
          </button>
        </div>
      )}
    </motion.div>
  )
})

// ── AI 消息 ──
// memo 化：只有 msg.content 变化时才重新渲染
// ══ Agent 工具调用卡片（借鉴 DSH ToolRow 设计）══
// DSH 设计要素：
// 1. 单行折叠行：[图标] [工具名] [分隔点] [摘要] — 紧凑可扫描
// 2. 运行中扫光动画：半透明光带从左到右扫过行
// 3. 状态点：错误红色，运行中靠扫光传达
// 4. IN/OUT 卡片：展开后显示带标签的输入/输出卡片
// 5. 分隔点：标题和摘要之间用 2x2 小圆点分隔

const TOOL_LABELS: Record<string, { Icon: LucideIcon; label: string; variant: 'read' | 'write' | 'list' | 'bash' | 'code' | 'others' }> = {
  read_file:        { Icon: FileText,   label: '读取文件',   variant: 'read'  },
  write_file:       { Icon: Pencil,     label: '写入文件',   variant: 'write' },
  edit_file:        { Icon: Pencil,     label: '编辑文件',   variant: 'write' },
  revert_file:      { Icon: Undo2,      label: '回退版本',   variant: 'write' },
  rename_file:      { Icon: PenLine,    label: '重命名',     variant: 'write' },
  list_files:       { Icon: FolderOpen, label: '列出文件',   variant: 'list' },
  delete_file:      { Icon: Trash2,     label: '删除文件',   variant: 'write' },
  run_python:       { Icon: Zap,        label: '执行 Python', variant: 'bash' },
  skill:            { Icon: Puzzle,     label: '加载技能',    variant: 'others' },
  read_skill_file:  { Icon: BookOpen,   label: '读取技能文件', variant: 'read'  },
}

/** 从工具参数中提取摘要文本（展示在折叠行上） */
function getToolSummary(tool: string, args?: Record<string, unknown>): string {
  if (!args) return ''
  const keys = Object.keys(args)
  if (keys.length === 0) return ''
  // 优先展示 path / file_path / name / skill_name / code 等关键字段
  const priorityKeys = ['path', 'file_path', 'name', 'skill_name', 'filename', 'command', 'code']
  for (const key of priorityKeys) {
    if (args[key] !== undefined) {
      const val = String(args[key])
      // 对于 code 字段，只取第一行
      if (key === 'code') return val.split('\n')[0].slice(0, 80)
      return val
    }
  }
  // 没有优先字段，展示 JSON 摘要
  const json = JSON.stringify(args)
  return json.length > 80 ? json.slice(0, 80) + '…' : json
}

/** 工具行状态 */
type ToolRowState = 'running' | 'ok' | 'error'

/** #1 工具卡↔文件跳转：从文件类工具参数里取出工作区路径（rename 成功后跳新路径） */
function getToolFilePath(tool: string, args?: Record<string, unknown>): string | null {
  if (!args) return null
  const str = (v: unknown) => (typeof v === 'string' && v.trim() ? v.trim() : null)
  if (tool === 'rename_file') return str(args.new_path)
  if (tool === 'write_file' || tool === 'edit_file' || tool === 'read_file' || tool === 'revert_file') return str(args.path)
  return null
}

/** 判断结果是否为错误（借鉴 DSH 的 terminalFailed 模式） */
function isErrorResult(result?: string): boolean {
  if (result === undefined) return false
  const lower = result.toLowerCase()
  // 后端错误统一为中文前缀："错误：..."、"工具执行出错: ..."、"运行时错误: ..."、"语法错误: ..."、"执行出错: ..."
  return lower.startsWith('error') || lower.startsWith('traceback')
    || lower.startsWith('错误') || lower.startsWith('工具执行出错')
    || lower.startsWith('运行时错误') || lower.startsWith('语法错误')
    || lower.startsWith('执行出错')
    || lower.includes('iserror: true') || lower.includes('执行失败')
}

const ToolCallCard = memo(function ToolCallCard({ tool }: { tool: ToolInvocation }) {
  const [expanded, setExpanded] = useState(false)
  // 超长结果默认头尾折叠，点击展开全文
  const [resultFull, setResultFull] = useState(false)
  const meta = TOOL_LABELS[tool.tool] ?? { Icon: Wrench, label: tool.tool, variant: 'others' as const }
  // P1-5: 大参数（write_file 的 content 可达几十 KB）只在 arguments 变化时序列化一次，
  // 避免 progress 流式更新时每帧重复 JSON.stringify 造成卡顿
  const argsStr = useMemo(
    () => (tool.arguments ? JSON.stringify(tool.arguments, null, 2) : ''),
    [tool.arguments],
  )
  const hasResult = tool.result !== undefined
  const isRunning = !hasResult
  const isError = isErrorResult(tool.result)
  const state: ToolRowState = isRunning ? 'running' : isError ? 'error' : 'ok'
  const summary = getToolSummary(tool.tool, tool.arguments)
  // #1 工具卡→文件：该工具操作的工作区文件路径（执行成功后可跳转）
  const filePath = getToolFilePath(tool.tool, tool.arguments)
  // 错误行：折叠摘要显示错误首行
  const failureLine = isError && tool.result ? tool.result.split('\n')[0].slice(0, 120) : null
  const summaryText = failureLine ?? summary
  // 运行中且有实时输出：摘要显示最后一行（可读性）
  const progressText = tool.progress
  const liveLine = isRunning && progressText && progressText.trim() ? progressText.trim().split('\n').pop() : null
  const displaySummary = liveLine ?? summaryText
  const expandable = argsStr !== '' || (hasResult && tool.result) || (isRunning && !!progressText)

  // ── diff 渲染：工具结果含 <diff> 块时转为彩色差异视图 ──
  const renderResult = (resultText: string) => {
    const m = resultText.match(/<diff>([\s\S]*?)<\/diff>/)
    if (!m) {
      const preStyle = { margin: 0, whiteSpace: 'pre-wrap' as const, wordBreak: 'break-word' as const, fontSize: 12, fontFamily: 'var(--font-mono, monospace)', color: isError ? '#ef4444' : 'var(--text-secondary)' }
      // 超长结果头尾折叠（后端已截断到 8000 字符，这里再按行数收拢视觉）
      const lines = resultText.split('\n')
      if (lines.length > 40 && !resultFull) {
        const head = lines.slice(0, 20)
        const tail = lines.slice(-8)
        const omitted = lines.length - head.length - tail.length
        return (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            <pre style={preStyle}>{head.join('\n')}</pre>
            <button type="button" onClick={() => setResultFull(true)}
              style={{ alignSelf: 'center', padding: '1px 12px', border: '1px dashed var(--line)', borderRadius: 6, background: 'transparent', color: 'var(--text-tertiary)', fontSize: 11, cursor: 'pointer', fontFamily: 'inherit' }}>
              ⋯ 已省略 {omitted} 行，点击展开 ⋯
            </button>
            <pre style={preStyle}>{tail.join('\n')}</pre>
          </div>
        )
      }
      return <pre style={preStyle}>{resultText}</pre>
    }
    const diffBody = m[1]
    const before = resultText.slice(0, m.index).trim()
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {before && <pre style={{ margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontSize: 12, fontFamily: 'var(--font-mono, monospace)', color: 'var(--text-secondary)' }}>{before}</pre>}
        <div style={{ border: '1px solid var(--line)', borderRadius: 8, overflow: 'hidden', fontSize: 11.5, fontFamily: 'var(--font-mono, monospace)', lineHeight: 1.55 }}>
          {diffBody.split('\n').map((line, i) => {
            let bg = 'transparent', color = 'var(--text-secondary)'
            if (line.startsWith('+')) { bg = 'rgba(34,197,94,0.08)'; color = '#22c55e' }
            else if (line.startsWith('-')) { bg = 'rgba(239,68,68,0.08)'; color = '#ef4444' }
            else if (line.startsWith('@@')) { bg = 'rgba(139,92,246,0.08)'; color = '#a78bfa' }
            return <div key={i} style={{ padding: '0 10px', background: bg, color, whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{line || ' '}</div>
          })}
        </div>
      </div>
    )
  }

  return (
    <div
      className="dsh-tool-row-root"
      data-state={state}
      data-variant={meta.variant}
      style={{
        position: 'relative',
        display: 'flex',
        flexDirection: 'column',
        borderRadius: 6,
        overflow: 'hidden',
        fontSize: 14,
        lineHeight: '24px',
        background: 'var(--code-bg, rgba(0,0,0,0.02))',
      }}
    >
      {/* 运行中扫光动画层（借鉴 DSH dsh-tool-row-sweep）*/}
      {state === 'running' && (
        <span
          aria-hidden
          style={{
            position: 'absolute',
            top: 0, bottom: 0, left: 0,
            width: 300,
            zIndex: 1,
            pointerEvents: 'none',
            background: 'linear-gradient(90deg, transparent 0%, var(--code-bg, rgba(0,0,0,0.06)) 55%, transparent 100%)',
            animation: 'dsh-tool-sweep 2.6s ease-out infinite',
          }}
        />
      )}
      <button
        type="button"
        onClick={() => expandable && setExpanded(v => !v)}
        disabled={!expandable}
        style={{
          position: 'relative',
          zIndex: 2,
          display: 'flex', alignItems: 'center', gap: 8, width: '100%',
          padding: '4px 12px', border: 'none', background: 'transparent',
          cursor: expandable ? 'pointer' : 'default',
          color: 'var(--text-secondary)', textAlign: 'left',
        }}
      >
        {/* 前导图标/状态点 */}
        {state === 'error' ? (
          <span style={{
            flexShrink: 0, width: 8, height: 8, borderRadius: '50%',
            background: '#ef4444', boxShadow: '0 0 0 3px rgba(239,68,68,0.12)',
          }} />
        ) : state === 'running' ? (
          <span style={{
            flexShrink: 0, width: 14, height: 14, borderRadius: '50%',
            border: '1.5px solid var(--text-tertiary)',
            borderTopColor: 'var(--accent)',
            animation: 'dsh-spin 0.6s linear infinite',
          }} />
        ) : (
          <span style={{ display: 'inline-flex', flexShrink: 0, lineHeight: 1 }}><meta.Icon size={14} strokeWidth={1.75} /></span>
        )}
        {/* 工具名 */}
        <span style={{ fontWeight: 400, flexShrink: 0 }}>{meta.label}</span>
        {/* B2：轮次徽标（有上限显示"第 N/M 轮"，无上限显示"第 N 轮"——历史数据无上限字段） */}
        {tool.round != null && (
          <span style={{
            flexShrink: 0, fontSize: 10.5, lineHeight: 1,
            padding: '2px 6px', borderRadius: 999,
            background: 'rgba(152,102,56,0.1)', color: 'var(--accent)',
            border: '1px solid rgba(152,102,56,0.25)',
          }}>
            第 {tool.round}{tool.maxRounds != null ? `/${tool.maxRounds}` : ''} 轮
          </span>
        )}
        {/* 分隔点（借鉴 DSH .sep）*/}
        {summaryText && (
          <span aria-hidden style={{
            flex: 'none', width: 2, height: 2, borderRadius: 1,
            margin: '0 4px', background: 'var(--text-tertiary)', opacity: 0.5,
          }} />
        )}
        {/* 摘要文本 */}
        {displaySummary && (
          <span style={{
            flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis',
            whiteSpace: 'nowrap', fontSize: 13, color: isError ? '#ef4444' : 'var(--text-tertiary)',
            fontFamily: 'var(--font-mono, monospace)',
          }}>
            {displaySummary}
          </span>
        )}
        {/* 状态标签 */}
        {hasResult && !isError && (
          <span style={{ color: '#22c55e', fontSize: 12, flexShrink: 0, display: 'flex', alignItems: 'center', gap: 3 }}>
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden>
              <path d="M3.5 8.5l3 3 6-6.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none" />
            </svg>
          </span>
        )}
        {/* #1 工具卡→文件：在工作区打开/定位此文件（成功后才有意义） */}
        {filePath && state === 'ok' && (
          <span
            role="button"
            aria-label="在工作区打开此文件"
            title="在工作区打开此文件"
            onClick={(e) => {
              e.stopPropagation()
              window.dispatchEvent(new CustomEvent(WORKSPACE_LOCATE_EVENT, { detail: { path: filePath } }))
            }}
            style={{
              flexShrink: 0, display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
              width: 22, height: 22, marginRight: -2, borderRadius: 5, cursor: 'pointer',
              color: 'var(--text-tertiary)', transition: 'color 0.15s, background 0.15s',
            }}
            onMouseEnter={e => { e.currentTarget.style.color = 'var(--accent)'; e.currentTarget.style.background = 'var(--code-bg)' }}
            onMouseLeave={e => { e.currentTarget.style.color = 'var(--text-tertiary)'; e.currentTarget.style.background = 'transparent' }}
          >
            <FolderOpen size={13} strokeWidth={1.75} aria-hidden />
          </span>
        )}
        {/* 展开箭头 */}
        {expandable && (
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" aria-hidden
            style={{ flexShrink: 0, transition: 'transform 0.2s', transform: expanded ? 'rotate(90deg)' : 'none', color: 'var(--text-tertiary)' }}>
            <path d="M6 4l4 4-4 4" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none" />
          </svg>
        )}
      </button>
      {/* 展开内容：IN/OUT 卡片（借鉴 DSH ioCard 设计）*/}
      {expanded && expandable && (
        <div style={{
          display: 'flex', flexDirection: 'column',
          margin: '4px 0 6px 22px',
        }}>
          {/* IN: 参数 */}
          {argsStr && (
            <div style={{
              display: 'grid', gridTemplateColumns: 'max-content 1fr',
              columnGap: 14, alignItems: 'baseline',
              padding: '10px 14px', maxHeight: 150, overflowY: 'auto',
              border: '1px solid var(--line)', borderRadius: '8px',
              background: 'var(--bg-primary, #fff)',
            }}>
              <span style={{
                position: 'sticky', top: 0, alignSelf: 'start',
                color: 'var(--text-tertiary)', fontSize: 11, fontWeight: 600,
                letterSpacing: '0.05em',
              }}>IN</span>
              <pre style={{
                margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                fontSize: 12, fontFamily: 'var(--font-mono, monospace)',
                color: 'var(--text-secondary)',
              }}>{argsStr}</pre>
            </div>
          )}
          {/* 分隔线 */}
          {argsStr && hasResult && tool.result && (
            <div style={{ flex: 'none', height: 1, background: 'var(--line)', margin: '0 0' }} />
          )}
          {/* OUT: 结果（含 diff 渲染）/ 实时输出流 */}
          {hasResult && tool.result ? (
            <div style={{
              display: 'grid', gridTemplateColumns: 'max-content 1fr',
              columnGap: 14, alignItems: 'baseline',
              padding: '10px 14px', maxHeight: 260, overflowY: 'auto',
              border: '1px solid var(--line)', borderRadius: '8px',
              background: 'var(--bg-primary, #fff)',
            }}>
              <span style={{
                position: 'sticky', top: 0, alignSelf: 'start',
                color: 'var(--text-tertiary)', fontSize: 11, fontWeight: 600,
                letterSpacing: '0.05em',
              }}>OUT</span>
              {renderResult(tool.result)}
            </div>
          ) : isRunning && progressText ? (
            <div style={{
              display: 'grid', gridTemplateColumns: 'max-content 1fr',
              columnGap: 14, alignItems: 'baseline',
              padding: '10px 14px', maxHeight: 260, overflowY: 'auto',
              border: '1px solid var(--line)', borderRadius: '8px',
              background: 'var(--bg-primary, #fff)',
            }}>
              <span style={{
                position: 'sticky', top: 0, alignSelf: 'start',
                color: 'var(--text-tertiary)', fontSize: 11, fontWeight: 600,
                letterSpacing: '0.05em',
              }}>OUT</span>
              <pre style={{
                margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                fontSize: 12, fontFamily: 'var(--font-mono, monospace)',
                color: 'var(--text-tertiary)',
              }}>{progressText}</pre>
            </div>
          ) : null}
        </div>
      )}
    </div>
  )
})

// ── E2：同一轮并行的工具合并显示 ──
// 折叠时一行展示该轮所有工具的状态点（第 N 轮 · ⚡M 个并行 · 🔧A ✓ 🔧B ⏳），
// 展开后显示每个工具的完整卡片，真实反映"并行执行"而非多张独立卡片。
// 单工具轮次不套组（由上层直接渲染 ToolCallCard，保持现状）。
const ToolRoundGroup = memo(function ToolRoundGroup({ tools }: { tools: ToolInvocation[] }) {
  const [open, setOpen] = useState(false)
  const round = tools[0]?.round
  return (
    <div style={{ border: '1px solid var(--line)', borderRadius: 8, overflow: 'hidden', background: 'var(--card-bg-solid)' }}>
      {/* 折叠头：一行并行状态点 */}
      <button type="button" onClick={() => setOpen(!open)}
        style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', padding: '4px 12px', border: 'none', background: 'transparent', cursor: 'pointer', fontFamily: 'inherit', color: 'var(--text-secondary)', fontSize: 13, textAlign: 'left' }}>
        {round != null && (
          <span style={{ fontSize: 10.5, lineHeight: 1, padding: '2px 6px', borderRadius: 999, background: 'rgba(152,102,56,0.1)', color: 'var(--accent)', border: '1px solid rgba(152,102,56,0.25)', flexShrink: 0 }}>
            第 {round} 轮
          </span>
        )}
        <span style={{ flexShrink: 0, display: 'inline-flex', alignItems: 'center', gap: 4, fontSize: 12 }}>
          <Zap size={12} strokeWidth={1.75} aria-hidden /> {tools.length} 个并行
        </span>
        {/* 每个工具的紧凑状态点（running/ok/error 实时变化） */}
        <span style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 0, overflow: 'hidden' }}>
          {tools.map((t, i) => {
            const meta = TOOL_LABELS[t.tool] ?? { Icon: Wrench, label: t.tool, variant: 'others' as const }
            const isRunning = t.result === undefined
            const isError = isErrorResult(t.result)
            return (
              <span key={t.id ?? i} style={{ display: 'inline-flex', alignItems: 'center', gap: 4, flexShrink: 0 }}>
                <span style={{ display: 'inline-flex', lineHeight: 1 }}><meta.Icon size={13} strokeWidth={1.75} /></span>
                <span style={{ color: 'var(--text-tertiary)', fontSize: 12, whiteSpace: 'nowrap' }}>{meta.label}</span>
                {isRunning ? (
                  <span style={{ width: 8, height: 8, borderRadius: '50%', border: '1.5px solid var(--text-tertiary)', borderTopColor: 'var(--accent)', animation: 'dsh-spin 0.6s linear infinite' }} />
                ) : isError ? (
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#ef4444' }} />
                ) : (
                  <span style={{ width: 8, height: 8, borderRadius: '50%', background: '#22c55e' }} />
                )}
              </span>
            )
          })}
        </span>
        {/* 展开箭头 */}
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none" aria-hidden style={{ flexShrink: 0, transition: 'transform 0.2s', transform: open ? 'rotate(180deg)' : 'none' }}>
          <path d="M2.5 3.5L5 6L7.5 3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none" />
        </svg>
      </button>
      {/* 展开：每个工具的完整卡片 */}
      {open && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, padding: 6, borderTop: '1px solid var(--line)' }}>
          {tools.map((t, i) => <ToolCallCard key={t.id ?? i} tool={t} />)}
        </div>
      )}
    </div>
  )
})

// E2：按轮次分组 — 同轮（round 相同）的并行工具合并为一组；
// 无 round 的旧数据（历史消息）每个独立成组，保持原有展示。
function groupToolsByRound(tools: ToolInvocation[]): ToolInvocation[][] {
  const groups: ToolInvocation[][] = []
  for (const t of tools) {
    const last = groups[groups.length - 1]
    if (t.round != null && last && last[0].round === t.round && last[0].round != null) {
      last.push(t)
    } else {
      groups.push([t])
    }
  }
  return groups
}

// ── 推理过程折叠区（P3：reasoning_content 展示）──
const ReasoningBlock = memo(function ReasoningBlock({ text }: { text: string }) {
  const [open, setOpen] = useState(false)
  return (
    <div style={{ border: '1px solid var(--line)', borderRadius: 10, overflow: 'hidden', background: 'var(--code-bg)' }}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        style={{ display: 'flex', alignItems: 'center', gap: 8, width: '100%', padding: '7px 12px', border: 'none', background: 'transparent', cursor: 'pointer', fontFamily: 'inherit', color: 'var(--text-tertiary)', fontSize: 12 }}
      >
        <Brain size={13} strokeWidth={1.75} aria-hidden />
        <span style={{ fontWeight: 500 }}>{open ? '思考过程' : `思考过程（${text.length} 字符）`}</span>
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none" style={{ marginLeft: 'auto', transition: 'transform 0.2s', transform: open ? 'rotate(180deg)' : 'none' }}>
          <path d="M2.5 3.5L5 6L7.5 3.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" fill="none" />
        </svg>
      </button>
      {open && (
        <pre style={{ margin: 0, padding: '4px 12px 10px', fontSize: 12, lineHeight: 1.6, fontFamily: 'var(--font-mono, monospace)', color: 'var(--text-tertiary)', whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: 240, overflowY: 'auto' }}>{text}</pre>
      )}
    </div>
  )
})

// ── AI 回复中的可下载文件卡片 ──
// #2 内联产物预览：HTML / SVG / Markdown 在对话里直接渲染（对齐 ChatGPT canvas /
// Claude artifacts 的"改了立刻看见"），其余类型仍是纯下载卡。
// HTML 走 srcDoc iframe + sandbox（只放开 allow-scripts，不开 allow-same-origin，
// 脚本以 opaque origin 运行、摸不到父页面与 Cookie）；SVG 经 Blob URL 由 <img> 渲染
// （<img> 天然不执行 SVG 内脚本）；Markdown 复用 MarkdownRenderer。

/** 超过此字节数的产物不做内联预览（iframe/渲染负担），仍是下载卡 */
const INLINE_PREVIEW_MAX_BYTES = 200 * 1024

/** 产物内联预览类型（按扩展名） */
function getInlinePreviewKind(name: string): 'html' | 'svg' | 'md' | null {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  if (ext === 'html' || ext === 'htm') return 'html'
  if (ext === 'svg') return 'svg'
  if (ext === 'md' || ext === 'markdown') return 'md'
  return null
}

const DownloadableFileCard = memo(function DownloadableFileCard({ file, defaultOpen = false }: { file: ChatFile; defaultOpen?: boolean }) {
  const [converting, setConverting] = useState(false)

  // ── #2 内联预览状态 ──
  const previewKind = getInlinePreviewKind(file.name)
  const previewable = !!previewKind && (file.size ?? file.content?.length ?? 0) <= INLINE_PREVIEW_MAX_BYTES
  const [showPreview, setShowPreview] = useState(() => previewable && defaultOpen)
  // 大文件落盘（content 为空、generatedPath 指向工作区）：首次展开时按需拉取
  const [fetchedContent, setFetchedContent] = useState<string | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const previewContent = file.content || fetchedContent

  const togglePreview = useCallback((e: React.MouseEvent) => {
    e.stopPropagation()
    const willOpen = !showPreview
    setShowPreview(willOpen)
    // 展开且内容未就绪（落盘大文件）→ 按需拉取；出错后再次展开可重试
    if (willOpen && !file.content && file.generatedPath && fetchedContent === null) {
      setPreviewError(null)
      fetchGeneratedFile(file.generatedPath)
        .then(res => setFetchedContent(res.content))
        .catch(err => setPreviewError(err instanceof Error ? err.message : String(err)))
    }
  }, [showPreview, file.content, file.generatedPath, fetchedContent])

  // SVG 预览：Blob URL（<img> 渲染不执行 SVG 内脚本），卸载/换内容时回收
  const svgUrl = useMemo(() => {
    if (previewKind !== 'svg' || !previewContent) return null
    return URL.createObjectURL(new Blob([previewContent], { type: 'image/svg+xml' }))
  }, [previewKind, previewContent])
  useEffect(() => () => { if (svgUrl) URL.revokeObjectURL(svgUrl) }, [svgUrl])

  const handleDownload = useCallback(async () => {
    // 大文件落盘：content 为空但带 generatedPath → 先从后端拉取内容
    let content = file.content
    if (!content && file.generatedPath) {
      try {
        const { getToken } = await import('../lib/auth-api')
        if (!getToken()) {
          alert('登录状态已失效，请刷新页面重新登录后再试')
          return
        }
        const res = await fetchGeneratedFile(file.generatedPath)
        content = res.content
      } catch (err) {
        const msg = err instanceof Error ? err.message : String(err)
        alert(`文件获取失败: ${msg}（生成文件可能已过期，请重新生成）`)
        return
      }
    }

    // 检查是否有内容可下载
    if (!content || !content.trim()) {
      alert('文件内容为空，无法下载')
      return
    }

    // PDF / Word 文件需要将纯文本转为二进制格式
    if (needsConversion(file.name)) {
      // 先检查是否已登录（token 是否存在）
      const { getToken } = await import('../lib/auth-api')
      if (!getToken()) {
        alert('登录状态已失效，请刷新页面重新登录后再试')
        return
      }
      setConverting(true)
      try {
        const blob = await convertToBlob(content, file.name)
        const url = URL.createObjectURL(blob)
        const a = document.createElement('a')
        a.href = url
        a.download = file.name
        document.body.appendChild(a)
        a.click()
        document.body.removeChild(a)
        setTimeout(() => URL.revokeObjectURL(url), 100)
      } catch (convErr) {
        // 区分不同错误类型给用户友好提示
        const errMsg = convErr instanceof Error ? convErr.message : String(convErr)
        if (errMsg.includes('401') || errMsg.includes('403') || errMsg.includes('Unauthorized') || errMsg.includes('Not authenticated')) {
          alert('登录已过期，请刷新页面后重新操作')
        } else if (errMsg.includes('Failed to fetch') || errMsg.includes('NetworkError') || errMsg.includes('fetch failed')) {
          alert('网络连接失败，请检查网络后重试')
        } else {
          alert(`文档生成失败: ${errMsg}\n\n建议：复制文本内容自行保存`)
        }
      } finally {
        setConverting(false)
      }
    } else {
      // 普通文本文件直接下载
      const blob = new Blob([content], { type: file.mime || 'text/plain' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = file.name
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      setTimeout(() => URL.revokeObjectURL(url), 100)
    }
  }, [file])

  // 根据扩展名推断文件类型标签
  const ext = file.name.split('.').pop()?.toLowerCase() || ''
  const typeLabel = (() => {
    const map: Record<string, string> = {
      txt: '文本', md: 'Markdown', json: 'JSON', yaml: 'YAML', yml: 'YAML',
      xml: 'XML', html: 'HTML', css: 'CSS', js: 'JavaScript', ts: 'TypeScript',
      tsx: 'TSX', jsx: 'JSX', py: 'Python', java: 'Java', go: 'Go',
      rs: 'Rust', c: 'C', cpp: 'C++', cs: 'C#', rb: 'Ruby', php: 'PHP',
      sql: 'SQL', sh: 'Shell', csv: 'CSV', svg: 'SVG', vue: 'Vue',
      pdf: 'PDF', doc: 'Word', docx: 'Word', xls: 'Excel', xlsx: 'Excel',
    }
    return map[ext] || ext.toUpperCase() || '文件'
  })()

  const fileSize = file.size ?? new Blob([file.content]).size

  return (
    <div
      style={{
        display: 'flex', flexDirection: 'column',
        borderRadius: 12,
        background: 'var(--card-bg-solid)',
        border: '1px solid var(--line)',
        transition: 'border-color 0.15s',
        boxShadow: SHADOW_SM,
        opacity: converting ? 0.7 : 1,
        overflow: 'hidden',
      }}
      onMouseEnter={e => { if (!converting) { e.currentTarget.style.borderColor = 'var(--accent)' } }}
      onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--line)' }}
    >
      {/* 头部行：点击下载（原有行为不变） */}
      <div
        onClick={converting ? undefined : handleDownload}
        style={{
          display: 'flex', alignItems: 'center', gap: 12,
          padding: '12px 16px', cursor: converting ? 'default' : 'pointer',
        }}
      >
        {/* 文件图标（平面）/ 转换中 loading */}
        {converting ? (
          <div style={{ flexShrink: 0, width: 26, height: 26, display: 'grid', placeItems: 'center' }}>
            <svg width="22" height="22" viewBox="0 0 22 22" fill="none" aria-hidden style={{ animation: 'dsh-spin 0.8s linear infinite' }}>
              <circle cx="11" cy="11" r="8" stroke="var(--line)" strokeWidth="2" fill="none" />
              <path d="M11 3a8 8 0 0 1 8 8" stroke="var(--accent)" strokeWidth="2" strokeLinecap="round" fill="none" />
            </svg>
          </div>
        ) : (
          <span style={{
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
            width: 36, height: 36, flexShrink: 0, borderRadius: 8,
            background: 'var(--code-bg)', color: 'var(--accent)',
          }}>
            <FileText size={18} strokeWidth={1.5} aria-hidden />
          </span>
        )}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0, flex: 1 }}>
          <span style={{ fontSize: 14, fontWeight: 500, color: 'var(--text-primary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{file.name}</span>
          <span style={{ fontSize: 12, color: 'var(--text-tertiary)', display: 'flex', gap: 8, alignItems: 'center' }}>
            <span>{typeLabel}</span>
            <span style={{ width: 3, height: 3, borderRadius: '50%', background: 'var(--text-tertiary)', display: 'inline-block' }} />
            <span>{formatFileSize(fileSize)}</span>
          </span>
        </div>
        {/* #2：可预览类型 → 眼睛开关（与下载图标并排） */}
        {previewable && (
          <button
            type="button"
            onClick={togglePreview}
            aria-label={showPreview ? '收起预览' : '预览'}
            title={showPreview ? '收起预览' : '预览'}
            style={{
              flexShrink: 0, display: 'grid', placeItems: 'center', width: 28, height: 28,
              border: 'none', borderRadius: 6, background: 'transparent',
              color: showPreview ? 'var(--accent)' : 'var(--text-tertiary)',
              cursor: 'pointer', transition: 'color 0.15s, background 0.15s',
            }}
            onMouseEnter={e => { e.currentTarget.style.background = 'var(--code-bg)'; e.currentTarget.style.color = 'var(--accent)' }}
            onMouseLeave={e => { if (!showPreview) { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--text-tertiary)' } }}
          >
            {showPreview ? <EyeOff size={15} strokeWidth={1.75} aria-hidden /> : <Eye size={15} strokeWidth={1.75} aria-hidden />}
          </button>
        )}
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden style={{ flexShrink: 0, color: 'var(--text-tertiary)' }}>
          <path d="M8 2v8M4.5 7L8 10.5L11.5 7M3 13h10" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" fill="none" />
        </svg>
      </div>
      {/* #2：内联预览体 */}
      {previewable && showPreview && (
        <div style={{ borderTop: '1px solid var(--line)', padding: 12, background: 'var(--bg)' }}>
          {previewError ? (
            <div style={{ padding: 10, fontSize: 12.5, color: '#ef4444', lineHeight: 1.6 }}>
              预览加载失败：{previewError}（生成文件可能已过期，可重新生成或下载重试）
            </div>
          ) : !previewContent ? (
            <div style={{ padding: 16, fontSize: 12.5, color: 'var(--text-tertiary)', textAlign: 'center' }}>加载中...</div>
          ) : previewKind === 'html' ? (
            <iframe
              srcDoc={previewContent}
              title={`预览 ${file.name}`}
              sandbox="allow-scripts"
              referrerPolicy="no-referrer"
              loading="lazy"
              style={{ display: 'block', width: '100%', height: 380, border: 'none', borderRadius: 8, background: '#fff' }}
            />
          ) : previewKind === 'svg' ? (
            <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', padding: 8, background: '#fff', borderRadius: 8, border: '1px solid var(--line)' }}>
              <img src={svgUrl ?? undefined} alt={`预览 ${file.name}`} style={{ maxWidth: '100%', maxHeight: 420 }} />
            </div>
          ) : (
            <div style={{ maxHeight: 420, overflowY: 'auto', fontSize: 14, lineHeight: 1.7, color: 'var(--text-primary)' }}>
              <MarkdownRenderer content={previewContent} />
            </div>
          )}
        </div>
      )}
    </div>
  )
})

const AssistantMessage = memo(function AssistantMessage({ msg, index, streaming }: { msg: ChatMessage; index: number; streaming?: boolean }) {
  const [copied, setCopied] = useState(false)
  const copyTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const onCopy = useCallback(() => {
    if (copied) return
    // 复制纯文本（剥离 Markdown 语法），粘贴到别处更友好
    void navigator.clipboard.writeText(markdownToPlainText(msg.content)).then(() => { setCopied(true); copyTimer.current = setTimeout(() => setCopied(false), 1000) })
  }, [copied, msg.content])
  useEffect(() => () => { if (copyTimer.current) clearTimeout(copyTimer.current) }, [])

  // ── 流式光标跟随末尾文本 ──
  // 光标不再作为独立行元素排在 Markdown 之后（那样表格/代码块结尾会掉到下一行），
  // 而是测量最后一个非空文本节点的位置，绝对定位紧跟其后
  const mdWrapRef = useRef<HTMLDivElement>(null)
  const cursorRef = useRef<HTMLSpanElement>(null)
  useLayoutEffect(() => {
    if (!streaming) return
    const wrap = mdWrapRef.current, cursor = cursorRef.current
    if (!wrap || !cursor) return
    const walker = document.createTreeWalker(wrap, NodeFilter.SHOW_TEXT)
    let node: Node | null = null
    let last: Node | null = null
    while ((node = walker.nextNode())) {
      if (node.nodeValue && node.nodeValue.trim()) last = node
    }
    const wrapRect = wrap.getBoundingClientRect()
    if (!last) {
      cursor.style.left = '0px'
      cursor.style.top = '4px'
      return
    }
    const range = document.createRange()
    range.selectNodeContents(last)
    range.collapse(false) // 折叠到文本末尾，得到光标插入点
    const rect = range.getBoundingClientRect()
    cursor.style.left = `${Math.max(0, rect.left - wrapRect.left)}px`
    cursor.style.top = `${Math.max(0, rect.top - wrapRect.top)}px`
  }, [streaming, msg.content])

  const hasFiles = msg.files && msg.files.length > 0
  const hasTools = msg.tools && msg.tools.length > 0
  return (
    <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25, delay: Math.min(index * 0.02, 0.1) }}
      style={{ display: 'flex', flexDirection: 'column', fontSize: 15.5, lineHeight: '26px', color: 'var(--text-primary)' }}>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
        {/* 消息级模式标记（E）：回看历史时清楚该回复是哪种模式产生的 */}
        {msg.mode && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: -4 }}>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, padding: '1px 8px', fontSize: 10.5, lineHeight: '18px', color: 'var(--text-tertiary)', borderRadius: 999, border: '1px solid var(--line)', background: 'var(--code-bg)', fontFamily: 'var(--font-mono, monospace)', letterSpacing: '0.02em' }}>
              {msg.mode === 'agent' ? '🤖 Agent 办公' : '💬 纯聊天'}
            </span>
          </div>
        )}
        {hasTools && (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
            {/* E2：同一轮并行的工具合并显示（单工具轮次直接渲染卡片） */}
            {groupToolsByRound(msg.tools!).map((group, gi) =>
              group.length === 1
                ? <ToolCallCard key={group[0].id ?? `g${gi}`} tool={group[0]} />
                : <ToolRoundGroup key={`g${gi}-${group[0].round ?? gi}`} tools={group} />
            )}
          </div>
        )}
        {/* 推理过程折叠区（始终显示，默认折叠，可手动展开） */}
        {msg.reasoning && <ReasoningBlock text={msg.reasoning} />}
        {/* 流式期间也渲染 Markdown（rAF 节流控制频率），结束后无视觉跳变 */}
        <div ref={mdWrapRef} style={{ flex: 1, minWidth: 0, position: 'relative' }}>
          <MarkdownRenderer content={msg.content} streaming={streaming} />
          {streaming && (
            <span ref={cursorRef} aria-hidden style={{ position: 'absolute', bottom: 4, width: 2.5, height: '1.1em', background: 'var(--accent)', borderRadius: 1.5, opacity: 0.9, animation: 'cursor-pulse 1.1s ease-in-out infinite', pointerEvents: 'none', boxShadow: '0 0 6px color-mix(in srgb, var(--accent) 50%, transparent)' }} />
          )}
        </div>
        {hasFiles && (
          <div style={{
            display: 'flex', flexDirection: 'column', gap: 8, marginTop: 4,
            // #2：有可内联预览的产物时放宽容器（预览体需要横向空间），否则保持窄卡美学
            maxWidth: (msg.files ?? []).some(f => getInlinePreviewKind(f.name))
              ? 'min(640px, 100%)'
              : 'min(280px, 42%)',
          }}>
            {msg.files!.map((f, i) => (
              <DownloadableFileCard
                key={i}
                file={f}
                // 产物只有一个且可预览 → 默认展开预览（"改了立刻看见"）；多个保持折叠避免刷屏
                defaultOpen={msg.files!.length === 1 && getInlinePreviewKind(f.name) !== null}
              />
            ))}
          </div>
        )}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', marginTop: 16, marginLeft: -6, gap: 4 }}>
        <button type="button" onClick={onCopy} aria-label={copied ? '已复制' : '复制'}
          style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', width: 28, height: 28, border: 'none', borderRadius: 6, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer', transition: 'background 0.15s, color 0.15s' }}
          onMouseEnter={e => { e.currentTarget.style.background = 'var(--code-bg)'; e.currentTarget.style.color = 'var(--text-secondary)' }}
          onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--text-tertiary)' }}>
          {copied ? <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden><path d="M3.5 8.5l3 3 6-6.5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" /></svg>
            : <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden><rect x="5" y="3" width="8" height="10" rx="2" stroke="currentColor" strokeWidth="1.5" fill="none" /><path d="M3 5.5v7a2 2 0 002 2h5" stroke="currentColor" strokeWidth="1.5" fill="none" strokeLinecap="round" /></svg>}
        </button>
      </div>
    </motion.div>
  )
})

// ============================================================
// Hero 空会话页面 — 3D 动态图像 + 文字 + 输入框 + 底部工具栏
// ============================================================
interface HeroPageProps {
  input: string
  onInput: (e: React.ChangeEvent<HTMLTextAreaElement>) => void
  onKeyDown: (e: React.KeyboardEvent) => void
  onSend: () => void
  onStop?: () => void
  canSend: boolean
  loading: boolean
  models: ReturnType<typeof useChat>['models']
  selectedModelId: number | undefined
  setSelectedModelId: ReturnType<typeof useChat>['setSelectedModelId']
  pendingHasImage: boolean
  quota: ReturnType<typeof useChat>['quota']
  contextPressure: ContextPressure | null
  inputRef: React.RefObject<HTMLTextAreaElement | null>
  mirrorRef: React.RefObject<HTMLDivElement | null>
  attachedFiles: AttachedFile[]
  onFilesSelected: (files: File[]) => void
  onRemoveFile: (index: number) => void
  maxFilesPerMessage: number
  supportsVision: boolean
  onPaste: (e: React.ClipboardEvent) => void
  ingest?: IngestState | null   // 优化11：附件摄取进度（并行+逐项状态）
  onCancelIngest?: () => void
  mode: 'chat' | 'agent'
  onSelectMode: (m: 'chat' | 'agent') => void
  injectMode?: WorkspaceInjectMode
  onCycleInjectMode?: () => void
  // 首页优化1+7：快捷建议卡片（Agent 模式含动态技能卡）
  skills: ChatSkillInfo[]
  onPickSuggestion: (text: string) => void
}

// ── Hero 空状态专用模式切换条（平面，仅 Hero 页使用）──
// 与共享 ModeSwitch 隔离：不修改 ModeSwitch，聊天区保持原样不受影响。
function HeroModeBar({ mode, onSelect, injectMode, onCycleInjectMode }: {
  mode: 'chat' | 'agent'
  onSelect: (m: 'chat' | 'agent') => void
  injectMode?: WorkspaceInjectMode
  onCycleInjectMode?: () => void
}) {
  const seg = (active: boolean): React.CSSProperties => ({
    display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
    height: 36, padding: '0 20px', fontSize: 13.5, fontWeight: 600,
    fontFamily: 'inherit', cursor: 'pointer', border: 'none', borderRadius: 999,
    background: active ? 'var(--accent)' : 'transparent',
    color: active ? '#fff' : 'var(--text-secondary)',
    transition: 'background 0.15s, color 0.15s',
  })
  const chip = (active: boolean): React.CSSProperties => ({
    display: 'inline-flex', alignItems: 'center', gap: 6, padding: '3px 14px', fontSize: 11.5, lineHeight: '20px',
    border: `1px solid ${active ? 'color-mix(in srgb, var(--accent) 40%, var(--line))' : 'var(--line)'}`,
    borderRadius: 999, fontFamily: 'inherit', cursor: 'pointer',
    background: 'var(--card-bg-solid)',
    color: active ? 'var(--accent)' : 'var(--text-tertiary)',
    transition: 'border-color 0.15s, color 0.15s',
  })
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10, flex: 'none', marginBottom: 16 }}>
      {/* 分段控制器（平面） */}
      <div role="group" aria-label="对话模式"
        style={{
          display: 'inline-flex', padding: 3, gap: 3, borderRadius: 999,
          background: 'var(--code-bg)',
          border: '1px solid var(--line)',
        }}>
        <button type="button" onClick={() => onSelect('chat')} style={seg(mode === 'chat')}
          title="纯聊天：AI 直接回答，不调用任何工具，响应更快">
          💬 纯聊天
        </button>
        <button type="button" onClick={() => onSelect('agent')} style={seg(mode === 'agent')}
          title="Agent 办公：AI 可读写工作区文件、执行代码、加载技能">
          🤖 Agent 办公
        </button>
      </div>
      {/* 两个开关并排一行（自动带入文件内容 + 思考过程） */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        {/* 注入开关优化3+5：三档「自动带入文件内容」— 点击循环 全文 → 仅目录 → 关 */}
        {mode === 'agent' && onCycleInjectMode && (
          <>
            <button type="button" onClick={onCycleInjectMode}
              title={INJECT_MODE_TITLE}
              aria-label={`自动带入文件内容：${INJECT_MODE_LABEL[injectMode ?? 'full']}，点击切换档位`}
              style={chip(injectMode !== 'off')}>
              <span style={{ opacity: injectMode === 'off' ? 0.45 : 1 }}>📁 自动带入文件内容</span>
              <span style={{ fontWeight: 600 }}>{INJECT_MODE_LABEL[injectMode ?? 'full']}</span>
            </button>
            {injectMode === 'off' && (
              <span style={{ fontSize: 11.5, lineHeight: '18px', color: 'var(--text-tertiary)' }}>
                {INJECT_MODE_OFF_HINT}
              </span>
            )}
          </>
        )}
      </div>
    </div>
  )
}

// ── Hero 首页快捷建议卡片（首页优化1+7）──
// 纯聊天：4 张静态场景卡；Agent：2 张能力卡 + 前 2 个技能卡（后端技能表动态生成）。
// 点击 = 填充输入框并聚焦（不自动发送——Agent 任务成本高，保留用户编辑确认一步）。
type HeroSuggestionItem = { Icon: LucideIcon; title: string; prompt: string; tip?: string }

const HERO_CHAT_SUGGESTIONS: HeroSuggestionItem[] = [
  { Icon: PenLine,   title: '写作润色', prompt: '帮我润色这段文字，保持原意，让表达更流畅专业：' },
  { Icon: Code2,     title: '代码求助', prompt: '这段代码有问题，帮我定位 bug 并给出修复方案：' },
  { Icon: Languages, title: '中英互译', prompt: '帮我翻译下面的内容（中译英或英译中），保留原文语气与格式：' },
  { Icon: BookOpen,  title: '通俗解释', prompt: '用通俗易懂的语言解释这个概念，并举一个例子：' },
]
const HERO_AGENT_SUGGESTIONS: HeroSuggestionItem[] = [
  { Icon: FileText,  title: '生成文档', prompt: '帮我撰写一份文档并保存到工作区，主题和要求如下：' },
  { Icon: BarChart3, title: '数据分析', prompt: '帮我编写并运行代码分析下面的数据，给出结论：' },
]

function HeroSuggestions({ mode, skills, onPick }: { mode: 'chat' | 'agent'; skills: ChatSkillInfo[]; onPick: (text: string) => void }) {
  const items: HeroSuggestionItem[] = mode === 'chat'
    ? HERO_CHAT_SUGGESTIONS
    : [
        ...HERO_AGENT_SUGGESTIONS,
        ...skills.slice(0, 2).map(s => ({ Icon: Puzzle, title: s.name, prompt: `/${s.name} `, tip: s.description })),
      ]
  return (
    <div role="group" aria-label="快捷开始"
      style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 8, width: '100%', maxWidth: COMPOSER_CARD_MAX_WIDTH, marginTop: 14 }}>
      {items.map(it => (
        <button key={it.title} type="button" onClick={() => onPick(it.prompt)} title={it.tip || it.prompt}
          style={{
            display: 'flex', alignItems: 'center', gap: 8, padding: '9px 12px', minWidth: 0,
            border: '1px solid var(--line)', borderRadius: 12, fontFamily: 'inherit', cursor: 'pointer',
            background: 'var(--card-bg-solid)',
            boxShadow: SHADOW_SM,
            transition: 'transform 0.15s, border-color 0.15s',
          }}
          onMouseEnter={e => {
            e.currentTarget.style.transform = 'translateY(-1px)'
            e.currentTarget.style.borderColor = 'color-mix(in srgb, var(--accent) 40%, var(--line))'
          }}
          onMouseLeave={e => {
            e.currentTarget.style.transform = 'none'
            e.currentTarget.style.borderColor = 'var(--line)'
          }}>
          <it.Icon size={15} strokeWidth={1.75} aria-hidden style={{ flex: 'none', color: 'var(--accent)' }} />
          <span style={{ fontSize: 12.5, fontWeight: 600, color: 'var(--text-secondary)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 }}>{it.title}</span>
        </button>
      ))}
    </div>
  )
}

function HeroPage({ input, onInput, onKeyDown, onSend, onStop, canSend, loading, models, selectedModelId, setSelectedModelId, pendingHasImage, quota, contextPressure, inputRef, mirrorRef, attachedFiles, onFilesSelected, onRemoveFile, maxFilesPerMessage, supportsVision, onPaste, ingest, onCancelIngest, mode, onSelectMode, injectMode, onCycleInjectMode, skills, onPickSuggestion }: HeroPageProps) {
  return (
    <div style={{ position: 'relative', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', height: '100%', width: '100%', overflow: 'hidden', padding: '0 16px' }}>
      <HeroGlow />

      {/* 品牌图标 + 标题文字（整体上移，视觉重心偏上） */}
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12, marginBottom: 28, marginTop: -64 }}>
        <Hero3DIcon />
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <span style={{ fontSize: 34, lineHeight: '40px', fontWeight: 700, color: 'var(--text-primary)', fontFamily: 'var(--font-serif)', letterSpacing: '0.04em' }}>智语知你</span>
            {/* BETA 徽章（平面） */}
            <span style={{
              padding: '2px 10px', border: '1px solid color-mix(in srgb, var(--accent) 35%, var(--line))', borderRadius: 999,
              background: 'var(--code-bg)',
              color: 'var(--accent)', fontFamily: 'monospace', fontSize: 11, lineHeight: '16px', fontWeight: 600,
              letterSpacing: '0.04em', whiteSpace: 'nowrap',
            }}>BETA</span>
          </div>
          <span style={{ fontSize: 14, color: 'var(--text-tertiary)', letterSpacing: '0.06em' }}>智能助理 · 使命必达</span>
        </div>
      </div>

      {/* 输入框 + 底部工具栏 */}
      <div style={{ width: '100%', maxWidth: COMPOSER_CARD_MAX_WIDTH, display: 'flex', flexDirection: 'column', gap: 0 }}>
        {/* 模式切换条（Hero 专用 3D 布局；聊天区仍用共享 ModeSwitch） */}
        <HeroModeBar mode={mode} onSelect={onSelectMode} injectMode={injectMode} onCycleInjectMode={onCycleInjectMode} />
        {ingest && onCancelIngest && <IngestProgress ingest={ingest} onCancel={onCancelIngest} />}
        {quota && (
          <div style={{ width: '100%', maxWidth: COMPOSER_CARD_MAX_WIDTH, marginBottom: 6, padding: '4px 8px', borderRadius: 8, background: 'var(--code-bg)', color: 'var(--text-secondary)', fontSize: 12, lineHeight: '18px', display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{ width: 6, height: 6, borderRadius: '50%', background: quota.image.remaining > 0 ? 'var(--accent)' : '#c44', animation: 'dsh-pulse-soft 2s ease-in-out infinite' }} />
            剩余 {quota.image.remaining} / {quota.image.daily_limit}
          </div>
        )}
          {/* 输入卡片 */}
          <div style={{ boxSizing: 'border-box', position: 'relative', display: 'flex', flexDirection: 'column', gap: 8, width: '100%', maxWidth: COMPOSER_CARD_MAX_WIDTH, paddingTop: 12, border: '1px solid var(--line)', borderRadius: 22, background: 'var(--card-bg-solid)', boxShadow: '0 2px 12px rgba(0,0,0,0.06)', fontSize: 16, lineHeight: '24px' }}>
              <AttachedFilesList files={attachedFiles} onRemove={onRemoveFile} />
            <div style={{ position: 'relative', zIndex: 1 }}>
              <div ref={mirrorRef} aria-hidden style={{ visibility: 'hidden', pointerEvents: 'none', boxSizing: 'border-box', padding: '10px 12px 8px 16px', fontFamily: 'inherit', fontSize: 'inherit', lineHeight: 'inherit', whiteSpace: 'pre-wrap', wordBreak: 'break-word', overflowWrap: 'anywhere', minHeight: 64 }}>{input + '\n'}</div>
              <textarea ref={inputRef} value={input} onChange={onInput} onKeyDown={onKeyDown} onPaste={onPaste} placeholder="输入你的问题... (Enter 发送, Shift+Enter 换行)" rows={1}
                style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', resize: 'none', overflow: 'hidden', border: 'none', outline: 'none', background: 'transparent', color: 'var(--text-primary)', caretColor: 'var(--accent)', boxSizing: 'border-box', padding: '10px 12px 8px 16px', fontFamily: 'inherit', fontSize: 'inherit', lineHeight: 'inherit', whiteSpace: 'pre-wrap', wordBreak: 'break-word', overflowWrap: 'anywhere' }} />
            </div>
            {/* 底部工具栏: 上传 + 上下文使用量 + 模型切换 + 发送 */}
            <div style={{ position: 'relative', zIndex: 2, display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '2px 8px 6px', minWidth: 0 }}>
              {/* 左侧 — 上传 + 记忆管理 */}
              <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                <FileUploadButton onFiles={onFilesSelected} disabled={loading} maxFiles={maxFilesPerMessage} currentCount={attachedFiles.length} supportsVision={supportsVision} />
                <MemoryButton />
              </div>
              {/* 右侧 — ContextMeter + 模型选择 + 发送 */}
              <div style={{ display: 'flex', alignItems: 'center', marginLeft: 'auto', gap: 8 }}>
                <ContextMeter pressure={contextPressure} />
                <ModelSelector models={models} selectedModelId={selectedModelId} setSelectedModelId={setSelectedModelId} pendingHasImage={pendingHasImage} />
                {/* 发送/停止按钮：loading 时变为可点击的停止按钮 */}
                <button type="button"
                  onClick={loading ? onStop : onSend}
                  disabled={!loading && !canSend}
                  aria-label={loading ? '停止生成' : '发送'}
                  style={{ display: 'grid', placeItems: 'center', flex: 'none', width: 34, height: 34, border: 'none', borderRadius: 999, background: loading ? '#e5484d' : canSend ? 'var(--accent)' : 'var(--line)', color: '#fff', cursor: loading || canSend ? 'pointer' : 'default', transition: 'background 0.15s', transform: 'translateY(-2px)', opacity: loading || canSend ? 1 : 0.4 }}>
                  {loading ? (
                    /* 停止图标：白色方形 */
                    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden><rect x="4" y="4" width="8" height="8" rx="2" fill="currentColor" /></svg>
                  ) : (
                    /* 发送图标：箭头 */
                    <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden><path d="M8 2.5L13 7.5L8 12.5M3 7.5h10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none" /></svg>
                  )}
                </button>
              </div>
            </div>
          </div>
          {/* 首页优化1+7：快捷建议卡片（纯聊天=场景卡；Agent=能力卡+技能卡） */}
          <HeroSuggestions mode={mode} skills={skills} onPick={onPickSuggestion} />
        </div>
      </div>
  )
}

// ============================================================
// 记忆管理面板 — 让用户看到/删除 Agent 记的内容
// ============================================================
function MemoryButton() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <button type="button" onClick={() => setOpen(true)} aria-label="记忆管理" title="记忆管理"
        style={{ display: 'grid', placeItems: 'center', width: 28, height: 28, border: 'none',
          borderRadius: 6, background: 'transparent', color: 'var(--text-tertiary)',
          cursor: 'pointer', transition: 'background 0.15s, color 0.15s' }}
        onMouseEnter={e => { e.currentTarget.style.background = 'var(--code-bg)'; e.currentTarget.style.color = 'var(--text-secondary)' }}
        onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--text-tertiary)' }}>
        <Brain size={16} />
      </button>
      {open && <MemoryPanel onClose={() => setOpen(false)} />}
    </>
  )
}

function MemoryPanel({ onClose }: { onClose: () => void }) {
  const [memories, setMemories] = useState<MemoryItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const load = useCallback(async () => {
    setLoading(true); setError('')
    try { setMemories(await listMemories()) }
    catch (e) { setError(e instanceof Error ? e.message : '加载失败') }
    finally { setLoading(false) }
  }, [])

  useEffect(() => { void load() }, [load])

  const handleDelete = useCallback(async (id: number) => {
    try { await deleteMemory(id); setMemories(prev => prev.filter(m => m.id !== id)) }
    catch (e) { setError(e instanceof Error ? e.message : '删除失败') }
  }, [])

  const handleClearAll = useCallback(async () => {
    if (!confirm('确定清空所有记忆？此操作不可恢复。')) return
    try { await clearAllMemories(); setMemories([]) }
    catch (e) { setError(e instanceof Error ? e.message : '清空失败') }
  }, [])

  const typeLabel: Record<string, string> = { fact: '事实', preference: '偏好', context: '上下文' }
  const typeColor: Record<string, string> = {
    fact: 'var(--accent)', preference: '#22c55e', context: '#3b82f6',
  }

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 1000, display: 'flex',
      alignItems: 'center', justifyContent: 'center', background: 'rgba(0,0,0,0.4)',
      backdropFilter: 'blur(2px)',
    }} onClick={onClose}>
      <div style={{
        width: 'min(560px, 92vw)', maxHeight: '80vh', display: 'flex', flexDirection: 'column',
        background: 'var(--card-bg-solid)', borderRadius: 16, boxShadow: '0 12px 40px rgba(0,0,0,0.2)',
        overflow: 'hidden',
      }} onClick={e => e.stopPropagation()}>
        {/* 标题栏 */}
        <div style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '16px 20px', borderBottom: '1px solid var(--line)',
        }}>
          <div style={{ fontSize: 16, fontWeight: 600, color: 'var(--text-primary)' }}>
            🧠 记忆管理
            <span style={{ marginLeft: 8, fontSize: 12, fontWeight: 400, color: 'var(--text-tertiary)' }}>
              {memories.length} 条
            </span>
          </div>
          <div style={{ display: 'flex', gap: 8 }}>
            {memories.length > 0 && (
              <button type="button" onClick={handleClearAll}
                style={{ padding: '4px 10px', border: '1px solid #ef4444', borderRadius: 6,
                  background: 'transparent', color: '#ef4444', fontSize: 12, cursor: 'pointer' }}>
                清空全部
              </button>
            )}
            <button type="button" onClick={onClose} aria-label="关闭"
              style={{ border: 'none', background: 'transparent', color: 'var(--text-tertiary)',
                cursor: 'pointer', fontSize: 18, padding: '0 4px' }}>✕</button>
          </div>
        </div>
        {/* 内容 */}
        <div style={{ flex: 1, overflow: 'auto', padding: '12px 20px' }}>
          {loading ? (
            <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', padding: 40 }}>加载中...</div>
          ) : error ? (
            <div style={{ color: '#ef4444', padding: 12, fontSize: 13 }}>{error}</div>
          ) : memories.length === 0 ? (
            <div style={{ textAlign: 'center', color: 'var(--text-tertiary)', padding: 40, fontSize: 14 }}>
              <div style={{ fontSize: 32, marginBottom: 8 }}>📭</div>
              暂无记忆<br />
              <span style={{ fontSize: 12 }}>Agent 会在对话中自动记住你的偏好和事实</span>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
              {memories.map(m => (
                <div key={m.id} style={{
                  display: 'flex', alignItems: 'flex-start', gap: 8, padding: '10px 12px',
                  borderRadius: 8, background: 'var(--code-bg)', border: '1px solid var(--line)',
                }}>
                  {/* 类型标签 */}
                  <span style={{
                    flex: 'none', padding: '2px 8px', borderRadius: 4, fontSize: 11,
                    fontWeight: 500, color: '#fff',
                    background: typeColor[m.memory_type] || 'var(--text-tertiary)',
                  }}>
                    {typeLabel[m.memory_type] || m.memory_type}
                  </span>
                  {/* 内容 */}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 13, lineHeight: 1.5, color: 'var(--text-primary)', wordBreak: 'break-word' }}>
                      {m.content}
                    </div>
                    <div style={{ marginTop: 4, fontSize: 11, color: 'var(--text-tertiary)', display: 'flex', gap: 12 }}>
                      <span>来源: {m.source}</span>
                      <span>命中: {m.access_count}次</span>
                      {m.has_embedding && <span style={{ color: 'var(--accent)' }}>● 向量</span>}
                    </div>
                  </div>
                  {/* 删除按钮 */}
                  <button type="button" onClick={() => handleDelete(m.id)} aria-label="删除"
                    style={{ flex: 'none', border: 'none', background: 'transparent',
                      color: 'var(--text-tertiary)', cursor: 'pointer', fontSize: 14, padding: '2px 6px' }}
                    title="删除">✕</button>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ============================================================
// Active 会话模式 — 输入卡片
// ============================================================
interface ActiveInputCardProps {
  input: string
  onInput: (e: React.ChangeEvent<HTMLTextAreaElement>) => void
  onKeyDown: (e: React.KeyboardEvent) => void
  onSend: () => void
  onStop?: () => void
  onReset?: () => void
  canSend: boolean
  loading: boolean
  models: ReturnType<typeof useChat>['models']
  selectedModelId: number | undefined
  setSelectedModelId: ReturnType<typeof useChat>['setSelectedModelId']
  pendingHasImage: boolean
  quota: ReturnType<typeof useChat>['quota']
  contextPressure: ContextPressure | null
  inputRef: React.RefObject<HTMLTextAreaElement | null>
  mirrorRef: React.RefObject<HTMLDivElement | null>
  attachedFiles: AttachedFile[]
  onFilesSelected: (files: File[]) => void
  onRemoveFile: (index: number) => void
  maxFilesPerMessage: number
  supportsVision: boolean
  onPaste: (e: React.ClipboardEvent) => void
  ingest?: IngestState | null   // 优化11：附件摄取进度（并行+逐项状态）
  onCancelIngest?: () => void
}

function ActiveInputCard({ input, onInput, onKeyDown, onSend, onStop, onReset, canSend, loading, models, selectedModelId, setSelectedModelId, pendingHasImage, quota, contextPressure, inputRef, mirrorRef, attachedFiles, onFilesSelected, onRemoveFile, maxFilesPerMessage, supportsVision, onPaste, ingest, onCancelIngest }: ActiveInputCardProps) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', padding: `0 ${COMPOSER_SIDE_CLEARANCE}px 8px` }}>
      {ingest && onCancelIngest && <IngestProgress ingest={ingest} onCancel={onCancelIngest} />}
      {quota && (
        <div style={{ width: '100%', maxWidth: COMPOSER_CARD_MAX_WIDTH, marginBottom: 6, padding: '4px 8px', borderRadius: 8, background: 'var(--code-bg)', color: 'var(--text-secondary)', fontSize: 12, lineHeight: '18px', display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: quota.image.remaining > 0 ? 'var(--accent)' : '#c44', animation: 'dsh-pulse-soft 2s ease-in-out infinite' }} />
          剩余 {quota.image.remaining} / {quota.image.daily_limit}
        </div>
      )}
      <div style={{ boxSizing: 'border-box', position: 'relative', display: 'flex', flexDirection: 'column', gap: 8, width: '100%', maxWidth: COMPOSER_CARD_MAX_WIDTH, paddingTop: 12, border: '1px solid var(--line)', borderRadius: 22, background: 'var(--card-bg-solid)', boxShadow: '0 2px 12px rgba(0,0,0,0.06)', fontSize: 16, lineHeight: '24px' }}>
          <AttachedFilesList files={attachedFiles} onRemove={onRemoveFile} />
        <div style={{ position: 'relative', zIndex: 1 }}>
          <div ref={mirrorRef} aria-hidden style={{ visibility: 'hidden', pointerEvents: 'none', boxSizing: 'border-box', padding: '10px 12px 8px 16px', fontFamily: 'inherit', fontSize: 'inherit', lineHeight: 'inherit', whiteSpace: 'pre-wrap', wordBreak: 'break-word', overflowWrap: 'anywhere', minHeight: 64 }}>{input + '\n'}</div>
            <textarea ref={inputRef} value={input} onChange={onInput} onKeyDown={onKeyDown} onPaste={onPaste} placeholder="继续对话..." rows={1}
            style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', resize: 'none', overflow: 'hidden', border: 'none', outline: 'none', background: 'transparent', color: 'var(--text-primary)', caretColor: 'var(--accent)', boxSizing: 'border-box', padding: '10px 12px 8px 16px', fontFamily: 'inherit', fontSize: 'inherit', lineHeight: 'inherit', whiteSpace: 'pre-wrap', wordBreak: 'break-word', overflowWrap: 'anywhere' }} />
        </div>
        <div style={{ position: 'relative', zIndex: 2, display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between', gap: 12, padding: '2px 8px 6px', minWidth: 0 }}>
          {/* 左侧 — 上传 + 清空 */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
            <FileUploadButton onFiles={onFilesSelected} disabled={loading} maxFiles={maxFilesPerMessage} currentCount={attachedFiles.length} supportsVision={supportsVision} />
            {onReset && (
              <button type="button" onClick={onReset} aria-label="清空对话"
                style={{ display: 'grid', placeItems: 'center', width: 28, height: 28, border: 'none', borderRadius: 999, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer', transition: 'background 0.15s, color 0.15s' }}
                onMouseEnter={e => { e.currentTarget.style.background = 'var(--code-bg)'; e.currentTarget.style.color = '#c44' }}
                onMouseLeave={e => { e.currentTarget.style.background = 'transparent'; e.currentTarget.style.color = 'var(--text-tertiary)' }}>
                <svg width="14" height="14" viewBox="0 0 14 14" fill="none" aria-hidden>
                  <path d="M2.5 4.5h9M5.5 4.5V2.5h3v2" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" fill="none" />
                  <path d="M4 4.5l.5 7c.03.42.39.75.81.75h3.38c.42 0 .78-.33.81-.75l.5-7" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" fill="none" />
                  <path d="M6 7v3M8 7v3" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" />
                </svg>
              </button>
            )}
          </div>
          {/* 右侧 — ContextMeter + 模型选择 + 发送 */}
          <div style={{ display: 'flex', alignItems: 'center', marginLeft: 'auto', gap: 8 }}>
            <ContextMeter pressure={contextPressure} />
            <ModelSelector models={models} selectedModelId={selectedModelId} setSelectedModelId={setSelectedModelId} pendingHasImage={pendingHasImage} />
            {/* 发送/停止按钮：loading 时变为可点击的停止按钮 */}
            <button type="button"
              onClick={loading ? onStop : onSend}
              disabled={!loading && !canSend}
              aria-label={loading ? '停止生成' : '发送'}
              style={{ display: 'grid', placeItems: 'center', flex: 'none', width: 34, height: 34, border: 'none', borderRadius: 999, background: loading ? '#e5484d' : canSend ? 'var(--accent)' : 'var(--line)', color: '#fff', cursor: loading || canSend ? 'pointer' : 'default', transition: 'background 0.15s', transform: 'translateY(-2px)', opacity: loading || canSend ? 1 : 0.4 }}>
              {loading ? (
                /* 停止图标：白色方形 */
                <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden><rect x="4" y="4" width="8" height="8" rx="2" fill="currentColor" /></svg>
              ) : (
                /* 发送图标：箭头 */
                <svg viewBox="0 0 16 16" width="16" height="16" aria-hidden><path d="M8 2.5L13 7.5L8 12.5M3 7.5h10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none" /></svg>
              )}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ============================================================
// 主组件 — 带侧边栏的 DSH 桌面端布局
// ============================================================
export function AIChat() {
  const { isLoggedIn } = useAuth()
  const [showAuthModal, setShowAuthModal] = useState(false)
  const {
    sessions, activeSessionId, activeSessionPk,
    newSession, switchSession, removeSession, renameSession,
    messages, loading, error, contextPressure,
    quota, models, selectedModelId, setSelectedModelId,
    lastModelSwitch,
    send, stopGeneration, reset, refreshQuota,
    storageWarning, onDismissStorageWarning,
    setSessionMode, getSessionMode, defaultMode,
    agentPhase, agentToolName,
    workspaceInjectMode, cycleWorkspaceInjectMode,
    loadedSkillsBySession, removeLoadedSkill,
  } = useChat()
  const [input, setInput] = useState('')
  const [atBottom, setAtBottom] = useState(true)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [sidebarTab, setSidebarTab] = useState<'sessions' | 'workspace' | 'skills'>('sessions')
  // SessionSidebar 已 memo 化，折叠回调必须稳定，否则内联箭头每次渲染都换引用、memo 白包
  const collapseSidebar = useCallback(() => setSidebarCollapsed(true), [])
  const expandSidebar = useCallback(() => setSidebarCollapsed(false), [])
  // 工作区面板的未登录空态里点"登录"→ 弹本页的 AuthModal（面板自身不持有弹窗）
  useEffect(() => {
    const onRequestLogin = () => setShowAuthModal(true)
    window.addEventListener(REQUEST_LOGIN_EVENT, onRequestLogin)
    return () => window.removeEventListener(REQUEST_LOGIN_EVENT, onRequestLogin)
  }, [])
  // #1 工具卡→工作区：工具卡点"打开此文件"→ 展开侧边栏并切到工作区 tab
  // （文件定位由 WorkspacePanel 自己监听同名事件完成：展开目录+预览+滚动高亮）
  useEffect(() => {
    const onLocate = () => {
      setSidebarCollapsed(false)
      setSidebarTab('workspace')
    }
    window.addEventListener(WORKSPACE_LOCATE_EVENT, onLocate)
    return () => window.removeEventListener(WORKSPACE_LOCATE_EVENT, onLocate)
  }, [])
  // #1 工作区→对话：文件行点"引用到对话"→ 把路径 token 插入输入框并聚焦（不覆盖已有内容）
  useEffect(() => {
    const onReference = (e: Event) => {
      const path = (e as CustomEvent<{ path?: string }>).detail?.path
      if (!path) return
      setInput(prev => {
        const token = `「${path}」`
        if (prev.includes(token)) return prev
        return prev.trim() ? `${prev}\n${token} ` : `${token} `
      })
      inputRef.current?.focus()
    }
    window.addEventListener(WORKSPACE_REFERENCE_EVENT, onReference)
    return () => window.removeEventListener(WORKSPACE_REFERENCE_EVENT, onReference)
  }, [])
  // 当前会话模式（chat 纯聊天 / agent 办公）— 按会话持久化，默认纯聊天
  const currentMode = getSessionMode(activeSessionId)
  // Hero 空状态预选的初始模式（无会话时选择，发送首条消息创建会话后生效）
  const [pendingMode, setPendingMode] = useState<'chat' | 'agent' | null>(null)
  // 展示模式：有活跃会话 → 该会话的模式；Hero 空状态 → 预选模式 ?? 用户最后使用的模式
  const displayMode = activeSessionId ? currentMode : (pendingMode ?? defaultMode ?? 'chat')
  // 用户主动停止后显示"继续任务"按钮
  const [showContinue, setShowContinue] = useState(false)
  // 历史被裁剪提示（已关闭状态）
  const [droppedDismissed, setDroppedDismissed] = useState(false)
  const [attachedFiles, setAttachedFiles] = useState<AttachedFile[]>([])
  const [maxFilesPerMessage, setMaxFilesPerMessage] = useState(5) // 默认 5，后续从后端获取
  // 优化8：模型选择器的多模态门禁只看本轮待发附件。历史里的图片已被 slimHistoryAttachments
  // 整块换成占位符，纯文本模型读得懂，「会话发过图」不再构成切换限制。
  const pendingHasImage = attachedFiles.some(f => f.isImage || !!f.imageXml)
  // 优化11：附件摄取进度 —— 并行处理、逐项状态、可取消
  const [ingest, setIngest] = useState<IngestState | null>(null)
  const ingestBatchSeq = useRef(0)
  const ingestControllers = useRef(new Set<AbortController>())
  const cancelIngest = useCallback(() => {
    ingestControllers.current.forEach(c => c.abort())
    ingestControllers.current.clear()
    setIngest(null)
  }, [])
  // A：技能面板 — 拉取可用技能列表（仅 Agent 模式展示）
  const [skills, setSkills] = useState<ChatSkillInfo[]>([])
  useEffect(() => {
    let cancelled = false
    fetchChatSkills()
      .then(list => { if (!cancelled) setSkills(list) })
      .catch(() => { /* 静默：技能面板显示空列表 */ })
    return () => { cancelled = true }
  }, [])
  // A：当前会话已加载技能（技能面板展示 / 取消加载）
  // 加载动作不再由面板直接触发：点击技能只是把 `/技能名` 填进输入框，
  // 真正加载发生在模型调用 skill 工具之后（useChat 内部自动登记到会话）
  const loadedSkills = activeSessionId ? (loadedSkillsBySession[activeSessionId] ?? EMPTY_LOADED_SKILLS) : EMPTY_LOADED_SKILLS
  // 已知技能名集合 — 识别输入框里的 `/技能名` token（单技能校验 + 填入时替换旧技能）
  const skillNameSet = useMemo(() => new Set(skills.map(s => s.name)), [skills])
  const showSkillToast = useCallback((msg: string, title: string, icon: 'warn' | 'info' = 'info', durationMs?: number) => {
    const ms = durationMs ?? (icon === 'warn' ? 3500 : 2500)
    setToast3D({ msg, icon, title, secs: Math.max(1, Math.round(ms / 1000)) })
    if (toast3DTimer.current) clearTimeout(toast3DTimer.current)
    toast3DTimer.current = setTimeout(() => setToast3D(null), ms)
  }, [])
  // #6 工作区内部的 alert → 非阻塞 toast（面板自身不持有 toast 组件，派发事件由本页弹）
  useEffect(() => {
    const onToast = (e: Event) => {
      const d = (e as CustomEvent<{ msg?: string; icon?: 'warn' | 'info'; title?: string }>).detail
      if (d?.msg) showSkillToast(d.msg, d.title ?? '提示', d.icon ?? 'info')
    }
    window.addEventListener(WORKSPACE_TOAST_EVENT, onToast)
    return () => window.removeEventListener(WORKSPACE_TOAST_EVENT, onToast)
  }, [showSkillToast])
  const handleUnloadSkill = useCallback((name: string) => {
    if (!activeSessionId) return
    removeLoadedSkill(activeSessionId, name)
    showSkillToast(`技能「${name}」已取消加载`, '已取消加载')
  }, [activeSessionId, removeLoadedSkill, showSkillToast])
  // 3D 立体 Toast 状态（icon 区分主题：warn 红色警告 / info 蓝色信息）
  const [toast3D, setToast3D] = useState<{ msg: string; icon: 'warn' | 'info'; title?: string; subtitle?: string; secs?: number } | null>(null)
  const toast3DTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const composerSeatRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const mirrorRef = useRef<HTMLDivElement>(null)
  const atBottomRef = useRef(true)

  // 模型自动降级（401/402/403/429 等）— toast 提示已切换
  useEffect(() => {
    if (lastModelSwitch) {
      setToast3D({
        msg: `当前模型请求失败（HTTP ${lastModelSwitch.status}），已自动切换至 ${lastModelSwitch.name}`,
        icon: 'warn',
        title: '模型已自动切换',
      })
      if (toast3DTimer.current) clearTimeout(toast3DTimer.current)
      toast3DTimer.current = setTimeout(() => setToast3D(null), 3500)
    }
  }, [lastModelSwitch])

  useEffect(() => { refreshQuota() }, [refreshQuota])

  // 获取对话配置（文件上传数量限制等）
  useEffect(() => {
    fetchChatConfig().then(cfg => {
      setMaxFilesPerMessage(cfg.max_files_per_message)
    }).catch(() => { /* 使用默认值 */ })
  }, [])

  useEffect(() => {
    if (scrollRef.current && atBottomRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages, loading])

  useEffect(() => {
    const seat = composerSeatRef.current
    const scroller = scrollRef.current
    if (!seat || !scroller || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => { scroller.style.setProperty('--composer-height', `${seat.offsetHeight}px`) })
    observer.observe(seat)
    return () => observer.disconnect()
  }, [])

  const onScroll = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const floor = Math.max(0, el.scrollHeight - el.clientHeight)
    const isAtBottom = floor - el.scrollTop <= FOLLOW_THRESHOLD + 1
    atBottomRef.current = isAtBottom
    setAtBottom(isAtBottom)
  }, [])

  const toBottom = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    el.scrollTop = el.scrollHeight
    atBottomRef.current = true
    setAtBottom(true)
  }, [])

  const handleSend = useCallback(() => {
    if ((!input.trim() && attachedFiles.length === 0) || loading) return
    // 新消息发出后隐藏"继续任务"按钮
    setShowContinue(false)
    // 未登录则弹出登录窗口（并明确提示，避免"点了没反应"的困惑）
    if (!isLoggedIn) {
      setShowAuthModal(true)
      showToast('登录状态已过期，请重新登录后再发送', 'warn', '需要登录')
      return
    }
    // 单技能限制：一条消息只允许带一个 `/技能名`。
    // 带多个会让模型重复加载技能、把注入预算吃满后互相截断，因此直接拦下让用户挑一个。
    const skillTokens = findSkillTokens(input, skillNameSet)
    if (skillTokens.length > 1) {
      const extra = Array.from(new Set(skillTokens.slice(1))).map(n => `/${n}`).join('、')
      showSkillToast(`一条消息只能使用一个技能，请删除多余的 ${extra}`, '技能冲突', 'warn')
      return
    }
    // 发给后端的完整消息（含文件内容），前端只显示用户实际输入的文本+附件卡片
    const displayText = input.trim()
    let message = displayText
    // 图片附件带 dataUrl（会话内缩略图展示；持久化时会剥离，不占 localStorage）
    // ref 是图片的工作区存档路径，随 attachments 一起持久化（~40 字符），刷新后据此取回缩略图。
    // 它**只进渲染层**：下面的 message 组装保持原样，发给模型的正文里没有任何存档路径，
    // 「历史图片一律是占位符」这条载荷不变量因此仍然成立。
    const fileMeta = attachedFiles.map(f => ({
      name: f.name,
      size: f.size,
      ...(f.isImage && f.dataUrl ? { dataUrl: f.dataUrl } : {}),
      ...(f.ref ? { ref: f.ref } : {}),
    }))
    if (attachedFiles.length > 0) {
      const textFiles = attachedFiles.filter(f => !f.isImage && !f.imageXml)
      // 图片类附件：普通图片（dataUrl）或图片型 PDF（imageXml 预构造的 <image> 串）
      const imageFiles = attachedFiles.filter(f => f.isImage || f.imageXml)
      const parts: string[] = []
      // 文本文件作为 XML 附件
      if (textFiles.length > 0) {
        const filesXml = textFiles.map(f =>
          `<file name="${f.name}" size="${formatFileSize(f.size)}">\n${f.content}\n</file>`
        ).join('\n')
        parts.push(filesXml)
      }
      // 图片文件作为 base64 数据 URL（含图片型 PDF 的多页图片）
      if (imageFiles.length > 0) {
        const imagesXml = imageFiles.map(f =>
          f.imageXml ?? `<image name="${f.name}" size="${formatFileSize(f.size)}">\n${f.dataUrl}\n</image>`
        ).join('\n')
        parts.push(imagesXml)
      }
      const attachmentsXml = `<attachments>\n${parts.join('\n')}\n</attachments>`
      message = displayText ? `${displayText}\n\n${attachmentsXml}` : attachmentsXml
    }
    // 前端只显示用户实际输入的文本（没输入就不显示文字，只显示文件卡片）
    send(message, undefined, displayText, fileMeta.length > 0 ? fileMeta : undefined, pendingMode ?? undefined)
    setPendingMode(null)
    setInput('')
    setAttachedFiles([])
    atBottomRef.current = true
    setAtBottom(true)
  }, [input, loading, send, isLoggedIn, attachedFiles, pendingMode, skillNameSet, showSkillToast])

  // 当前选中模型是否支持多模态
  const selectedModel = models.find(m => m.id === selectedModelId) || models.find(m => m.is_default) || models[0]
  const supportsVision = selectedModel?.supports_vision ?? false
  // 技能全文注入预算（优化2）：与后端 skill_service.LOADED_SKILLS_BUDGET_RATIO=0.5 同口径
  // （context_window tokens × 0.5 字符），面板据此画占用条并预警截断
  const skillBudgetChars = Math.floor((selectedModel?.context_length || 0) * 0.5)

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
  }

  const handleInput = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => { setInput(e.target.value) }, [])
  // 首页优化1+7：点击建议卡片 = 填充输入框并聚焦（不自动发送，留给用户编辑确认）
  const handlePickSuggestion = useCallback((text: string) => {
    setInput(text)
    const el = inputRef.current
    if (el) {
      el.focus()
      try { el.setSelectionRange(text.length, text.length) } catch { /* noop */ }
    }
  }, [inputRef])
  // 技能面板点击「使用技能」= 把 `/技能名 ` 填入输入框。
  // 先清掉已有的技能 token 再前置新的，保证一条消息只带一个技能；
  // 不依赖 activeSessionId，首页空状态同样可用（优化5：去掉"请先发一条消息"门禁）。
  const handleUseSkill = useCallback((name: string) => {
    const token = `/${name} `
    // 读 ref 而不是闭包 input：闭包 input 会让本函数每敲一个键就换引用，
    // 传给 memo 化的 SessionSidebar 后浅比较必然不等，memo 直接失效（实测确认）
    const rest = stripSkillTokens(inputRef.current?.value ?? '', skillNameSet)
    setInput(rest ? `${token}${rest}` : token)
    showSkillToast(`已填入 /${name}，补充需求后发送`, '技能已选择')
    // 受控 textarea 的值要等重渲染才落到 DOM，同步 setSelectionRange 会按旧值定位
    requestAnimationFrame(() => {
      const el = inputRef.current
      if (!el) return
      el.focus()
      try { el.setSelectionRange(token.length, token.length) } catch { /* noop */ }
    })
  }, [skillNameSet, showSkillToast, inputRef])

  // 处理文件选择
  const handleFilesSelected = useCallback(async (files: File[]) => {
    // 二次防护：按钮已做阻断，这里静默截断超出的文件
    if (maxFilesPerMessage <= 0) return
    const remaining = maxFilesPerMessage - attachedFiles.length
    if (remaining <= 0) return
    const filesToProcess = files.length > remaining ? files.slice(0, remaining) : files

    // ── 优化11：并行处理 + 逐项进度 + 可取消 ──
    // 原先是 for await 串行：多张图时压缩单张可达 1~2.5s，全程零反馈也停不下来。
    // key 带批次号，前一批还没跑完时再选一批也能各自精确更新自己的行。
    const batchId = ++ingestBatchSeq.current
    const keys = filesToProcess.map((_, i) => `${batchId}:${i}`)
    setIngest(prev => ({
      items: [
        ...(prev?.items ?? []),
        ...filesToProcess.map((f, i) => ({ key: keys[i], name: f.name, phase: 'queued' as const })),
      ],
    }))
    const controller = new AbortController()
    ingestControllers.current.add(controller)
    const { signal } = controller
    const patchIngest = (key: string, p: Partial<IngestItem>) => {
      setIngest(prev => prev ? { items: prev.items.map(it => it.key === key ? { ...it, ...p } : it) } : prev)
    }

    // ── 优化12：内容指纹去重（跨批次 + 批次内）──
    const seenHashes = new Map<string, string>()
    for (const f of attachedFiles) if (f.hash) seenHashes.set(f.hash, f.name)

    // ── 优化10：失败原因聚合后一条内联提示（alert 会逐个阻塞弹窗）──
    const notices: string[] = []

    // ── 优化9：待存档到工作区的图片（压缩后的字节 + 内容寻址路径）──
    // 批处理结束后一次性上传，不逐图发：既省后端独立限流桶的 token，也避免
    // 逐图触发工作区面板刷新（每次刷新都会连带一轮惰性清理与文件树请求）。
    const archiveItems: { blob: Blob; path: string }[] = []

    const results = await mapWithConcurrency(filesToProcess, INGEST_CONCURRENCY, async (file, i): Promise<AttachedFile | null> => {
      const key = keys[i]
      const cancelled = (): null => { patchIngest(key, { phase: 'skipped', detail: '已取消' }); return null }
      const skip = (notice: string, detail = '已跳过'): null => {
        notices.push(notice)
        patchIngest(key, { phase: 'skipped', detail })
        return null
      }
      if (signal.aborted) return cancelled()
      patchIngest(key, { phase: 'working' })

      // 不含点的文件名（README）没有扩展名；split('.').pop() 会把整个文件名当成扩展名，
      // 那样下面的「无扩展名」分支永远不可达，提示会误导成「类型 .readme 不支持」
      const ext = file.name.includes('.') ? (file.name.split('.').pop()?.toLowerCase() || '') : ''
      const isImage = file.type.startsWith('image/') || IMAGE_EXTENSIONS.has(ext)
      const isDoc = !isImage && isDocumentFile(file)

      // 先跑零成本闸门再做重活：注定被拒的文件不该白读一遍磁盘、白解码一次
      if (isImage) {
        if (!supportsVision) return skip(`当前模型不支持图片输入（模型能力限制，与聊天模式无关），已跳过 ${file.name}`, '模型不支持')
        if (file.size > MAX_IMAGE_UPLOAD) return skip(`图片 ${file.name} 超过 ${formatFileSize(MAX_IMAGE_UPLOAD)} 限制`, '过大')
      } else if (isDoc) {
        if (file.size > MAX_DOCUMENT_SIZE) return skip(`文档 ${file.name} 超过 ${formatFileSize(MAX_DOCUMENT_SIZE)} 限制`, '过大')
      } else {
        if (file.size > MAX_FILE_SIZE) return skip(`文件 ${file.name} 超过 ${formatFileSize(MAX_FILE_SIZE)} 限制`, '过大')
        // 无扩展名的文件，只放行常见的那几个
        const knownNoExt = ['dockerfile', 'makefile', 'gitignore']
        const lowerName = file.name.toLowerCase()
        if (!ext && !knownNoExt.includes(lowerName)) return skip(`文件 ${file.name} 无扩展名`, '无扩展名')
        if (ext && !TEXT_EXTENSIONS.has(ext) && !knownNoExt.includes(lowerName)) {
          return skip(`文件 ${file.name} 类型 .${ext} 不支持，仅支持文本文件`, '类型不支持')
        }
      }

      // get 与 set 之间没有 await：JS 单线程下同批次的两份相同文件不可能双双通过
      const hash = await fileFingerprint(file)
      if (signal.aborted) return cancelled()
      const dupOf = seenHashes.get(hash)
      if (dupOf) return skip(`与已附加的「${dupOf}」内容完全相同，已跳过重复的 ${file.name}`, '重复')
      seenHashes.set(hash, file.name)

      // ── 图片 ──
      if (isImage) {
        try {
          const prepared = await prepareImageFile(file)
          if (signal.aborted) return cancelled()
          // 压缩失败会回退原图，此时仍可能超过发送上限
          if (prepared.size > MAX_IMAGE_SIZE) {
            return skip(`图片 ${file.name} 压缩后仍有 ${formatFileSize(prepared.size)}，超过 ${formatFileSize(MAX_IMAGE_SIZE)} 限制`, '压缩后仍过大')
          }
          // ── 优化9：存档到工作区，刷新后据此还原缩略图，Agent 也能 read_file 回看（优化14）──
          // 路径由内容寻址在客户端算出，所以 ref 可以**乐观写入**：不必等上传响应，
          // 更不必事后回填消息对象（saveMessagesToStorage 用引用比较做脏跟踪，原地
          // mutate 检测不到，回填极易漏存）。存档失败 = ref 悬空 = 取图 404 =
          // 静默回落到文件图标，不报错、不重试，也绝不影响发送。
          const ref = await buildChatArchiveRef(prepared.blob, file.name)
          if (signal.aborted) return cancelled()
          if (ref) archiveItems.push({ blob: prepared.blob, path: ref })
          patchIngest(key, { phase: 'done', detail: `${formatFileSize(file.size)} → ${formatFileSize(prepared.size)}` })
          return {
            name: file.name, size: prepared.size, content: '',
            dataUrl: prepared.dataUrl, isImage: true, hash,
            ...(ref ? { ref } : {}),
          }
        } catch {
          return skip(`图片 ${file.name} 读取失败`, '读取失败')
        }
      }

      // ── 文档 (PDF / Word .docx / Excel .xlsx) — 后端统一提取文本 ──
      if (isDoc) {
        try {
          const doc = await extractDocumentText(file, signal)
          if (signal.aborted) return cancelled()
          // 图片型 PDF（扫描件）：转前 N 页图片，需 vision 模型识别
          if (doc.is_image_pdf) {
            if (!supportsVision) return skip(`文档 ${file.name} 是扫描件（图片型 PDF），当前模型不支持图片输入`, '模型不支持')
            const base = file.name.replace(/\.pdf$/i, '')
            const imageXml = doc.images.map((img, idx) =>
              `<image name="${base}-p${idx + 1}" size="0">\n${img}\n</image>`
            ).join('\n')
            patchIngest(key, { phase: 'done', detail: `扫描件 ${doc.images.length} 页` })
            return { name: file.name, size: file.size, content: '', imageXml, hash }
          }
          if (!doc.content.trim()) return skip(`文档 ${file.name} 未提取到文本内容（可能是空文档）`, '无文本内容')
          patchIngest(key, { phase: 'done', detail: `${doc.total_chars} 字` })
          return { name: file.name, size: file.size, content: doc.content, hash }
        } catch (err) {
          if (signal.aborted) return cancelled()
          return skip(`文档 ${file.name} 解析失败: ${err instanceof Error ? err.message : String(err)}`, '解析失败')
        }
      }

      // ── 文本文件 ──
      try {
        const content = await readFileContent(file)
        if (signal.aborted) return cancelled()
        patchIngest(key, { phase: 'done', detail: formatFileSize(file.size) })
        return { name: file.name, size: file.size, content, hash }
      } catch {
        return skip(`文件 ${file.name} 读取失败`, '读取失败')
      }
    })

    ingestControllers.current.delete(controller)
    setIngest(prev => {
      if (!prev) return prev
      const items = prev.items.filter(it => !keys.includes(it.key))
      return items.length > 0 ? { items } : null
    })

    if (signal.aborted) {
      showSkillToast('已取消附件处理，未完成的文件不会附加', '已取消')
      return
    }

    // mapWithConcurrency 按下标保序，附件卡片顺序与用户选择顺序一致
    const newFiles = results.filter((f): f is AttachedFile => f !== null)
    if (newFiles.length > 0) setAttachedFiles(prev => [...prev, ...newFiles])

    // ── 优化9：存档上传 fire-and-forget ──
    // 绝不 await：setAttachedFiles 在整批 resolve 之后才执行，而 handleSend 读的就是
    // attachedFiles —— 存档一旦挂起或失败，用户就发不出消息了。ref 是客户端算好的，
    // 本来也不需要响应回填。
    // 取消的批次走上面的 early return，压根不会发存档请求，不存在「已上传撤不回」的残留。
    if (archiveItems.length > 0) {
      void archiveChatImages(archiveItems)
        .then((res) => {
          // 路径内容寻址：这张图可能先前失败过并被负缓存，成功后必须作废，
          // 否则整个页面生命周期里它都只会显示文件图标
          for (const item of res.archived) invalidateChatImageRef(item.path)
          // 每批只刷一次面板树，且只在真有文件落地时刷（失败时刷纯属白跑一轮惰性清理）
          if (res.archived.length > 0) window.dispatchEvent(new Event('workspace-refresh'))
        })
        .catch(() => {
          // best-effort：存档失败就是 ref 悬空，取图 404 后静默回落到文件图标。
          // 不提示也不重试 —— 缩略图不值得打断用户，更不值得让他以为消息没发出去。
        })
    }

    // 优化10：多条失败原因合并成一条（alert 连环阻塞，单条 toast 又只剩最后一条）
    if (notices.length > 0) {
      const shown = notices.slice(0, 5)
      const body = notices.length === 1
        ? notices[0]
        : shown.map(n => `· ${n}`).join('\n')
          + (notices.length > shown.length ? `\n· …另有 ${notices.length - shown.length} 项` : '')
      showSkillToast(body, `${notices.length} 个附件未添加`, 'warn', 3500 + Math.min(notices.length, 6) * 900)
    }
  }, [maxFilesPerMessage, attachedFiles, supportsVision, showSkillToast])

  // 粘贴图片处理 — 在 handleFilesSelected 之后定义以避免 TDZ
  const handlePaste = useCallback((e: React.ClipboardEvent) => {
    const items = e.clipboardData?.items
    if (!items) return
    const imageFiles: File[] = []
    for (let i = 0; i < items.length; i++) {
      const item = items[i]
      if (item.kind === 'file' && item.type.startsWith('image/')) {
        const file = item.getAsFile()
        if (file) imageFiles.push(file)
      }
    }
    if (imageFiles.length > 0) {
      // 非多模态模型阻断粘贴图片
      if (!supportsVision) {
        e.preventDefault()
        setToast3D({ msg: '当前模型不支持图片输入（模型能力限制，与聊天模式无关），请切换支持多模态的模型', icon: 'warn', title: '不支持图片输入', subtitle: '当前模型未开启多模态能力' })
        if (toast3DTimer.current) clearTimeout(toast3DTimer.current)
        toast3DTimer.current = setTimeout(() => setToast3D(null), 3000)
        return
      }
      e.preventDefault()
      handleFilesSelected(imageFiles)
    }
  }, [supportsVision, handleFilesSelected])

  // 移除已选文件
  const handleRemoveFile = useCallback((index: number) => {
    setAttachedFiles(prev => prev.filter((_, i) => i !== index))
  }, [])

  // 3D toast 提示（icon 决定主题与标题：warn=红色警告，info=蓝色信息）
  const showToast = useCallback((msg: string, icon: 'warn' | 'info' = 'info', title?: string, subtitle?: string) => {
    setToast3D({ msg, icon, title, subtitle })
    if (toast3DTimer.current) clearTimeout(toast3DTimer.current)
    toast3DTimer.current = setTimeout(() => setToast3D(null), 3000)
  }, [])

  // 新建对话
  const handleNewSession = useCallback(async () => {
    await newSession()
    setInput('')
  }, [newSession])

  // 历史被裁剪提示：裁剪数量清零（新对话/压缩后）时重置关闭状态，下次裁剪重新提示
  useEffect(() => {
    if (contextPressure?.historyDropped === undefined || contextPressure.historyDropped === 0) {
      setDroppedDismissed(false)
    }
  }, [contextPressure?.historyDropped])

  // 停止 AI 回复 — 用户点击停止按钮时中断流式请求
  const handleStop = useCallback(() => {
    stopGeneration()
    // 仅用户主动停止后显示"继续任务"按钮，避免无中断也出现
    setShowContinue(true)
  }, [stopGeneration])

  // 继续任务 — 停止后从上次位置继续执行（上下文保留在历史中）
  const handleContinue = useCallback(() => {
    if (loading) return
    setShowContinue(false)
    send('请继续刚才的任务，从上次停止的地方继续执行。', undefined, '请继续刚才的任务')
  }, [send, loading])

  // 选择当前会话模式（chat 纯聊天 ↔ agent 办公），按会话持久化
  // 无活跃会话（Hero 空状态）时：预选模式，发送首条消息创建会话后生效
  // 模式锁定（P1 配套）：当前会话已有对话记录后不允许切换模式，
  // 点击另一模式弹窗提示（模式在首次对话时确定并锁定）
  // 模式联动侧边栏（A 方案）：切 Agent → 展开侧边栏并切到「工作区」tab；
  // 切纯聊天 → 回到「会话」tab（纯聊天模式下工作区 tab 本就隐藏）
  const handleSelectMode = useCallback((next: 'chat' | 'agent') => {
    // 已有对话记录 → 模式锁定，弹窗提示
    if (activeSessionId && messages.length > 0 && next !== currentMode) {
      showToast(
        next === 'agent'
          ? `当前对话已锁定为 💬 纯聊天 模式，无法切换到 Agent 办公；如需使用请新建对话。`
          : `当前对话已锁定为 🤖 Agent 办公 模式，无法切换到纯聊天；如需使用请新建对话。`,
        'info',
        '模式已锁定',
      )
      return
    }
    if (next === 'agent') {
      setSidebarCollapsed(false)
      setSidebarTab('workspace')
    } else {
      setSidebarTab('sessions')
    }
    if (!activeSessionId) {
      // 与 Hero 展示模式（displayMode）保持一致
      if (next === (pendingMode ?? defaultMode ?? 'chat')) return
      setPendingMode(next)
      showToast(
        next === 'agent'
          ? '已选择 Agent 办公模式：发送第一条消息后生效（AI 可读写文件/执行代码/加载技能）'
          : '已选择纯聊天模式：发送第一条消息后生效（更轻量、响应更快）',
        'info',
      )
      return
    }
    if (next === currentMode) return
    setSessionMode(activeSessionId, next)
    showToast(
      next === 'agent'
        ? '已切换到 Agent 办公模式：AI 可读写工作区文件、执行代码、加载技能'
        : '已切换到纯聊天模式：更轻量、响应更快，AI 不调用任何工具',
      'info',
    )
  }, [activeSessionId, currentMode, messages.length, pendingMode, defaultMode, setSessionMode, showToast])

  const canSend = (!!input.trim() || attachedFiles.length > 0) && !loading
  const isEmpty = messages.length === 0 && !loading

  return (
    <div style={{ display: 'flex', height: '100vh', minHeight: 0, background: 'var(--bg)', position: 'relative', overflow: 'hidden' }}>
      {/* 侧边栏 */}
      <AnimatePresence initial={false}>
        {sidebarCollapsed ? (
          <SidebarRail key="rail" onToggle={expandSidebar} />
        ) : (
          <SessionSidebar
            key="sidebar"
            sessions={sessions}
            activeSessionId={activeSessionId}
            collapsed={false}
            onToggle={collapseSidebar}
            onNewSession={handleNewSession}
            onSwitch={switchSession}
            onDelete={removeSession}
            onRename={renameSession}
            sidebarTab={sidebarTab}
            onTabChange={setSidebarTab}
            skills={skills}
            loadedSkills={loadedSkills}
            onUseSkill={handleUseSkill}
            onUnloadSkill={handleUnloadSkill}
            skillBudgetChars={skillBudgetChars}
            mode={displayMode}
          />
        )}
      </AnimatePresence>

      {/* 主对话区 */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, position: 'relative', overflow: 'hidden' }}>
        {isEmpty ? (
          // ---- Hero 空会话页面 ----
          // 只有 3D 动态图像 + 文字 + 输入框 + 底部工具栏（上下文使用量、模型切换）
          // 输入信息回车后才创建会话并进入 Active 会话页面
          <HeroPage
            input={input}
            onInput={handleInput}
            onKeyDown={handleKeyDown}
            onSend={handleSend}
            onStop={handleStop}
            canSend={canSend}
            loading={loading}
            models={models}
            selectedModelId={selectedModelId}
            setSelectedModelId={setSelectedModelId}
            pendingHasImage={pendingHasImage}
            quota={quota}
            contextPressure={contextPressure}
            inputRef={inputRef}
            mirrorRef={mirrorRef}
            attachedFiles={attachedFiles}
            onFilesSelected={handleFilesSelected}
            onRemoveFile={handleRemoveFile}
            maxFilesPerMessage={maxFilesPerMessage}
            supportsVision={supportsVision}
            onPaste={handlePaste}
            ingest={ingest}
            onCancelIngest={cancelIngest}
            mode={displayMode}
            onSelectMode={handleSelectMode}
            injectMode={workspaceInjectMode}
            onCycleInjectMode={cycleWorkspaceInjectMode}
            skills={skills}
            onPickSuggestion={handlePickSuggestion}
          />
        ) : (
          // ---- Active 会话模式 ----
          <>
            <div ref={scrollRef} onScroll={onScroll} data-conversation-scroll=""
style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0, overflowY: 'auto', overflowX: 'hidden', scrollbarGutter: 'stable', padding: `24px calc(${COMPOSER_SIDE_CLEARANCE}px + 16px)` }}>
              <div style={{ maxWidth: CHAT_CONTENT_WIDTH, width: '100%', margin: '0 auto', display: 'flex', flexDirection: 'column', gap: 32 }}>
                {/* 历史被裁剪提示（记忆被窗口策略丢弃时告知用户） */}
                {contextPressure?.historyDropped != null && contextPressure.historyDropped > 0 && !droppedDismissed && (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '8px 12px', borderRadius: 10, background: 'var(--code-bg)', border: '1px solid var(--line)', fontSize: 12, lineHeight: '18px', color: 'var(--text-secondary)' }}>
                    <span style={{ flex: 1, minWidth: 0 }}>
                      为保持在上下文窗口内，已自动丢弃较早的 <b>{contextPressure.historyDropped}</b> 条消息。
                      继续对话即可，系统会在上下文接近上限（80%）时自动压缩历史为摘要。
                    </span>
                    <button type="button" onClick={() => setDroppedDismissed(true)} aria-label="关闭提示"
                      style={{ flex: 'none', display: 'grid', placeItems: 'center', width: 22, height: 22, border: 'none', borderRadius: 6, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer' }}>
                      <svg width="10" height="10" viewBox="0 0 12 12" fill="none" aria-hidden><path d="M3.5 3.5l5 5M8.5 3.5l-5 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
                    </button>
                  </div>
                )}
                {messages.map((msg, i) => {
                  const isLastAssistant = loading && msg.role === 'assistant' && i === messages.length - 1
                  return msg.role === 'user'
                    ? <UserMessage key={msg.id ?? i} msg={msg} index={i} />
                    : <AssistantMessage key={msg.id ?? i} msg={msg} index={i} streaming={isLastAssistant} />
                })}
                <AnimatePresence>{loading && <TurnStatus phase={agentPhase} toolName={agentToolName} />}</AnimatePresence>
                <AnimatePresence>
                  {error && (
                    <motion.div initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -8 }}
                      style={{ display: 'grid', gridTemplateColumns: '10px minmax(0, 1fr)', gap: 8, alignItems: 'start', padding: '4px 0', fontSize: 13, lineHeight: '20px' }}>
                      <span style={{ marginTop: 5, width: 8, height: 8, borderRadius: '50%', background: '#c44', flex: 'none' }} />
                      <div style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
                        <span style={{ marginRight: 6, color: '#c44', fontWeight: 600 }}>对话出错</span>
                        <span style={{ color: 'var(--text-secondary)' }}>{error}</span>
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
                {!atBottom && <ScrollToBottom onClick={toBottom} />}
              </div>
            </div>
            <div ref={composerSeatRef} data-composer-seat=""
              style={{ display: 'flex', flex: 'none', flexDirection: 'column', position: 'sticky', bottom: 0, zIndex: 7, background: `linear-gradient(180deg, color-mix(in srgb, var(--bg) 0%, transparent) 0px, var(--bg) 36px)` }}>
              {/* 本地存储空间不足警告 */}
              {storageWarning && (
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, maxWidth: COMPOSER_CARD_MAX_WIDTH, width: '100%', margin: '0 auto 6px', boxSizing: 'border-box', padding: '8px 12px', borderRadius: 10, background: 'rgba(200,60,60,0.08)', border: '1px solid rgba(200,60,60,0.25)', fontSize: 12, lineHeight: '18px', color: 'var(--text-secondary)' }}>
                  <AlertTriangle size={13} strokeWidth={1.75} aria-hidden style={{ flex: 'none', color: '#c44' }} />
                  <span style={{ flex: 1, minWidth: 0 }}>{storageWarning}</span>
                  <button type="button" onClick={onDismissStorageWarning} aria-label="关闭警告"
                    style={{ flex: 'none', display: 'grid', placeItems: 'center', width: 22, height: 22, border: 'none', borderRadius: 6, background: 'transparent', color: 'var(--text-tertiary)', cursor: 'pointer' }}>
                    <svg width="10" height="10" viewBox="0 0 12 12" fill="none" aria-hidden><path d="M3.5 3.5l5 5M8.5 3.5l-5 5" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" /></svg>
                  </button>
                </div>
              )}
              {/* 继续任务按钮 — 仅用户主动停止后显示 */}
              {showContinue && !loading && messages.length > 0 && (
                <div style={{ display: 'flex', justifyContent: 'center', padding: '2px 0 6px' }}>
                  <button type="button" onClick={handleContinue} title="从上次停止的位置继续执行任务"
                    style={{ display: 'inline-flex', alignItems: 'center', gap: 6, padding: '6px 16px', fontSize: 12, fontWeight: 500, border: '1px solid var(--accent)', borderRadius: 999, background: 'var(--code-bg)', color: 'var(--accent)', cursor: 'pointer', fontFamily: 'inherit', transition: 'all 0.15s' }}
                    onMouseEnter={e => { e.currentTarget.style.background = 'var(--accent)'; e.currentTarget.style.color = '#fff' }}
                    onMouseLeave={e => { e.currentTarget.style.background = 'var(--code-bg)'; e.currentTarget.style.color = 'var(--accent)' }}>
                    <Zap size={12} strokeWidth={1.75} aria-hidden /> 继续任务
                  </button>
                </div>
              )}
              {/* 模式切换器 — 当前会话模式，分段式一键切换（B）；首次对话后锁定 */}
              <ModeSwitch mode={currentMode} onSelect={handleSelectMode} injectMode={workspaceInjectMode} onCycleInjectMode={cycleWorkspaceInjectMode} />
              <ActiveInputCard input={input} onInput={handleInput} onKeyDown={handleKeyDown} onSend={handleSend} onStop={handleStop} onReset={reset} canSend={canSend} loading={loading}
                models={models} selectedModelId={selectedModelId} setSelectedModelId={setSelectedModelId} pendingHasImage={pendingHasImage}
                quota={quota} contextPressure={contextPressure} inputRef={inputRef} mirrorRef={mirrorRef}
                attachedFiles={attachedFiles} onFilesSelected={handleFilesSelected} onRemoveFile={handleRemoveFile} maxFilesPerMessage={maxFilesPerMessage} supportsVision={supportsVision} onPaste={handlePaste} ingest={ingest} onCancelIngest={cancelIngest} />
            </div>
          </>
        )}
      </div>
      <AuthModal open={showAuthModal} onClose={() => setShowAuthModal(false)} />
      <UserBadge />
      {/* 浮动 Toast（平面卡片）— icon 区分主题：warn 红色警告 / info 蓝色信息 */}
      <AnimatePresence>
        {toast3D && (
          <motion.div
            initial={{ opacity: 0, y: -12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.2 }}
            onClick={() => setToast3D(null)}
            style={{
              position: 'fixed', top: '50%', left: '50%',
              transform: 'translate(-50%, -50%)',
              zIndex: 9999, cursor: 'pointer',
              display: 'flex', alignItems: 'flex-start', gap: 10,
              padding: '14px 16px',
              borderRadius: 12, minWidth: 280, maxWidth: 360,
              background: 'var(--card-bg-solid)',
              border: `1px solid ${toast3D.icon === 'warn' ? 'rgba(200,60,60,0.35)' : 'rgba(86,156,210,0.35)'}`,
              boxShadow: SHADOW_MD,
            }}>
            <span style={{
              display: 'inline-flex', flex: 'none', marginTop: 1,
              color: toast3D.icon === 'warn' ? '#c44' : 'rgba(86,156,210,0.95)',
            }}>
              {toast3D.icon === 'warn'
                ? <AlertTriangle size={17} strokeWidth={1.75} aria-hidden />
                : <Info size={17} strokeWidth={1.75} aria-hidden />}
            </span>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0 }}>
              <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>
                {toast3D.title ?? (toast3D.icon === 'warn' ? '警告' : '提示')}
              </span>
              {toast3D.subtitle && (
                <span style={{ fontSize: 11, color: 'var(--text-tertiary)' }}>{toast3D.subtitle}</span>
              )}
              <p style={{
                fontSize: 13, lineHeight: '20px', color: 'var(--text-secondary)',
                margin: '4px 0 0', whiteSpace: 'pre-line',
              }}>{toast3D.msg}</p>
              <span style={{ marginTop: 6, fontSize: 10, color: 'var(--text-tertiary)' }}>
                点击关闭 · {toast3D.secs ?? 3}秒后自动消失
              </span>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}