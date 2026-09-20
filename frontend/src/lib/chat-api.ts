const API_BASE = '/api/v1'

/** 构建带 Authorization header 的请求配置 */
function buildHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...extra,
  }
  const token = localStorage.getItem('auth_token')
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }
  return headers
}

export interface ChatResponse {
  response: string
  session_id: string
  finish_reason: string | null
  context_used_tokens: number | null
  context_window: number | null
  system_tokens: number | null
  tools_tokens: number | null
  message_tokens: number | null
  // ══ DSH 风格扩展字段 ══
  pressure_tokens?: number | null       // Provider 报告的最近一次请求的 prompt 侧 token
  projected_tokens?: number | null      // 下一次请求的预估 prompt token
  history_selected?: number             // 智能选取的历史消息数
  history_total?: number                // 前端传入的历史消息总数
  history_dropped?: number              // 被丢弃的历史消息数
  pressure_percent?: number             // 上下文压力百分比
}

/** DSH 风格压缩响应 */
export interface CompactResponse {
  session_id: string
  compacted: boolean
  response: string
  summary_text?: string               // 7 维度摘要纯文本
  shadowed_count?: number             // 被压缩的消息数
  shadowed_tokens?: number            // 被压缩的 token 数
  summary_tokens?: number             // 摘要的 token 数
  saved_tokens?: number               // 节省的 token 数
  context_used_tokens: number | null
  context_window: number | null
  system_tokens: number
  tools_tokens: number
  message_tokens: number
  pressure_percent?: number
  error?: string
}

export interface ChatModel {
  id: number
  provider_id: number
  provider_name: string
  name: string
  display_name: string
  is_active: boolean
  is_default: boolean
  supports_vision: boolean
  context_length: number
  created_at: string
}

export interface ChatQuotaItem {
  daily_limit: number
  used: number
  remaining: number
}

export interface ChatQuotaResponse {
  image: ChatQuotaItem
}

export interface ChatSession {
  id: number
  session_id: string
  visitor_id: string
  title: string
  model_id: number | null
  is_active: boolean
  created_at: string
  updated_at: string
}

/** DSH 对话 idle 超时 — 60 秒内无任何数据（chunk/heartbeat）则断开 */
const CHAT_IDLE_TIMEOUT_MS = 60_000

/** 流式对话"业务活动空闲"硬上限（S4）— 只有收到真实业务数据
 *  （chunk/file/工具/reasoning）才重置计时；后端每 15s 的心跳不重置。
 *  模型持续输出推理/正文时永不误断；真正卡死（连 reasoning 都没有、
 *  纯无输出）超过上限才中断。
 *  20 分钟：Agent 长任务（多图 PPT 生成等）在后台可跑更久；
 *  后端任务总时限 20 分钟（方案 B），前端与之对齐。 */
const CHAT_STALL_TIMEOUT_MS = 1_200_000

/** 非流式对话超时（3 分钟，压缩接口使用） */
const CHAT_TIMEOUT_MS = 180_000

/** 工作区内容自动注入档位（对应界面上的「自动带入文件内容」开关）
 *  - off  不注入：模型看不到任何工作区信息，需自己 list_files / read_file
 *  - tree 仅目录：只注入文件树（路径+大小），内容按需读取，成本仅数百字节
 *  - full 全文：文件树 + 预算内的文件内容（最多 64KB）
 *  三档都不影响工具能力——能否读写工作区文件由 Agent 模式（enable_tools）决定。 */
export type WorkspaceInjectMode = 'off' | 'tree' | 'full'

/** 流式发送对话消息 — 通过 SSE 逐块接收 AI 回复
 *
 *  可选传入 externalSignal，用于外部中断（如用户点击停止按钮）。
 *  中断时不抛异常，正常返回（让调用方保留已收到的内容）。
 */
