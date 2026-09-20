/**
 * 文档文本提取 — 统一走后端 /workspace/doc-extract 服务
 *
 * 后端用 openpyxl / PyPDF2 / python-docx 解析 xlsx / pdf / docx，
 * 前端不再内置 pdfjs-dist / mammoth 等重依赖（减小打包体积）。
 * 图片型 PDF（无文本层）由后端转前 N 页图片，供 vision 模型识别。
 */

/** 支持文档提取的扩展名 */
export const DOCUMENT_EXTENSIONS = new Set(['pdf', 'docx', 'xlsx'])

/** 最大文档大小 (10 MB) */
export const MAX_DOCUMENT_SIZE = 10 * 1024 * 1024

/** 判断文件是否为可提取文本的文档 */
export function isDocumentFile(file: File): boolean {
  const ext = file.name.split('.').pop()?.toLowerCase() || ''
  return DOCUMENT_EXTENSIONS.has(ext)
}

/** 后端返回的提取结果 */
export interface DocumentExtractResult {
  name: string
  content: string
  total_chars: number
  is_image_pdf: boolean
  images: string[]   // 图片型 PDF 的前 N 页 base64 data URL
}

/**
 * 从文档文件提取文本 — 后端统一解析（PDF / Word / Excel）
 * 返回 { content, images, is_image_pdf }；无文本且非图片型 PDF 时抛错。
 * signal 中止时 fetch 抛 AbortError（上传最多 10MB，取消要真的断流而不是跑完再丢弃）。
 */
export async function extractDocumentText(file: File, signal?: AbortSignal): Promise<DocumentExtractResult> {
  const formData = new FormData()
  formData.append('file', file)
  const token = localStorage.getItem('auth_token')
  const headers: Record<string, string> = {}
  if (token) headers['Authorization'] = `Bearer ${token}`

  const res = await fetch('/api/v1/workspace/doc-extract', {
    method: 'POST',
    headers,
    body: formData,
    signal,
  })
  if (!res.ok) {
    let detail = '文档解析失败'
    try {
      const err = await res.json()
      if (typeof err.detail === 'string') detail = err.detail
    } catch { /* 用默认 detail */ }
    throw new Error(detail)
  }
  const data = await res.json() as DocumentExtractResult
  if (!data.content && !data.is_image_pdf) {
    throw new Error('未提取到文本内容（可能是扫描件或空文档）')
  }
  return data
}
