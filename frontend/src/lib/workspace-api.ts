const API_BASE = '/api/v1'

/** 构建带 Authorization header 的请求配置 */
function buildHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = {
    ...extra,
  }
  const token = localStorage.getItem('auth_token')
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return headers
}

/** 工作区文件树节点 */
export interface FileTreeNode {
  name: string
  type: 'file' | 'directory'
  path?: string
  size?: number
  mime_type?: string
  created_at?: string
  // 最后修改时间（ISO）。后端 write_file/edit_file/rename_file 落库时更新；
  // 前端"最近编辑"分区靠 diff 连续两次树快照的 updated_at 判断"本轮对话改了哪些"。
  updated_at?: string
  children?: FileTreeNode[]
}

/** 工作区统计信息 */
export interface WorkspaceStats {
  file_count: number
  total_size: number
  max_files: number
  max_size: number
  remaining_files: number
  remaining_size: number
}

/** 上传结果 */
export interface UploadResult {
  uploaded: { path: string; size: number }[]
  errors: string[]
  count: number
}

/** 把非 2xx 响应转成带服务端 detail + 状态码的 Error
 *
 * 读接口原先只抛「获取文件树失败」这类笼统字符串，用户看不出到底是配额超限、
 * 路径非法还是会话过期。这里统一带上 detail 和 HTTP 状态码，认证类单独给可操作文案。
 */
async function toError(res: Response, fallback: string): Promise<Error> {
  if (res.status === 401 || res.status === 403) {
    return new Error(`${fallback}：登录状态已失效，请重新登录`)
  }
  let detail = ''
  try {
    const body = await res.json()
    if (typeof body?.detail === 'string') detail = body.detail
  } catch {
    // 响应体不是 JSON（反向代理错误页、502 等），只保留状态码
  }
  return new Error(`${fallback}${detail ? `：${detail}` : ''}（HTTP ${res.status}）`)
}

/** 获取工作区文件树 */
export async function fetchFileTree(): Promise<FileTreeNode> {
  const res = await fetch(`${API_BASE}/workspace/tree`, {
    headers: buildHeaders(),
  })
  if (!res.ok) throw await toError(res, '获取文件树失败')
  return res.json()
}

/** 获取工作区统计 */
export async function fetchWorkspaceStats(): Promise<WorkspaceStats> {
  const res = await fetch(`${API_BASE}/workspace/stats`, {
    headers: buildHeaders(),
  })
  if (!res.ok) throw await toError(res, '获取统计信息失败')
  return res.json()
}

/** 上传文件
 *
 * 当上传目录时，浏览器会给每个 File 对象附加 webkitRelativePath 属性
 * （如 "myFolder/src/index.ts"），但 FormData 上传后后端只能拿到 file.filename
 * （如 "index.ts"），丢失了路径信息。
 *
 * 解决方案：把每个文件的 webkitRelativePath 作为额外的 form 字段一起发送，
 * 后端优先使用 relative_paths 中的路径，回退到 file.filename。
 *
 * 保留完整的目录结构，包括用户选择的目录名本身。
 * 例如上传 myFolder 目录，里面有 src/index.ts，
 * 工作区中会显示为 myFolder/src/index.ts。
 */