export async function streamChatMessage(
  message: string,
  sessionId: string | undefined,
  modelId: number | undefined,
  onChunk: (text: string) => void,
  onDone?: (res: ChatResponse) => void,
  onError?: (err: string) => void,
  history?: { role: string; content: string }[],
  onFile?: (file: { name: string; content: string; mime?: string | null; generatedPath?: string; size?: number }) => void,
  workspaceContext?: boolean,
  workspaceContextMode?: WorkspaceInjectMode,   // 三档注入：off / tree(仅目录) / full(全文)；缺省时由 workspaceContext 布尔值推导
  onToolCall?: (tool: string, args: Record<string, unknown>, id?: string, round?: number, maxRounds?: number) => void,
  onToolResult?: (tool: string, result: string, id?: string) => void,
  externalSignal?: AbortSignal,
  enableTools?: boolean,
  loadedSkills?: string[],
  skillsFirstRound?: boolean,   // 技能全文分级注入：首轮 true 注入完整指令，后续只注入摘要
  onToolProgress?: (text: string, id?: string) => void,
  onReasoning?: (text: string) => void,
  onTask?: (taskId: string) => void,
  onModelSwitch?: (name: string, status: number) => void,
): Promise<void> {
  const controller = new AbortController()
  // S4：业务活动空闲硬上限 — 心跳会重置 idle 计时，但只有真实业务数据才重置本计时
  let stallTimedOut = false
  let activityTimer: ReturnType<typeof setTimeout> | null = null
  const resetActivityTimer = () => {
    if (activityTimer) clearTimeout(activityTimer)
    activityTimer = setTimeout(() => {
      stallTimedOut = true
      controller.abort()
    }, CHAT_STALL_TIMEOUT_MS)
  }
  resetActivityTimer()
  // idle timeout：每次收到数据重置，只有持续无数据才超时
  let idleTimer: ReturnType<typeof setTimeout> | null = null
  const resetIdleTimer = () => {
    if (idleTimer) clearTimeout(idleTimer)
    idleTimer = setTimeout(() => controller.abort(), CHAT_IDLE_TIMEOUT_MS)
  }
  resetIdleTimer()

  // 如果有外部信号，转发中断
  if (externalSignal) {
    externalSignal.addEventListener('abort', () => controller.abort(), { once: true })
  }
  // 追踪是否为用户主动中断
  let userAborted = false
  if (externalSignal) {
    userAborted = externalSignal.aborted
    externalSignal.addEventListener('abort', () => { userAborted = true }, { once: true })
  }

  // 三档注入模式：新参数优先；旧调用方只给布尔值时 True→full、False→off
  const injectMode: WorkspaceInjectMode = workspaceContextMode ?? (workspaceContext ? 'full' : 'off')

  let res: Response
  try {
    res = await fetch(`${API_BASE}/ai/chat/stream`, {
      method: 'POST',
      headers: buildHeaders(),
      body: JSON.stringify({
        message,
        session_id: sessionId ?? null,
        model_id: modelId ?? null,
        history: history ?? [],
        // workspace_context 布尔字段保留，兼容未升级的后端；三档以 mode 字段为准
        workspace_context: injectMode === 'full',
        workspace_context_mode: injectMode,
        enable_tools: enableTools ?? false,
        loaded_skills: loadedSkills ?? [],
        skills_first_round: skillsFirstRound ?? true,
      }),
      signal: controller.signal,
    })
  } catch (err) {
    if (idleTimer) clearTimeout(idleTimer)
    // 用户主动中断：不抛异常，正常返回
    if (userAborted || (err instanceof DOMException && err.name === 'AbortError' && externalSignal?.aborted)) {
      return
    }
    if (err instanceof DOMException && err.name === 'AbortError') {
      // S4：区分"业务无输出卡死"与"网络空闲"，给出可操作的提示
      if (stallTimedOut) {
        throw new Error('模型超过 20 分钟未产生任何输出，已自动中断；可降低推理等级或稍后重试')
      }
      throw new Error('对话超时，请稍后重试或降低思考深度')
    }
    throw err
  }

  if (!res.ok) {
    if (idleTimer) clearTimeout(idleTimer)
    let detail = `HTTP ${res.status}`
    try {
      const err = await res.json()
      if (typeof err.detail === 'string') detail = err.detail
    } catch { /* */ }
    throw new Error(detail)
  }

  // 读取 SSE 流
  const reader = res.body?.getReader()
  if (!reader) {
    if (idleTimer) clearTimeout(idleTimer)
    throw new Error('浏览器不支持流式读取')
  }

  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      let readResult: { done: boolean; value: Uint8Array | undefined }
      try {
        readResult = await reader.read()
      } catch {
        // reader.read() 在 abort 时会抛 TypeError: BodyStreamBuffer was aborted
        // 用户主动中断 — 直接退出循环，不抛异常
        if (userAborted) break
        // S4：业务无输出卡死（心跳仍在发，但 5 分钟无任何业务数据）— 给出可操作提示
        if (stallTimedOut) {
          throw new Error('模型超过 20 分钟未产生任何输出，已自动中断；可降低推理等级或稍后重试')
        }
        throw new Error('流式连接中断')
      }
      const { done, value } = readResult
      if (done) break

      // 收到数据，重置 idle timer
      resetIdleTimer()

      // 检查用户是否主动中断
      if (userAborted) {
        // 取消 reader，退出循环（不抛异常）
        break
      }

      buffer += decoder.decode(value, { stream: true })

      // 按行解析 SSE
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''

      for (const line of lines) {
        const trimmed = line.trim()
        if (!trimmed || trimmed.startsWith(':')) continue // 空行或心跳注释
        if (!trimmed.startsWith('data:')) continue

        const jsonStr = trimmed.slice(5).trim()
        if (!jsonStr) continue

        // 只 try JSON.parse——事件处理（含 onError 的 throw）必须在 try 外，
        // 否则 onError 抛出的错误会被 catch 吞掉，导致错误静默丢失
        let evt: any
        try {
          evt = JSON.parse(jsonStr)
        } catch {
          continue // JSON 解析失败，跳过
        }

        // S4：任何有效业务事件（chunk/file/工具/reasoning/done/error）都视为活动，
        // 重置卡死计时；心跳（": heartbeat" 注释行）在上方被跳过，不重置
        resetActivityTimer()

        if (evt.type === 'chunk' && typeof evt.text === 'string') {
          onChunk(evt.text)
        } else if (evt.type === 'task' && typeof evt.task_id === 'string') {
          // 方案 B：任务 ID（刷新/断线后凭此查询后台任务进度与结果）
          onTask?.(evt.task_id)
        } else if (evt.type === 'file' && evt.name) {
          // AI 输出的文件块
          onFile?.({
            name: evt.name,
            content: evt.content ?? '',
            mime: evt.mime ?? null,
            generatedPath: evt.generated_path ?? undefined,
            size: evt.size ?? undefined,
          })
        } else if (evt.type === 'tool_call' && evt.tool) {
          // AI 正在调用工具（id = tool_call_id，前端按 id 匹配结果/实时输出）
          // B2：round/max_rounds 为轮次信息（后端 SSE 为下划线命名，前端显示"第 N/M 轮"）
          onToolCall?.(evt.tool, evt.arguments ?? {}, evt.id, evt.round, evt.max_rounds)
        } else if (evt.type === 'tool_result' && evt.tool) {
          // 工具执行结果
          onToolResult?.(evt.tool, evt.result ?? '', evt.id)
        } else if (evt.type === 'tool_progress' && typeof evt.text === 'string') {
          // 工具实时输出流（run_python stdout，按 id 归属到对应工具卡片）
          onToolProgress?.(evt.text, evt.id)
        } else if (evt.type === 'reasoning' && typeof evt.text === 'string') {
          // 推理内容流
          onReasoning?.(evt.text)
        } else if (evt.type === 'model_switch' && typeof evt.name === 'string') {
          // 模型自动降级（401/402/403/429 等）— 通知上层同步模型栏
          onModelSwitch?.(evt.name, typeof evt.status === 'number' ? evt.status : 0)
        } else if (evt.type === 'done') {
          onDone?.(evt as ChatResponse)
        } else if (evt.type === 'error') {
          onError?.(evt.error ?? '对话失败')
        }
      }
    }
  } finally {
    if (activityTimer) clearTimeout(activityTimer)
    if (idleTimer) clearTimeout(idleTimer)
    // 尝试取消 reader（安全清理）
    if (userAborted) {
      try { reader.cancel() } catch { /* 静默 */ }
    }
  }
}

/** 查询对话配额 */
export async function fetchChatQuota(): Promise<ChatQuotaResponse> {
  const res = await fetch(`${API_BASE}/ai/chat/quota`, { headers: buildHeaders() })
  if (!res.ok) throw new Error('获取配额失败')
  return res.json()
}

/** 获取可用对话模型列表 */
export async function fetchChatModels(): Promise<ChatModel[]> {
  const res = await fetch(`${API_BASE}/ai/chat/models`, { headers: buildHeaders() })
  if (!res.ok) throw new Error('获取模型列表失败')
  return res.json()
}

/** 技能摘要（供前端展示 / 手动加载） */
export interface ChatSkillInfo {
  name: string
  description: string
  has_resources: boolean   // 目录形式技能（含参考文件）
  // 以下三项后端较新才返回；前端热更新早于后端重启时可能缺失，UI 按缺省值处理
  category?: string        // 分类标签（空/缺省 = 未分类），技能面板下拉筛选用
  pack?: string            // 所属技能包（空/缺省 = 独立技能）
  content_chars?: number   // SKILL.md 正文字符数，用于体积提示与预算占用条
}

/** 获取可用技能列表（仅活跃技能的摘要） */
export async function fetchChatSkills(): Promise<ChatSkillInfo[]> {
  const res = await fetch(`${API_BASE}/ai/chat/skills`, { headers: buildHeaders() })
  if (!res.ok) throw new Error('获取技能列表失败')
  return res.json()
}