export async function uploadFiles(
  files: File[],
  basePath: string = '',
): Promise<UploadResult> {
  const formData = new FormData()
  const relativePaths: string[] = []
  for (const file of files) {
    formData.append('files', file)
    // webkitRelativePath 在上传目录时有值（如 "myFolder/src/index.ts"），
    // 普通文件上传时为空字符串
    // 注意：webkitRelativePath 是非标准属性，某些浏览器可能需要通过 Object.getOwnPropertyDescriptor 获取
    const desc = Object.getOwnPropertyDescriptor(file, 'webkitRelativePath')
    const relPath = desc?.value || (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.webkitRelativePath || ''
    // 保留完整的相对路径（包括目录名）
    if (relPath) {
      relativePaths.push(relPath)
    } else {
      relativePaths.push('')
    }
  }
  // 将相对路径数组作为 JSON 字符串发送
  formData.append('relative_paths', JSON.stringify(relativePaths))
  if (basePath) {
    formData.append('base_path', basePath)
  }
  const res = await fetch(`${API_BASE}/workspace/files`, {
    method: 'POST',
    headers: buildHeaders(),
    body: formData,
  })
  if (!res.ok) throw await toError(res, '上传失败')
  return res.json()
}

/** 创建目录 */
export async function createDirectory(path: string): Promise<void> {
  const res = await fetch(`${API_BASE}/workspace/directory`, {
    method: 'POST',
    headers: buildHeaders({ 'Content-Type': 'application/json' }),
    body: JSON.stringify({ path }),
  })
  if (!res.ok) throw await toError(res, '创建目录失败')
}

/** 读取文件内容（预览用）
 *
 * maxBytes 由服务端截断：不传则用后端默认 1MB。返回的 truncated 是权威标志，
 * 前端不需要再把全文拉下来自己 slice。
 */
export async function readFile(
  path: string,
  maxBytes?: number,
): Promise<{ path: string; content: string; size: number; total_size: number; truncated: boolean; is_text: boolean }> {
  const params = new URLSearchParams({ path })
  if (maxBytes) params.set('max_bytes', String(maxBytes))
  const res = await fetch(`${API_BASE}/workspace/files?${params}`, {
    headers: buildHeaders(),
  })
  if (!res.ok) throw await toError(res, '读取文件失败')
  return res.json()
}

/** 读取 AI 生成的落盘文件（.dsh_generated/，无 DB 记录，直接读磁盘） */
export async function fetchGeneratedFile(path: string): Promise<{ path: string; content: string; size: number }> {
  const params = new URLSearchParams({ path })
  const res = await fetch(`${API_BASE}/workspace/generated?${params}`, {
    headers: buildHeaders(),
  })
  if (!res.ok) throw await toError(res, '获取生成文件失败')
  return res.json()
}

/** 删除文件/目录 */
export async function deleteFile(path: string): Promise<void> {
  const params = new URLSearchParams({ path })
  const res = await fetch(`${API_BASE}/workspace/files?${params}`, {
    method: 'DELETE',
    headers: buildHeaders(),
  })
  if (!res.ok) throw await toError(res, '删除失败')
}

/** 历史快照版本（后端编辑快照，write/edit 覆写前自动留存） */
export interface SnapshotVersion {
  steps: number   // 倒数第 N 版，1 = 最近一版（与 restore 的 steps 对齐）
  ts: string      // 落盘时间 ISO
  size: number    // 字节数
}

/** 列出某文件的历史快照（新版在前） */
export async function listSnapshots(path: string): Promise<SnapshotVersion[]> {
  const params = new URLSearchParams({ path })
  const res = await fetch(`${API_BASE}/workspace/snapshots?${params}`, {
    headers: buildHeaders(),
  })
  if (!res.ok) throw await toError(res, '获取历史版本失败')
  const data = await res.json()
  return data.versions ?? []
}

/** 把文件回退到倒数第 steps 版（1=最近一版）；回退不改写历史 */
export async function restoreSnapshot(path: string, steps: number): Promise<{ ts: string; size: number }> {
  const params = new URLSearchParams({ path, steps: String(steps) })
  const res = await fetch(`${API_BASE}/workspace/restore?${params}`, {
    method: 'POST',
    headers: buildHeaders(),
  })
  if (!res.ok) throw await toError(res, '回退失败')
  return res.json()
}

/** 打包下载整个工作区为 ZIP */
export async function downloadWorkspace(): Promise<void> {
  const res = await fetch(`${API_BASE}/workspace/download`, {
    headers: buildHeaders(),
  })
  if (!res.ok) throw await toError(res, '下载失败')

  // 从 Content-Disposition 提取文件名
  const cd = res.headers.get('Content-Disposition') || ''
  const match = cd.match(/filename="?([^"]+)"?/)
  const filename = match?.[1] || 'workspace.zip'

  // 将响应体转为 Blob 并触发浏览器下载
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

// ════════════════════════════════════════
//  聊天图片存档（优化9）
// ════════════════════════════════════════

/** 存档结果：errors[].index 对齐请求里 items 的下标 */
export interface ChatArchiveResult {
  archived: { path: string }[]
  errors: { index: number; detail: string }[]
  count: number
}

/**
 * 把聊天里选中的图片批量存档到工作区（`聊天图片/`），供刷新后还原缩略图、
 * 以及 Agent 用 read_file 回看用户发过的图（优化14）。
 *
 * 走独立端点而不是 uploadFiles：后者在写入前会触发工作区惰性清理，
 * 用户在聊天里贴一张图就可能清掉他几小时前上传的工作文件；而且它与
 * 文档解析共用一个 10 次/分钟的限流桶，逐图上传几条消息就会 429。
 *
 * 路径由调用方内容寻址算好（buildChatArchiveRef），后端会重算 sha256 校验，
 * 所以**一批只发一次请求**：既省限流 token，也避免逐图触发面板刷新风暴。
 */
export async function archiveChatImages(
  items: { blob: Blob; path: string }[],
  signal?: AbortSignal,
): Promise<ChatArchiveResult> {
  const formData = new FormData()
  const paths: string[] = []
  for (const item of items) {
    // Blob → File：FormData 直接塞 Blob 时 filename 为空，后端 UploadFile.filename
    // 会拿到 ''；这里补上 basename（真实路径以 paths 字段为准，filename 只是兜底）
    const filename = item.path.split('/').pop() || 'image'
    formData.append('files', new File([item.blob], filename, { type: item.blob.type }))
    paths.push(item.path)
  }
  formData.append('paths', JSON.stringify(paths))
  const res = await fetch(`${API_BASE}/workspace/chat-archive`, {
    method: 'POST',
    headers: buildHeaders(),
    body: formData,
    signal,
  })
  if (!res.ok) throw await toError(res, '图片存档失败')
  return res.json()
}

/**
 * 取回工作区图片的原始字节并转成 data URL。
 *
 * 必须程序化 fetch 而不能把端点 URL 直接当 `<img src>`：该端点要求
 * Authorization 头，而 img 请求发不出 Bearer。
 *
 * 返回 data URL 而非 blob URL：缩略图会被多个组件实例共享，blob URL 无法
 * 安全 revoke（一个卡片卸载就弄坏其它卡片），data URL 由 GC 自动回收。
 */
export async function fetchWorkspaceRawDataUrl(path: string, signal?: AbortSignal): Promise<string> {
  const res = await fetch(`${API_BASE}/workspace/raw?${new URLSearchParams({ path })}`, {
    headers: buildHeaders(),
    signal,
  })
  if (!res.ok) throw await toError(res, '获取图片失败')
  // 后端按魔数嗅探真实格式（bmp/ico 会被前端转码，扩展名可能与字节不符）
  const mime = res.headers.get('Content-Type')?.split(';')[0].trim() || 'image/png'
  const bytes = new Uint8Array(await res.arrayBuffer())
  // 分块展开：4MB 图一次性 String.fromCharCode(...bytes) 会超出实参上限
  let binary = ''
  const CHUNK = 0x8000
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK))
  }
  return `data:${mime};base64,${btoa(binary)}`
}