// ════════════════════════════════════════
//  会话窗口 API
// ════════════════════════════════════════

/** 获取当前访客的所有会话窗口 */
export async function fetchChatSessions(): Promise<ChatSession[]> {
  const res = await fetch(`${API_BASE}/ai/chat/sessions`, { headers: buildHeaders() })
  if (!res.ok) throw new Error('获取会话列表失败')
  return res.json()
}

/** 创建新会话窗口 */
export async function createChatSession(
  title?: string,
  modelId?: number,
): Promise<ChatSession> {
  const res = await fetch(`${API_BASE}/ai/chat/sessions`, {
    method: 'POST',
    headers: buildHeaders(),
    body: JSON.stringify({ title: title ?? null, model_id: modelId ?? null }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: '创建会话失败' }))
    throw new Error(typeof err.detail === 'string' ? err.detail : '创建会话失败')
  }
  return res.json()
}

/** 更新会话窗口（重命名或关闭） */
export async function updateChatSession(
  sessionPk: number,
  updates: { title?: string; is_active?: boolean },
): Promise<ChatSession> {
  const res = await fetch(`${API_BASE}/ai/chat/sessions/${sessionPk}`, {
    method: 'PUT',
    headers: buildHeaders(),
    body: JSON.stringify(updates),
  })
  if (!res.ok) throw new Error('更新会话失败')
  return res.json()
}

/** 删除会话窗口 */
export async function deleteChatSession(sessionPk: number): Promise<void> {
  const res = await fetch(`${API_BASE}/ai/chat/sessions/${sessionPk}`, {
    method: 'DELETE',
    headers: buildHeaders(),
  })
  if (!res.ok) throw new Error('删除会话失败')
}

/** 手动触发 DSH 风格上下文压缩 — 7 维度结构化摘要 */
export async function compactChatContext(
  sessionId: string,
  modelId?: number,
  history?: { role: string; content: string }[],
): Promise<CompactResponse> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), CHAT_TIMEOUT_MS)
  let res: Response
  try {
    res = await fetch(`${API_BASE}/ai/chat/compact`, {
      method: 'POST',
      headers: buildHeaders(),
      body: JSON.stringify({
        session_id: sessionId,
        model_id: modelId ?? null,
        history: history ?? [],
      }),
      signal: controller.signal,
    })
  } catch (err) {
    clearTimeout(timer)
    if (err instanceof DOMException && err.name === 'AbortError') {
      throw new Error('压缩超时，请稍后重试')
    }
    throw err
  }
  clearTimeout(timer)
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const err = await res.json()
      if (typeof err.detail === 'string') detail = err.detail
    } catch { /* 用默认 detail */ }
    throw new Error(detail)
  }
  return res.json()
}

/** 对话配置（公开接口返回的字段） */
export interface ChatConfig {
  max_files_per_message: number
}

/** 获取对话配置（公开接口） */
export async function fetchChatConfig(): Promise<ChatConfig> {
  const res = await fetch(`${API_BASE}/ai/chat/config`)
  if (!res.ok) throw new Error('获取对话配置失败')
  return res.json()
}

/** 获取会话最近一次 AI 回复的推理过程（刷新后从后端兜底恢复） */
export async function fetchSessionReasoning(sessionId: string): Promise<string> {
  const res = await fetch(`${API_BASE}/ai/chat/sessions/${sessionId}/reasoning`, {
    headers: buildHeaders(),
  })
  if (!res.ok) return '' // 静默失败，不影响用户使用
  const data = await res.json()
  return (data?.reasoning as string) ?? ''
}

// ═══ Agent 长任务（方案 B）：后台执行，刷新/断线可查 ═══

/** 后台任务持久化的工具调用事件（AgentTask.tool_events，刷新恢复工具卡片用） */
export interface AgentTaskEvent {
  id?: string
  tool: string
  arguments?: Record<string, unknown>
  result?: string
  round?: number
  max_rounds?: number
}

export interface AgentTaskInfo {
  task_id: string
  status: string // running / done / failed / cancelled
  result: string
  tool_events?: AgentTaskEvent[] // 工具调用痕迹（旧任务记录可能缺失该字段）
  error: string
  created_at: string
  finished_at: string
}

/** 查询后台任务状态与结果（刷新页面后恢复进度） */
export async function fetchAgentTask(taskId: string): Promise<AgentTaskInfo | null> {
  try {
    const res = await fetch(`${API_BASE}/ai/tasks/${encodeURIComponent(taskId)}`, {
      headers: buildHeaders(),
    })
    if (!res.ok) return null
    return await res.json()
  } catch {
    return null
  }
}

/** 取消后台任务（用户点击停止按钮时调用；SSE abort 后任务仍会后台继续） */
export async function cancelAgentTask(taskId: string): Promise<void> {
  try {
    await fetch(`${API_BASE}/ai/tasks/${encodeURIComponent(taskId)}/cancel`, {
      method: 'POST',
      headers: buildHeaders(),
    })
  } catch { /* 静默失败 */ }
}

// ═══ 用户长期记忆管理（原 ai-api.ts，生图模块删除后迁入） ═══

export interface MemoryItem {
  id: number
  memory_type: string  // fact | preference | context
  content: string
  source: string
  access_count: number
  created_at: string | null
  last_used_at: string | null
  has_embedding: boolean
}

/** 列出当前用户所有记忆 */
export async function listMemories(): Promise<MemoryItem[]> {
  const res = await fetch(`${API_BASE}/ai/memories`, { headers: buildHeaders() })
  if (!res.ok) throw new Error('获取记忆失败')
  const data = await res.json()
  return data.memories ?? []
}

/** 删除一条记忆 */
export async function deleteMemory(memoryId: number): Promise<void> {
  const res = await fetch(`${API_BASE}/ai/memories/${memoryId}`, {
    method: 'DELETE',
    headers: buildHeaders(),
  })
  if (!res.ok) throw new Error('删除失败')
}

/** 清空所有记忆 */
export async function clearAllMemories(): Promise<number> {
  const res = await fetch(`${API_BASE}/ai/memories`, {
    method: 'DELETE',
    headers: buildHeaders(),
  })
  if (!res.ok) throw new Error('清空失败')
  const data = await res.json()
  return data.deleted ?? 0
}
