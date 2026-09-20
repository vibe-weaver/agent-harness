import { useState, useCallback, useRef, useEffect, useMemo } from 'react'
import { useAuth } from './useAuth'
import {
  streamChatMessage,
  fetchChatQuota,
  fetchChatModels,
  fetchChatSessions,
  createChatSession,
  updateChatSession,
  deleteChatSession,
  compactChatContext,
  fetchSessionReasoning,
  fetchAgentTask,
  cancelAgentTask,
  type AgentTaskEvent,
  type ChatResponse,
  type ChatQuotaResponse,
  type ChatModel,
  type ChatSession,
  type CompactResponse,
  type WorkspaceInjectMode,
} from '../lib/chat-api'

export interface ChatAttachment {
  name: string
  size: number
  dataUrl?: string  // 图片附件 base64（仅会话内展示缩略图，不持久化）
  ref?: string      // 图片的工作区存档相对路径（聊天图片/<hash>_名.png）。仅渲染层用于刷新后
                    // 取回缩略图，绝不写进 rawContent/prompt —— 载荷侧不变量见下方 slimHistoryAttachments 注释
}

/** AI 回复中输出的可下载文件 */
export interface ChatFile {
  name: string
  content: string
  mime?: string | null
  generatedPath?: string   // 大文件落盘到工作区 .dsh_generated/，下载时按路径获取（content 为空）
  size?: number            // 原始文件字节数（落盘时由后端标注）
}

/** Agent 工具调用记录 */
export interface ToolInvocation {
  id?: string            // 后端下发的 tool_call_id，结果/实时输出按 id 匹配
  tool: string
  arguments?: Record<string, unknown>
  result?: string
  progress?: string      // 该工具的实时输出流（run_python stdout，不持久化）
  round?: number         // B2：该工具调用发生在第几轮（1 起）
  maxRounds?: number     // B2：当前任务的总轮数上限（管理端配置）
}

export interface ChatMessage {
  id?: string            // 前端稳定 id（列表 key 用，避免 index 位移导致状态丢失）
  role: 'user' | 'assistant'
  content: string          // 前端显示用的文本（用户消息为 displayText，不含文件内容）
  rawContent?: string       // 发给后端的完整消息（含文件内容），用于构建 history
  session_id?: string
  attachments?: ChatAttachment[]
  files?: ChatFile[]        // AI 输出的可下载文件列表
  tools?: ToolInvocation[]  // Agent 工具调用记录
  progress?: string          // 旧版工具实时输出（兼容历史数据；新版本按工具 id 归属到 tools[].progress）
  reasoning?: string         // 推理过程（reasoning_content，持久化到 localStorage + 后端）
  compressedSummary?: string // 上下文压缩摘要（checkpoint 消息可展开查看）
  mode?: 'chat' | 'agent'    // 发送该消息时的模式（消息流模式标记用）
  resumedTaskId?: string     // 刷新后由后台任务补全的消息标记（幂等去重用，不显示在正文）
}

/** 当前回合的 Agent 阶段（S1 阶段感知状态机） */
export type AgentPhase =
  | 'idle'        // 无进行中的回合
  | 'waiting'     // 已发送，等待模型首次输出
  | 'thinking'    // 模型深度思考中（收到 reasoning 流）
  | 'tool_call'   // AI 正在调用工具（已发出 tool_call）
  | 'tool_exec'   // 工具执行中（收到 tool_progress 实时输出）
  | 'answering'   // 生成回答中（收到正文 chunk）

/** 生成消息稳定 id（时间戳 + 随机后缀，够用即可） */
let _msgIdSeq = 0
function genMsgId(): string {
  _msgIdSeq = (_msgIdSeq + 1) % 10000
  return `${Date.now().toString(36)}-${_msgIdSeq.toString(36)}-${Math.random().toString(36).slice(2, 6)}`
}

/** 智能会话标题：去掉附件 XML 块，取第一行有效文本，40 字符截断 */
function smartTitle(text: string): string {
  const clean = text
    .replace(/<attachments>[\s\S]*?<\/attachments>/g, '')
    .replace(/<file\s[^>]*>[\s\S]*?<\/file>/g, '')
    .replace(/<image\s[^>]*>[\s\S]*?<\/image>/g, '')
    .trim()
  const firstLine = clean.split('\n').map(l => l.trim()).find(Boolean) ?? ''
  if (!firstLine) return '新对话'
  return firstLine.length > 40 ? firstLine.slice(0, 40) + '…' : firstLine
}

// ── 历史附件瘦身 ──
// 附件全量内容只在发送当轮传给后端（send 的 text 参数）；历史轮次每轮重传几百 KB
// 的文件/图片内容纯属浪费——后端 _sanitize_history 本来就会把单条消息截断到 4000
// 字符（大文件被腰斩、图片 base64 截断后永远无法还原成图）。
// 策略：小文件（≤3500 字符）原样保留（模型可见内容不变）；大文件保留开头并标注
// 截断；图片整块替换为占位符。请求体从几百 KB 降到几 KB。
// 注意：「历史图片一律变占位符」还是一条载荷性不变量——它是含图会话可以安全切换到
// 非 vision 模型的前提（后端永远收不到历史里的 <image> 块）。若改成保留历史图片，
// 模型选择器就必须重新加回 vision 门禁。
// 图片存档（attachments[].ref，用于刷新后还原缩略图）只挂在渲染层、不进 rawContent，
// 所以这条不变量在存档上线后依然成立，门禁不需要加回来。
const HISTORY_FILE_KEEP_CHARS = 3500

function slimHistoryAttachments(content: string): string {
  if (!content.includes('<file') && !content.includes('<image')) return content
  return content
    .replace(
      /(<file\s+name="([^"]*)"[^>]*>)([\s\S]*?)<\/file>/g,
      (match: string, _open: string, _name: string, body: string) =>
        body.length <= HISTORY_FILE_KEEP_CHARS
          ? match
          : `${_open}${body.slice(0, HISTORY_FILE_KEEP_CHARS)}\n[...附件过长，历史中仅保留开头；完整内容已在发送当轮提供...]`,
    )
    .replace(
      /<image\s+name="([^"]*)"[^>]*>[\s\S]*?<\/image>/g,
      (_match: string, name: string) => `[图片附件 ${name}（内容已省略，仅发送当轮提供给模型）]`,
    )
}

export interface ContextPressure {
  percent: number
  usedTokens: number
  contextWindow: number
  systemTokens: number
  toolsTokens: number
  messageTokens: number
  // ══ DSH 风格扩展 ══
  pressureTokens?: number | null     // Provider 报告的 prompt 侧 token
  projectedTokens?: number | null    // 下一次请求的预估 prompt token
  historySelected?: number           // 智能选取的历史消息数
  historyTotal?: number              // 历史消息总数
  historyDropped?: number            // 被丢弃的历史消息数
}

// ════════════════════════════════════════
//  localStorage 持久化
// ════════════════════════════════════════
const STORAGE_KEY = 'chat_messages_v1'          // 旧版单 key（所有会话挤一个 value；已废弃，仅迁移时读取）
const SHARD_KEY_BASE = 'chat_shard_v1'          // 分片存储：每个会话独立一个 key
const CONTEXT_STORAGE_KEY = 'chat_context_v1'
const MODEL_STORAGE_KEY = 'chat_selected_model_v1'
const LOADED_SKILLS_STORAGE_KEY = 'chat_loaded_skills_v1'
const MODE_STORAGE_KEY = 'chat_mode_v1'
const ACTIVE_SESSION_STORAGE_KEY = 'chat_active_session_v1'
const WORKSPACE_INJECT_KEY = 'chat_workspace_inject_v1'
const STORAGE_LIMIT = 200 // 每会话最多存 200 条消息

// ── 工作区内容自动注入档位（三态）持久化 ──
// 沿用旧的 chat_workspace_inject_v1 key，存储值向后兼容布尔写法：
// null/'1' → full（旧的"开"），'0' → off（旧的"关"），'tree' → 仅目录（新增档）
function readInjectMode(): WorkspaceInjectMode {
  try {
    const raw = localStorage.getItem(storageKey(WORKSPACE_INJECT_KEY))
    if (raw === 'tree') return 'tree'
    if (raw === '0') return 'off'
    return 'full'
  } catch {
    return 'full'
  }
}

function persistInjectMode(v: WorkspaceInjectMode): void {
  try {
    localStorage.setItem(storageKey(WORKSPACE_INJECT_KEY), v === 'tree' ? 'tree' : v === 'off' ? '0' : '1')
  } catch {
    // 静默：localStorage 不可写时仅本次会话生效
  }
}

// ── 分片存储 + 脏跟踪 ──
// 旧版把所有会话挤在一个 key 里，每次保存都全量 JSON.stringify 整个 blob，
// 长会话（多会话 × 工具大输出）时数 MB 同步序列化反复卡主线程。
// 现在每个会话一个 key，保存时只序列化数组引用真正变化的会话——
// React 状态更新全部不可变（{...prev, [sid]: [...msgs]}），未变会话的
// 数组引用保持不变，引用比较即可跳过。
// sid → 上次已写入 localStorage 的数组引用（脏跟踪基线）
const lastSavedSessions = new Map<string, ChatMessage[]>()

function shardKey(sid: string): string {
  return `${storageKey(SHARD_KEY_BASE)}:${sid}`
}

// ── 存储域（按用户隔离）────────────────────────────
// 所有持久化 key 增加用户维度，防止同一浏览器上不同用户的消息/模型/技能互相串用或泄漏。
// 未登录（user 为 null）使用 anon 域；登录后使用 `key:{userId}` 域。
// 首次进入新域时，把升级前的全局老 key（无后缀）数据一次性迁移进当前域并删除老 key。
let chatStorageUserId: string | null = null

/** 当前存储域对应的完整 key */
function storageKey(base: string): string {
  return chatStorageUserId ? `${base}:${chatStorageUserId}` : `${base}:anon`
}

/** 切换存储域（登录用户变化时调用），并迁移升级前的全局老 key 数据 */
function setChatStorageUser(userId: string | null): void {
  const next = userId || null
  if (next === chatStorageUserId) return
  chatStorageUserId = next
  // 切换存储域：脏跟踪基线全部作废（loadMessagesFromStorage 会按新域重建）
  lastSavedSessions.clear()
  // 只在进入用户域时迁移升级前的全局老 key：
  // 页面刷新瞬间 user 尚未加载（处于 anon 域），此时迁移会把老数据错归到 anon 域
  // （消息数据的全局老 key 不在此处迁移，由 loadMessagesFromStorage 随分片迁移一并处理）
  if (!next) return
  for (const base of [CONTEXT_STORAGE_KEY, MODEL_STORAGE_KEY, LOADED_SKILLS_STORAGE_KEY, MODE_STORAGE_KEY]) {
    try {
      const legacy = localStorage.getItem(base)
      if (legacy === null) continue
      const target = storageKey(base)
      if (localStorage.getItem(target) !== null) continue
      localStorage.setItem(target, legacy)
      localStorage.removeItem(base)
    } catch {
      // 静默：迁移失败不阻塞
    }
  }
}

// ── Agent 后台任务持久化（方案 B）──
// 发送时把 task_id 存 localStorage（按会话），刷新后凭此恢复任务状态提示
const AGENT_TASK_KEY_PREFIX = 'agent_task_'

function setAgentTaskStored(sid: string, taskId: string | null): void {
  try {
    const key = storageKey(`${AGENT_TASK_KEY_PREFIX}${sid}`)
    if (taskId) localStorage.setItem(key, taskId)
    else localStorage.removeItem(key)
  } catch { /* 忽略 */ }
}

function loadStoredAgentTasks(): { sid: string; taskId: string }[] {
  const out: { sid: string; taskId: string }[] = []
  try {
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (!key || !key.startsWith(AGENT_TASK_KEY_PREFIX)) continue
      const taskId = localStorage.getItem(key)
      if (!taskId) continue
      // key 格式: agent_task_{sid}:{domain}
      const rest = key.slice(AGENT_TASK_KEY_PREFIX.length)
      const colon = rest.indexOf(':')
      out.push({ sid: colon >= 0 ? rest.slice(0, colon) : rest, taskId })
    }
  } catch { /* 忽略 */ }
  return out
}

/** 解析旧版单 key 的消息 blob（所有会话挤一个 value），失败返回 null */
function parseMessagesBlob(raw: string): Record<string, ChatMessage[]> | null {
  try {
    const parsed = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return null
    const out: Record<string, ChatMessage[]> = {}
    for (const [sid, msgs] of Object.entries(parsed)) {
      if (Array.isArray(msgs)) out[sid] = msgs
    }
    return out
  } catch {
    return null
  }
}

/** 从 localStorage 读取所有会话消息（分片格式；旧版单 key 自动迁移） */
function loadMessagesFromStorage(): Record<string, ChatMessage[]> {
  try {
    const data: Record<string, ChatMessage[]> = {}
    // 1) 读取当前域的全部分片（单片损坏只丢该会话，不拖垮其他）
    const prefix = `${storageKey(SHARD_KEY_BASE)}:`
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (!key || !key.startsWith(prefix)) continue
      const sid = key.slice(prefix.length)
      if (!sid) continue
      try {
        const parsed = JSON.parse(localStorage.getItem(key) || '[]')
        if (Array.isArray(parsed)) data[sid] = parsed
      } catch { /* 静默：跳过损坏分片 */ }
    }
    // 2) 旧版单 key → 分片迁移（一次性，quota 不足时保留老 key 下次重试）
    let legacyKey: string | null = null
    const legacySids: string[] = []
    const domainRaw = localStorage.getItem(storageKey(STORAGE_KEY))
    let legacyData: Record<string, ChatMessage[]> | null = null
    if (domainRaw !== null) {
      legacyKey = storageKey(STORAGE_KEY)
      legacyData = parseMessagesBlob(domainRaw)
    } else if (chatStorageUserId) {
      // 全局老 key（无存储域后缀）只在用户域认领——
      // anon 挂载时认领会把老数据错归到 anon 域（与 setChatStorageUser 迁移语义一致）
      const globalRaw = localStorage.getItem(STORAGE_KEY)
      if (globalRaw !== null) {
        legacyKey = STORAGE_KEY
        legacyData = parseMessagesBlob(globalRaw)
      }
    }
    if (legacyData) {
      for (const [sid, msgs] of Object.entries(legacyData)) {
        if (sid in data) continue // 已有分片的数据更新，分片优先
        data[sid] = msgs
        legacySids.push(sid)
      }
    }
    // 3) 逐消息迁移（旧数据补字段；先迁移再落盘，保证分片内容与内存一致）
    for (const msgs of Object.values(data)) {
      if (!Array.isArray(msgs)) continue
      for (const msg of msgs) {
        // 迁移：补稳定 id（旧数据无 id，避免列表 key 用 index）
        if (!msg.id) msg.id = genMsgId()
        // 迁移：剥离历史附件中的 dataUrl（不持久化，防止 localStorage 爆满）
        if (msg.attachments) {
          msg.attachments = msg.attachments.map(({ dataUrl: _d, ...rest }) => rest)
        }
      }
      // 清理末尾空的 assistant 占位消息（流式回复中断/刷新时残留）
      // 如果最后一条是 assistant 且 content 为空（或只有空白），删除它
      if (msgs.length > 0) {
        const last = msgs[msgs.length - 1]
        if (last && last.role === 'assistant' && (!last.content || !last.content.trim()) && !last.files?.length && !last.tools?.length) {
          msgs.length = msgs.length - 1
        }
      }
    }
    // 4) 把老 key 会话写入分片；全部成功才删老 key（quota 不足时下次加载重试）
    if (legacyKey && legacySids.length > 0) {
      let migrated = true
      for (const sid of legacySids) {
        try {
          // 走 buildTrimmedMessages 而不是裸 JSON.stringify：这条迁移路径否则会绕过
          // 唯一的瘦身闸门，老数据里含图片 base64 的 rawContent 会撞配额，
          // 导致 migrated 永远 false、每次加载都重试迁移。
          const trimmed = buildTrimmedMessages({ [sid]: data[sid] }, STORAGE_LIMIT)[sid] ?? []
          localStorage.setItem(shardKey(sid), JSON.stringify(trimmed))
        } catch {
          migrated = false // 该会话留在老 key 里，本次仍可见（上面已并入 data）
        }
      }
      if (migrated) {
        try { localStorage.removeItem(legacyKey) } catch { /* 静默 */ }
      }
    }
    // 5) 重建脏跟踪基线：加载得到的数据视为"已写入"状态
    lastSavedSessions.clear()
    for (const [sid, msgs] of Object.entries(data)) {
      if (msgs.length > 0) lastSavedSessions.set(sid, msgs)
    }
    return data
  } catch {
    return {}
  }
}

/** 构建可持久化的消息数据（剥离流式临时字段 + 裁剪条数）
 *  reasoning 现在保留持久化（方案一），仅剥离 progress 类临时字段 */
function buildTrimmedMessages(data: Record<string, ChatMessage[]>, limit: number): Record<string, ChatMessage[]> {
  const trimmed: Record<string, ChatMessage[]> = {}
  for (const [sid, msgs] of Object.entries(data)) {
    if (msgs && msgs.length > 0) {
      trimmed[sid] = msgs.slice(-limit).map(m => {
        let next = m
        // 剥离流式临时字段（仅 progress 类；reasoning 保留以便刷新后恢复思考过程）
        if (m.progress !== undefined) {
          const { progress, ...rest } = m
          next = rest as ChatMessage
        }
        // 剥离附件 dataUrl（大 base64）
        if (next.attachments && next.attachments.some(a => a.dataUrl !== undefined)) {
          next = { ...next, attachments: next.attachments.map(({ dataUrl: _d, ...rest }) => rest) }
        }
        // 瘦身 rawContent：它存的是发给后端的完整原文，含 <image>dataURL</image> 整块 base64。
        // buildHistory 每轮发送前本来就会过一遍 slimHistoryAttachments，所以持久化同样瘦身
        // 不改变任何后续请求的内容；不瘦身的话一张 4MB 图（5.6MB base64）就足以撑爆分片，
        // writeSessionShard 降到 40 条仍然写不进去 → 整个会话历史丢失。
        if (next.rawContent) {
          const slimmed = slimHistoryAttachments(next.rawContent)
          if (slimmed !== next.rawContent) next = { ...next, rawContent: slimmed }
        }
        if (next.tools && next.tools.some(t => t.progress !== undefined)) {
          next = {
            ...next,
            tools: next.tools.map(t => {
              if (t.progress === undefined) return t
              const { progress: _p, ...rest } = t
              return rest
            }),
          }
        }
        return next
      })
    }
  }
  return trimmed
}

/** 单个会话分片的预警阈值（防单个巨型会话吃满 localStorage 总配额） */
const SESSION_WARN_BYTES = 1024 * 1024

/** 把单个会话写入独立分片 key。超限自动降级裁剪（200 → 100 → 40），失败返回 false。 */
function writeSessionShard(sid: string, msgs: ChatMessage[]): boolean {
  const write = (limit: number): number => {
    const trimmed = buildTrimmedMessages({ [sid]: msgs }, limit)[sid] ?? []
    const json = JSON.stringify(trimmed)
    localStorage.setItem(shardKey(sid), json)
    return json.length * 2 // UTF-16 近似字节数
  }
  try {
    let bytes = write(STORAGE_LIMIT)
    if (bytes > SESSION_WARN_BYTES) {
      // 降级 1：只保留最近 100 条
      bytes = write(100)
      if (bytes > SESSION_WARN_BYTES) {
        // 降级 2：只保留最近 40 条（尽力保底）
        write(40)
      }
    }
    return true
  } catch {
    // localStorage 满了或不可用（QuotaExceededError）：降档重试，仍失败才放弃
    try {
      write(100)
      return true
    } catch {
      try {
        write(40)
        return true
      } catch {
        return false
      }
    }
  }
}

/** 删除某个会话的持久化数据（分片 + 老 key 中的残留一并清理，防下次加载"复活"） */
function removeSessionFromStorage(sessionId: string) {
  try {
    localStorage.removeItem(shardKey(sessionId))
  } catch { /* 静默 */ }
  lastSavedSessions.delete(sessionId)
  // 老格式单 key 里可能还有该会话：存在时重写一次（用户主动删会话，低频操作）
  try {
    let key: string | null = null
    const domainKey = storageKey(STORAGE_KEY)
    if (localStorage.getItem(domainKey) !== null) key = domainKey
    else if (chatStorageUserId && localStorage.getItem(STORAGE_KEY) !== null) key = STORAGE_KEY
    if (!key) return
    const blob = parseMessagesBlob(localStorage.getItem(key) || '')
    if (!blob || !(sessionId in blob)) return
    delete blob[sessionId]
    localStorage.setItem(key, JSON.stringify(blob))
  } catch { /* 静默 */ }
}

/** 把会话消息写入 localStorage（分片 + 脏跟踪：只写数组引用变化的会话）。
 *  返回是否成功；失败时调用方提示存储空间不足。 */
function saveMessagesToStorage(data: Record<string, ChatMessage[]>): boolean {
  // 1) 已从状态中消失的会话：清理分片（正常删除路径都会显式调用
  //    removeSessionFromStorage，这里兜底 state 层面的会话移除）
  for (const sid of Array.from(lastSavedSessions.keys())) {
    if (!(sid in data)) removeSessionFromStorage(sid)
  }
  // 2) 只序列化脏会话（未变会话数组引用不变，直接跳过——这是性能关键）
  let allOk = true
  for (const [sid, msgs] of Object.entries(data)) {
    if (lastSavedSessions.get(sid) === msgs) continue
    // 空会话不落盘（与旧版一致）；从有内容到清空 → 删除分片
    if (!msgs || msgs.length === 0) {
      if (lastSavedSessions.has(sid)) removeSessionFromStorage(sid)
      continue
    }
    if (writeSessionShard(sid, msgs)) {
      lastSavedSessions.set(sid, msgs)
    } else {
      allOk = false
    }
  }
  return allOk
}

/** 从 localStorage 读取上下文压力状态 */
function loadContextFromStorage(): Record<string, ContextPressure> {
  try {
    const raw = localStorage.getItem(storageKey(CONTEXT_STORAGE_KEY))
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return {}
    return parsed as Record<string, ContextPressure>
  } catch {
    return {}
  }
}

/** 把上下文压力状态写入 localStorage */
function saveContextToStorage(data: Record<string, ContextPressure>) {
  try {
    const trimmed: Record<string, ContextPressure> = {}
    for (const [sid, ctx] of Object.entries(data)) {
      if (ctx && ctx.contextWindow > 0) {
        trimmed[sid] = ctx
      }
    }
    localStorage.setItem(storageKey(CONTEXT_STORAGE_KEY), JSON.stringify(trimmed))
  } catch {
    // 静默
  }
}

/** 删除 localStorage 中某个会话的上下文状态 */
function deleteContextFromStorage(sessionId: string) {
  try {
    const data = loadContextFromStorage()
    if (sessionId in data) {
      delete data[sessionId]
      localStorage.setItem(storageKey(CONTEXT_STORAGE_KEY), JSON.stringify(data))
    }
  } catch {
    // 静默
  }
}

// ════════════════════════════════════════
//  会话级已加载技能（loaded_skills）持久化
//  会话级技能层：技能内容由后端注入 system prompt，
//  前端只需持久化"当前会话已加载了哪些技能"，每轮请求带给后端。
// ════════════════════════════════════════

/** 从 localStorage 读取各会话已加载的技能名列表 */
function loadLoadedSkillsFromStorage(): Record<string, string[]> {
  try {
    const raw = localStorage.getItem(storageKey(LOADED_SKILLS_STORAGE_KEY))
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return {}
    const data: Record<string, string[]> = {}
    for (const [k, v] of Object.entries(parsed)) {
      if (Array.isArray(v)) data[k] = v.filter(x => typeof x === 'string')
    }
    return data
  } catch {
    return {}
  }
}

/** 写入 localStorage（全量覆盖） */
function saveLoadedSkillsToStorage(data: Record<string, string[]>) {
  try {
    localStorage.setItem(storageKey(LOADED_SKILLS_STORAGE_KEY), JSON.stringify(data))
  } catch {
    // localStorage 满了或不可用，静默
  }
}

/** 从 localStorage 读取各会话的模式（纯聊天 / Agent 办公；默认纯聊天） */
function loadSessionModesFromStorage(): Record<string, 'chat' | 'agent'> {
  try {
    const raw = localStorage.getItem(storageKey(MODE_STORAGE_KEY))
    if (!raw) return {}
    const parsed = JSON.parse(raw)
    if (typeof parsed !== 'object' || parsed === null) return {}
    const data: Record<string, 'chat' | 'agent'> = {}
    for (const [k, v] of Object.entries(parsed)) {
      if (v === 'chat' || v === 'agent') data[k] = v
    }
    return data
  } catch {
    return {}
  }
}

/** 写入各会话模式（全量覆盖） */
function saveSessionModesToStorage(data: Record<string, 'chat' | 'agent'>) {
  try {
    localStorage.setItem(storageKey(MODE_STORAGE_KEY), JSON.stringify(data))
  } catch {
    // 静默
  }
}

// ── 用户最后使用的模式（打开页面/新建对话时默认沿用）──
// 用户级持久化：用户上次选了 Agent 办公，下次打开页面默认就是办公模式，
// 避免"想用 Agent 却显示纯聊天、工作区目录展不开"的困惑。
// 无记录时默认纯聊天（保持既有默认决策）。
const DEFAULT_MODE_KEY = 'chat_default_mode_v1'

/** 读取用户最后使用的模式（无记录返回 null → 默认纯聊天） */
function readDefaultMode(): 'chat' | 'agent' | null {
  try {
    const v = localStorage.getItem(storageKey(DEFAULT_MODE_KEY))
    return v === 'agent' ? 'agent' : v === 'chat' ? 'chat' : null
  } catch {
    return null
  }
}

/** 记录用户最后使用的模式 */
function saveDefaultMode(mode: 'chat' | 'agent') {
  try {
    localStorage.setItem(storageKey(DEFAULT_MODE_KEY), mode)
  } catch {
    // 静默
  }
}

/** AI 对话 hook — 支持多会话窗口和上下文占用百分比 */
export function useChat() {
  // ── 登录用户（用于 localStorage 存储域隔离）──
  const { user: authUser } = useAuth()

  // ── 多会话状态 ──
  const [sessions, setSessions] = useState<ChatSession[]>([])
  const [activeSessionId, setActiveSessionId] = useState<string | undefined>(undefined)
  const [activeSessionPk, setActiveSessionPk] = useState<number | undefined>(undefined)

  // 每个会话的消息历史（key = session_id）
  // 初始化时从 localStorage 恢复
  const [messagesBySession, setMessagesBySession] = useState<Record<string, ChatMessage[]>>(() =>
    loadMessagesFromStorage(),
  )
  // 每个会话的上下文压力（key = session_id）
  // 初始化时从 localStorage 恢复
  const [contextBySession, setContextBySession] = useState<Record<string, ContextPressure>>(() =>
    loadContextFromStorage(),
  )

  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [quota, setQuota] = useState<ChatQuotaResponse | null>(null)
  const [models, setModels] = useState<ChatModel[]>([])
  // 最近一次模型自动降级信息（{name, status}）— 供 AIChat 弹 toast 提示
  const [lastModelSwitch, setLastModelSwitch] = useState<{ name: string; status: number } | null>(null)
  const [selectedModelId, setSelectedModelId] = useState<number | undefined>(() => {
    try {
      const raw = localStorage.getItem(storageKey(MODEL_STORAGE_KEY))
      return raw ? Number(raw) : undefined
    } catch { return undefined }
  })

  // ── messagesBySession 的 ref 镜像 ──
  // 用于在 async 回调和 pagehide 中获取最新值（避免闭包过期问题）
  // 注意：ref 的同步在 setMessagesBySessionSync 中完成（functional update 时同步写 ref）
  const messagesBySessionRef = useRef(messagesBySession)

  // ── contextBySession 的 ref 镜像 ──
  const contextBySessionRef = useRef(contextBySession)

  // ── 会话级已加载技能（key = session_id）──
  // 初始化时从 localStorage 恢复；加载成功/清理时同步更新 ref 并持久化
  const [loadedSkillsBySession, setLoadedSkillsBySession] = useState<Record<string, string[]>>(() =>
    loadLoadedSkillsFromStorage(),
  )
  const loadedSkillsBySessionRef = useRef(loadedSkillsBySession)
  // 技能全文是否已注入过（per session，内存态即可）：
  // 技能加载后首轮发送 full_inject=true（后端注入完整指令），之后只注入摘要，
  // 避免大技能全文反复占用上下文（继续任务又从头加载的根治）。
  const loadedSkillsInjectedRef = useRef<Record<string, boolean>>({})

  // ── 会话级模式（chat 纯聊天 / agent 办公）──
  // 按 session_id 持久化到 localStorage（默认纯聊天，避免用户被 Agent 复杂度淹没）
  const [sessionModes, setSessionModes] = useState<Record<string, 'chat' | 'agent'>>(() =>
    loadSessionModesFromStorage(),
  )
  const sessionModesRef = useRef(sessionModes)

  // ── 用户最后使用的模式（打开页面/新建对话默认沿用；无记录默认纯聊天）──
  const [defaultMode, setDefaultMode] = useState<'chat' | 'agent' | null>(() => readDefaultMode())

  // ── 当前回合的 Agent 阶段（S1 阶段感知状态机）──
  // 根据 SSE 事件实时推断 AI 处于哪个阶段，供状态条显示：
  // waiting=等待模型响应 / thinking=深度思考 / tool_call=调用工具 /
  // tool_exec=工具执行中 / answering=生成回答
  const [agentPhase, setAgentPhase] = useState<AgentPhase>('idle')
  const [agentToolName, setAgentToolName] = useState<string | undefined>(undefined)

  // ── 工作区内容自动注入档位（P4 + 注入开关优化3）──
  // Agent 模式下三档：full 全文 / tree 仅目录 / off 关。
  // 只决定"是否预先把工作区文件内容塞进 system prompt"，不影响工具能力——
  // 读写工作区文件由 Agent 模式本身（enable_tools）决定，off 档 AI 依然能建文件。
  // 用户级持久化，默认全文（与旧的默认"开"一致）。
  const [workspaceInjectMode, setWorkspaceInjectModeState] = useState<WorkspaceInjectMode>(readInjectMode)
  const setWorkspaceInjectMode = useCallback((v: WorkspaceInjectMode) => {
    setWorkspaceInjectModeState(v)
    persistInjectMode(v)
  }, [])
  /** 点一下按「全文 → 仅目录 → 关 → 全文」循环；侧边栏与首页两处开关共用此顺序 */
  const cycleWorkspaceInjectMode = useCallback(() => {
    setWorkspaceInjectMode(
      workspaceInjectMode === 'full' ? 'tree' : workspaceInjectMode === 'tree' ? 'off' : 'full'
    )
  }, [workspaceInjectMode, setWorkspaceInjectMode])

  /** 设置指定会话的模式（持久化；同时记录为用户最后使用的模式，新会话默认沿用） */
  const setSessionMode = useCallback((sid: string, mode: 'chat' | 'agent') => {
    setSessionModes(prev => {
      const next = { ...prev, [sid]: mode }
      sessionModesRef.current = next
      saveSessionModesToStorage(next)
      return next
    })
    setDefaultMode(mode)
    saveDefaultMode(mode)
  }, [])

  /** 读取指定会话的模式（无记录时默认纯聊天） */
  const getSessionMode = useCallback((sid?: string): 'chat' | 'agent' => {
    if (!sid) return 'chat'
    return sessionModesRef.current[sid] ?? 'chat'
  }, [])

  /** 向指定会话追加一个已加载技能（去重 + 立即持久化） */
  const addLoadedSkill = useCallback((sid: string, name: string) => {
    setLoadedSkillsBySession(prev => {
      const cur = prev[sid] ?? []
      if (cur.includes(name)) return prev
      const next = { ...prev, [sid]: [...cur, name] }
      loadedSkillsBySessionRef.current = next
      saveLoadedSkillsToStorage(next)
      // 技能刚加载：下一次发送需注入完整指令（首轮），重置全文注入标记
      loadedSkillsInjectedRef.current[sid] = false
      return next
    })
  }, [])

  /** 删除指定会话的已加载技能记录 */
  const clearLoadedSkills = useCallback((sid: string) => {
    setLoadedSkillsBySession(prev => {
      if (!(sid in prev)) return prev
      const next = { ...prev }
      delete next[sid]
      loadedSkillsBySessionRef.current = next
      saveLoadedSkillsToStorage(next)
      return next
    })
  }, [])

  /** 移除指定会话的单个已加载技能（技能面板"取消加载"用） */
  const removeLoadedSkill = useCallback((sid: string, name: string) => {
    setLoadedSkillsBySession(prev => {
      const cur = prev[sid] ?? []
      if (!cur.includes(name)) return prev
      const next = { ...prev, [sid]: cur.filter(n => n !== name) }
      loadedSkillsBySessionRef.current = next
      saveLoadedSkillsToStorage(next)
      return next
    })
  }, [])

  // ── wrapped setter：更新 state 的同时同步更新 ref ──
  // 关键：在 functional update 回调中同步写 ref，确保 ref 和 state 永远同步
  const setMessagesBySessionSync = useCallback((
    updater: Record<string, ChatMessage[]> | ((prev: Record<string, ChatMessage[]>) => Record<string, ChatMessage[]>),
  ) => {
    setMessagesBySession(prev => {
      const next = typeof updater === 'function' ? updater(prev) : updater
      messagesBySessionRef.current = next
      return next
    })
  }, [])

  const setContextBySessionSync = useCallback((
    updater: Record<string, ContextPressure> | ((prev: Record<string, ContextPressure>) => Record<string, ContextPressure>),
  ) => {
    setContextBySession(prev => {
      const next = typeof updater === 'function' ? updater(prev) : updater
      contextBySessionRef.current = next
      return next
    })
  }, [])

  // ── 存储域跟随登录用户：切换用户/登录/登出时重新加载对应域数据 ──
  // 首次渲染时 user 可能尚未加载完成（AuthProvider 异步 refresh），
  // 此 effect 在 user 确定后切换存储域并重新从 localStorage 加载该域数据。
  const authUserId = authUser ? String(authUser.id) : null
  useEffect(() => {
    setChatStorageUser(authUserId)
    setMessagesBySessionSync(loadMessagesFromStorage())
    setContextBySessionSync(loadContextFromStorage())
    // 注意：以下两个用 functional update 同步 ref（与 setMessagesBySessionSync 同理），
    // 否则 getSessionMode/loadedSkills 读取的 ref 停留在旧存储域 → 模式错乱（显示聊天/工作区隐藏）
    setLoadedSkillsBySession(prev => {
      const next = loadLoadedSkillsFromStorage()
      loadedSkillsBySessionRef.current = next
      // 用户切换：重置技能全文注入标记（新用户/新存储域从首轮全文开始）
      loadedSkillsInjectedRef.current = {}
      return next
    })
    setSessionModes(prev => {
      const next = loadSessionModesFromStorage()
      sessionModesRef.current = next
      return next
    })
    setDefaultMode(readDefaultMode())
    // 存储域切换后必须重读注入档位：挂载瞬间处于 anon 域，读到的永远是默认 full，
    // 不重读就会把用户持久化的 tree/off 静默丢掉（每次刷新都退回「全文」）。
    // 用原始 setter，避免把刚读出的值再回写一次 localStorage。
    setWorkspaceInjectModeState(readInjectMode())
    setSelectedModelId(() => {
      try {
        const raw = localStorage.getItem(storageKey(MODEL_STORAGE_KEY))
        return raw ? Number(raw) : undefined
      } catch { return undefined }
    })
  }, [authUserId, setMessagesBySessionSync, setContextBySessionSync])

  // ── 方案 B + 轮询增强：刷新后恢复后台任务状态，轮询到终态自动补全 ──
  // 发送时任务 ID 已持久化到 localStorage；刷新后逐个轮询（每 5 秒）：
  // running → 会话内提示仍在后台生成；done → 用完整结果替换/补齐半截回复；
  // failed/cancelled → 清理记录。轮询最多持续 25 分钟（后端任务总时限 20 分钟）。
  useEffect(() => {
    if (!authUserId) return
    const tasks = loadStoredAgentTasks()
    if (tasks.length === 0) return

    const POLL_INTERVAL_MS = 5000
    const POLL_TIMEOUT_MS = 25 * 60 * 1000

    const timers: ReturnType<typeof setInterval>[] = []
    tasks.forEach(({ sid, taskId }) => {
      const startedAt = Date.now()
      let timer: ReturnType<typeof setInterval> | null = null
      let finished = false
      const stop = () => {
        finished = true
        if (timer) clearInterval(timer)
      }

      const poll = () => {
        if (finished) return
        fetchAgentTask(taskId).then(info => {
          if (!info) {
            // 任务记录不存在（已被清理/异常丢失）→ 停止轮询
            setAgentTaskStored(sid, null)
            stop()
            return
          }
          // 工具调用痕迹（#10）：后端持久化的工具事件 → 工具卡片格式
          // （流式期间工具卡片挂在同一条 assistant 消息上，恢复时同样整组挂回复消息）
          const restoredTools: ToolInvocation[] | undefined = info.tool_events?.length
            ? info.tool_events.map((ev: AgentTaskEvent) => ({
                id: ev.id,
                tool: ev.tool,
                arguments: ev.arguments,
                result: ev.result,
                round: ev.round,
                maxRounds: ev.max_rounds,
              }))
            : undefined
          if (info.status === 'done' && info.result) {
            // 完成：无缝替换刷新时留下的半截回复为完整结果（不带任何任务前缀）。
            // 注意：刷新后第一次 poll 若任务仍在 running，会插入"仍在后台生成"提示消息，
            // 消息序列变成 [user, 半截, 提示]——必须把半截替换为完整并删除提示；
            // 只替换最后一条会把提示换成完整而半截残留 → 出现两段内容。
            // 幂等用隐藏标记 resumedTaskId（不显示在正文）。
            setMessagesBySessionSync(prev => {
              const msgs = prev[sid] ?? []
              if (msgs.some(m => m.resumedTaskId === taskId)) return prev
              const fullContent = info.result
              const updated = [...msgs]
              const isStale = (m: ChatMessage) =>
                !m.content
                || m.content.includes('仍在后台生成')
                || (fullContent.trim().startsWith(m.content.trim()) && m.content.trim().length > 0)
                || m.content.includes(fullContent)

              // 收集所有本任务的残留 assistant 消息（半截/空占位/提示）
              const staleIdxList: number[] = []
              for (let i = 0; i < updated.length; i++) {
                const m = updated[i]
                if (m.role === 'assistant' && isStale(m)) staleIdxList.push(i)
              }

              if (staleIdxList.length === 0) {
                // 无残留（半截被清理或期间发了新消息）→ 追加完整回复
                updated.push({ role: 'assistant', content: fullContent, resumedTaskId: taskId, tools: restoredTools })
                return { ...prev, [sid]: updated }
              }

              // 第一条残留替换为完整回复，其余残留（提示消息等）删除。
              // 工具卡片：后端持久化的完整事件优先；旧任务无记录时保留刷新前已流出的部分卡片
              const firstStale = staleIdxList[0]
              const dropSet = new Set(staleIdxList.slice(1))
              const result: ChatMessage[] = []
              for (let i = 0; i < updated.length; i++) {
                if (i === firstStale) {
                  result.push({
                    ...updated[i],
                    content: fullContent,
                    resumedTaskId: taskId,
                    tools: restoredTools ?? updated[i].tools,
                  })
                } else if (!dropSet.has(i)) {
                  result.push(updated[i])
                }
              }
              return { ...prev, [sid]: result }
            })
            setAgentTaskStored(sid, null)
            stop()
            return
          }
          if (info.status === 'failed') {
            // 失败：提示错误（幂等，只加一次）；附工具卡片（能看到失败前执行了什么）
            setMessagesBySessionSync(prev => {
              const msgs = prev[sid] ?? []
              if (msgs.some(m => m.content && m.content.includes('任务执行失败'))) return prev
              const errText = `⚠️ 上次的任务执行失败：${(info.error || '未知错误').slice(0, 500)}`
              return { ...prev, [sid]: [...msgs, { role: 'assistant', content: errText, tools: restoredTools }] }
            })
            setAgentTaskStored(sid, null)
            stop()
            return
          }
          if (info.status === 'cancelled') {
            setAgentTaskStored(sid, null)
            stop()
            return
          }
          // running：提示任务仍在后台生成（幂等，只加一次）
          setMessagesBySessionSync(prev => {
            const msgs = prev[sid] ?? []
            if (msgs.some(m => m.content && m.content.includes('仍在后台生成'))) return prev
            return {
              ...prev,
              [sid]: [...msgs, {
                role: 'assistant' as const,
                content: '⚠️ 上次的回复仍在后台生成中，完成后会自动补全（无需刷新页面）。',
              }],
            }
          })
          // 超时保护：后端任务总时限 20 分钟，超过 25 分钟仍未完成视为异常，停止轮询
          if (Date.now() - startedAt > POLL_TIMEOUT_MS) {
            setAgentTaskStored(sid, null)
            stop()
          }
        }).catch(() => {
          // fetchAgentTask 内部已吞错（网络异常返回 null 已在上方处理）；
          // 此处兜底：不清理记录，等下一轮重试
        })
      }

      poll()
      timer = setInterval(poll, POLL_INTERVAL_MS)
      timers.push(timer)
    })
    return () => timers.forEach(t => clearInterval(t))
  }, [authUserId, setMessagesBySessionSync])

  // ── 刷新后从后端恢复 reasoning（方案二前端部分）──
  // 仅在切换会话时触发一次：检查最后一条 assistant 消息是否缺少 reasoning，
  // 若缺少则从后端 API 拉取并补充。用 ref 去重，不依赖 loading（避免渲染循环）。
  const restoredReasoningRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    if (!activeSessionId) return
    const sid = activeSessionId
    if (restoredReasoningRef.current.has(sid)) return
    // 流式期间不触发（用 ref 检查，不进依赖数组）
    if (isStreamingRef.current) return
    const msgs = messagesBySessionRef.current[sid]
    if (!msgs || msgs.length === 0) return
    const lastAssistant = [...msgs].reverse().find(m => m.role === 'assistant' && m.content)
    if (!lastAssistant || lastAssistant.reasoning) {
      restoredReasoningRef.current.add(sid)
      return
    }
    restoredReasoningRef.current.add(sid)
    let cancelled = false
    fetchSessionReasoning(sid).then((reasoning) => {
      if (cancelled || !reasoning || isStreamingRef.current) return
      setMessagesBySessionSync(prev => {
        const m = prev[sid]
        if (!m || m.length === 0) return prev
        const updated = [...m]
        for (let i = updated.length - 1; i >= 0; i--) {
          if (updated[i].role === 'assistant' && updated[i].content) {
            if (!updated[i].reasoning) updated[i] = { ...updated[i], reasoning }
            break
          }
        }
        return { ...prev, [sid]: updated }
      })
    }).catch(() => {
      restoredReasoningRef.current.delete(sid)
    })
    return () => { cancelled = true }
  }, [activeSessionId, setMessagesBySessionSync])

  // ── 流式渲染节流 ──
  const streamBufferRef = useRef<{ sid: string | null; text: string; rafId: number | null }>({ sid: null, text: '', rafId: null })

  // ── 自动压缩用的 compactContext 引用（定义在 send 之后，用 ref 避免 TDZ）──
  const compactContextRef = useRef<(() => Promise<unknown>) | null>(null)

  // ── 流式中断控制器 ──
  // 用户点击停止按钮时 abort，SSE 流正常返回（保留已收到的内容）
  const abortControllerRef = useRef<AbortController | null>(null)
  // 标记本次流式结束是否为用户主动停止（区分异常结束）
  const userStoppedRef = useRef(false)
  // 方案 B：当前后台任务 ID（SSE 首事件下发；停止时发取消，刷新后凭此查询）
  const currentTaskIdRef = useRef<string | null>(null)

  // ── 停止 AI 回复 ──
  // 中断 SSE 流，保留已收到的部分内容，清理空占位消息
  const stopGeneration = useCallback(() => {
    // 标记本次为用户主动停止（区分异常结束）
    userStoppedRef.current = true
    // 方案 B：后台任务显式取消（SSE abort 后任务仍在后台跑，需发取消请求）
    const tid = currentTaskIdRef.current
    if (tid) {
      currentTaskIdRef.current = null
      cancelAgentTask(tid)
      if (activeSessionId) setAgentTaskStored(activeSessionId, null)
    }
    if (abortControllerRef.current) {
      abortControllerRef.current.abort()
      abortControllerRef.current = null
    }
    // 刷新残留的 stream buffer
    const buf = streamBufferRef.current
    if (buf.rafId !== null) {
      clearTimeout(buf.rafId)
      buf.rafId = null
    }
    const pending = buf.text
    buf.text = ''
    buf.sid = null
    // 把残留的 buffer 内容追加到当前 assistant 消息
    if (pending) {
      setMessagesBySessionSync(prev => {
        const sid = activeSessionId
        if (!sid) return prev
        const msgs = prev[sid] ?? []
        const last = msgs[msgs.length - 1]
        if (last && last.role === 'assistant') {
          const updated = [...msgs]
          updated[updated.length - 1] = { ...last, content: last.content + pending }
          return { ...prev, [sid]: updated }
        }
        return prev
      })
    }
    isStreamingRef.current = false
    setLoading(false)
  }, [activeSessionId])

  // ── 消息变化时自动持久化到 localStorage ──
  // 流式过程中用更短的 debounce（150ms），非流式时 500ms
  const saveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const isStreamingRef = useRef(false)
  useEffect(() => {
    if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
    // 流式期间每 2s 全量保存一次（过长对话 + 大工具输出时避免频繁 stringify 卡顿）；
    // 流式结束/页面卸载时另有强制保存兜底
    const delay = isStreamingRef.current ? 2000 : 500
    saveTimerRef.current = setTimeout(() => {
      const ok = saveMessagesToStorage(messagesBySessionRef.current)
      setStorageWarning(ok ? null : '本地存储空间不足，对话记录可能无法完整保存。建议删除旧会话或清理浏览器数据。')
    }, delay)
    return () => {
      if (saveTimerRef.current) clearTimeout(saveTimerRef.current)
    }
  }, [messagesBySession])

  // ── 页面卸载/刷新前立即同步写入 localStorage ──
  // 监听器只注册一次（不随 state 变化重新注册），通过 ref 读取最新数据
  useEffect(() => {
    const flushToStorage = () => {
      // 取消 debounce 定时器
      if (saveTimerRef.current) {
        clearTimeout(saveTimerRef.current)
        saveTimerRef.current = null
      }
      // 取消可能残留的 rAF，把 buffer 中未刷的文本手动拼入
      const buf = streamBufferRef.current
      if (buf.rafId !== null) {
        clearTimeout(buf.rafId)
        buf.rafId = null
      }
      const pendingText = buf.text
      const bufSid = buf.sid
      // 从 ref 读取最新的 messagesBySession（不受闭包过期影响）
      let dataToSave = messagesBySessionRef.current
      if (pendingText && bufSid) {
        // 把 buffer 中未刷入 state 的文本追加到对应会话的最后一条 assistant 消息
        const msgs = dataToSave[bufSid] ?? []
        const last = msgs[msgs.length - 1]
        if (last && last.role === 'assistant') {
          const updated = [...msgs]
          updated[updated.length - 1] = { ...last, content: last.content + pendingText }
          dataToSave = { ...dataToSave, [bufSid]: updated }
        }
      }
      saveMessagesToStorage(dataToSave)
      saveContextToStorage(contextBySessionRef.current)
    }
    // pagehide 兼容性最好（包括 bfcache 场景）
    window.addEventListener('pagehide', flushToStorage)
    // beforeunload 作为补充：先强制落盘；AI 流式生成中则弹系统确认框拦截误刷新
    const handleBeforeUnload = (e: BeforeUnloadEvent) => {
      flushToStorage()
      if (isStreamingRef.current) {
        e.preventDefault()
        e.returnValue = ''
      }
    }
    window.addEventListener('beforeunload', handleBeforeUnload)
    // visibilitychange 处理移动端切后台场景
    const handleVisibility = () => {
      if (document.visibilityState === 'hidden') {
        flushToStorage()
      }
    }
    document.addEventListener('visibilitychange', handleVisibility)
    return () => {
      window.removeEventListener('pagehide', flushToStorage)
      window.removeEventListener('beforeunload', handleBeforeUnload)
      document.removeEventListener('visibilitychange', handleVisibility)
    }
  }, []) // ← 空依赖数组：只注册一次，通过 ref 读取最新值

  // ── 上下文压力变化时持久化到 localStorage ──
  const contextSaveTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => {
    if (contextSaveTimerRef.current) clearTimeout(contextSaveTimerRef.current)
    contextSaveTimerRef.current = setTimeout(() => {
      saveContextToStorage(contextBySession)
    }, 500)
    return () => {
      if (contextSaveTimerRef.current) clearTimeout(contextSaveTimerRef.current)
    }
  }, [contextBySession])

  // ── 加载模型列表 ──
  // 如果首次加载失败（后端不可用），会自动重试，确保模型栏不会永久消失
  const loadModels = useCallback(async () => {
    try {
      const list = await fetchChatModels()
      setModels(list)
      if (list.length > 0) {
        // 用 functional update 读取最新选中模型，避免闭包过期覆盖用户选择：
        // 刷新后 authUserId 异步恢复用户上次选的模型，与 loadModels 存在竞态，
        // 旧闭包里的 selectedModelId 是首次渲染值（undefined）→ 会把模型改回默认。
        setSelectedModelId(prev => {
          if (prev !== undefined && list.some(m => m.id === prev)) return prev
          const def = list.find(m => m.is_default)
          return def ? def.id : list[0].id
        })
      }
    } catch { /* 静默 */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    loadModels()
    // ── 自动重试：如果模型列表为空（首次加载失败），每隔 5 秒重试 ──
    const retryTimer = setInterval(() => {
      setModels(prev => {
        if (prev.length === 0) {
          loadModels()
        }
        return prev
      })
    }, 5000)
    return () => clearInterval(retryTimer)
  }, [loadModels])

  // ── 模型选择变化时持久化到 localStorage ──
  useEffect(() => {
    if (selectedModelId !== undefined) {
      try { localStorage.setItem(storageKey(MODEL_STORAGE_KEY), String(selectedModelId)) } catch { /* 静默 */ }
    }
  }, [selectedModelId])

  // ── 加载会话列表 ──
  const loadSessions = useCallback(async () => {
    try {
      const list = await fetchChatSessions()
      setSessions(list)
      // 刷新后恢复上次活跃会话（方案 B：刷新不丢当前聊天上下文，回到原聊天框）
      // 会话已被删除/未找到 → 停留 Hero，让用户自由选择
      try {
        const saved = localStorage.getItem(storageKey(ACTIVE_SESSION_STORAGE_KEY))
        if (saved) {
          const restored = list.find(s => s.session_id === saved)
          if (restored) {
            setActiveSessionId(restored.session_id)
            setActiveSessionPk(restored.id)
          }
        }
      } catch { /* 忽略 */ }
    } catch { /* 静默 */ }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    // authUser 就绪后再加载（刷新瞬间 token 未恢复，提前拉列表会 401 失败）
    if (authUserId) loadSessions()
  }, [loadSessions, authUserId])

  // ── 持久化当前活跃会话（刷新后凭此恢复）──
  useEffect(() => {
    try {
      if (activeSessionId) {
        localStorage.setItem(storageKey(ACTIVE_SESSION_STORAGE_KEY), activeSessionId)
      } else {
        localStorage.removeItem(storageKey(ACTIVE_SESSION_STORAGE_KEY))
      }
    } catch { /* 忽略 */ }
  }, [activeSessionId])

  // ── 创建新会话 ──
  const newSession = useCallback(async (): Promise<ChatSession | null> => {
    try {
      const s = await createChatSession('新对话', selectedModelId)
      setSessions(prev => [s, ...prev])
      setActiveSessionId(s.session_id)
      setActiveSessionPk(s.id)
      setMessagesBySessionSync(prev => ({ ...prev, [s.session_id]: [] }))
      setContextBySessionSync(prev => ({ ...prev, [s.session_id]: { percent: 0, usedTokens: 0, contextWindow: 0, systemTokens: 0, toolsTokens: 0, messageTokens: 0 } }))
      // 新会话无已加载技能
      setLoadedSkillsBySession(prev => {
        if (!(s.session_id in prev)) return prev
        const next = { ...prev, [s.session_id]: [] }
        loadedSkillsBySessionRef.current = next
        saveLoadedSkillsToStorage(next)
        return next
      })
      setError(null)
      window.dispatchEvent(new CustomEvent('chat-session-changed', { detail: { sessionId: s.session_id } }))
      return s
    } catch (err) {
      const msg = err instanceof Error ? err.message : '创建会话失败'
      setError(msg)
      return null
    }
  }, [selectedModelId])

  // ── 切换会话 ──
  const switchSession = useCallback((sessionId: string) => {
    const s = sessions.find(x => x.session_id === sessionId)
    if (!s) return
    setActiveSessionId(sessionId)
    setActiveSessionPk(s.id)
    setError(null)
    // 通知工作区面板清空"最近编辑"集合：切换会话 = 新对话上下文，
    // 上一轮的触达高亮不再相关。WorkspacePanel 监听此事件重置 baseline。
    window.dispatchEvent(new CustomEvent('chat-session-changed', { detail: { sessionId } }))
  }, [sessions])

  // ── 删除会话 ──
  const removeSession = useCallback(async (sessionPk: number) => {
    try {
      // 找到被删除会话的 session_id，清理 localStorage
      const target = sessions.find(s => s.id === sessionPk)
      if (target) {
        removeSessionFromStorage(target.session_id)
        deleteContextFromStorage(target.session_id)
        // 清理已加载技能记录
        clearLoadedSkills(target.session_id)
      }
      await deleteChatSession(sessionPk)
      setSessions(prev => {
        const next = prev.filter(s => s.id !== sessionPk)
        // 如果删除的是当前活跃会话，切换到第一个
        if (activeSessionPk === sessionPk) {
          if (next.length > 0) {
            setActiveSessionId(next[0].session_id)
            setActiveSessionPk(next[0].id)
          } else {
            setActiveSessionId(undefined)
            setActiveSessionPk(undefined)
          }
        }
        return next
      })
      // 清理内存中的消息
      if (target) {
        setMessagesBySessionSync(prev => {
          const next = { ...prev }
          delete next[target.session_id]
          return next
        })
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : '删除会话失败'
      setError(msg)
    }
  }, [activeSessionPk, sessions])

  // ── 重命名会话 ──
  const renameSession = useCallback(async (sessionPk: number, title: string) => {
    try {
      const updated = await updateChatSession(sessionPk, { title })
      setSessions(prev => prev.map(s => s.id === sessionPk ? updated : s))
    } catch { /* 静默 */ }
  }, [])

  // ── 当前会话的消息 ──
  const messages = activeSessionId
    ? messagesBySession[activeSessionId] ?? []
    : []

  // ── 当前会话的上下文压力 ──
  const contextPressure = activeSessionId
    ? contextBySession[activeSessionId] ?? null
    : null

  // ── 构建发给后端的 history ──
  // 取当前会话的消息（排除空的 assistant 占位），转为 {role, content}
  // 关键：用户消息优先使用 rawContent（含文件内容），确保后端能在后续轮次看到文件内容
  const buildHistory = useCallback((sid: string): { role: string; content: string }[] => {
    const msgs = messagesBySession[sid] ?? []
    return msgs
      .filter(m => {
        // 有附件但没有文字的消息也要保留（rawContent 不为空即可）
        if (m.role === 'user' && m.attachments && m.attachments.length > 0) {
          return !!(m.rawContent && m.rawContent.trim().length > 0)
        }
        return m.content && m.content.trim().length > 0
      })
      .map(m => {
        let content = (m.role === 'user' && m.rawContent) ? m.rawContent : m.content
        // 历史附件瘦身：大文件/图片不每轮重传（当前消息仍全量发送）
        content = slimHistoryAttachments(content)
        // assistant 消息附带本轮工具操作摘要——让下一轮 agent 记得本轮做过什么
        // （工具细节本身不持久化，但紧凑摘要进入 history）
        if (m.role === 'assistant' && m.tools && m.tools.length > 0) {
          const parts = m.tools.map(t => {
            const arg = t.arguments ?? {}
            const pathVal = typeof arg.path === 'string' ? arg.path
              : typeof arg.file_path === 'string' ? arg.file_path
              : typeof arg.name === 'string' ? arg.name
              : typeof arg.skill_name === 'string' ? arg.skill_name : ''
            const resultHead = t.result ? ` → ${t.result.split('\n')[0].slice(0, 60)}` : ''
            return `${t.tool}${pathVal ? `(${pathVal})` : ''}${resultHead}`
          }).filter(Boolean)
          if (parts.length > 0) {
            content = `${content}\n\n[本轮工具操作] ${parts.join('; ')}`
          }
        }
        return { role: m.role, content }
      })
  }, [messagesBySession])

  // ── 发送消息 ──
  // text: 发给后端的完整消息（含文件内容）
  // displayText: 前端显示用的纯文本（不含文件内容）
  // attachments: 附件元信息列表（用于前端渲染卡片）
  // initialMode: 可选。新会话首次发送时应用的初始模式（Hero 页预选；已有会话忽略）
  // 模式（chat 纯聊天 / agent 办公）按会话从 sessionModes 读取，决定是否启用工具与工作区注入
  const send = useCallback(async (text: string, modelId?: number, displayText?: string, attachments?: ChatAttachment[], initialMode?: 'chat' | 'agent') => {
    if (!text.trim() || loading) return

    setLoading(true)
    setError(null)
    setAgentPhase('waiting')  // S1：回合开始 → 等待模型首次输出
    setAgentToolName(undefined)

    // 如果没有活跃会话，自动创建一个
    let sid = activeSessionId
    let spk = activeSessionPk
    // 新会话刚创建时 setSessionMode 更新 ref 是异步的，立即 getSessionMode 会读到旧值，
    // 所以新会话用本地变量记录模式，避免"选了办公却当纯聊天处理"。
    let sessionInitMode: 'chat' | 'agent' | null = null
    if (!sid) {
      try {
        // 创建会话加 10 秒超时：正常应毫秒级返回；卡住说明网络/代理/后端问题，
        // 明确报错而不是无限转圈（用户以为"卡死"）
        const s = await Promise.race([
          createChatSession(text.trim().slice(0, 50), modelId ?? selectedModelId),
          new Promise<never>((_, rej) => setTimeout(
            () => rej(new Error('创建会话超时（10 秒）：请检查网络代理或后端服务是否正常运行')),
            10000,
          )),
        ])
        setSessions(prev => [s, ...prev])
        setActiveSessionId(s.session_id)
        setActiveSessionPk(s.id)
        sid = s.session_id
        spk = s.id
        // 新会话应用 Hero 页预选的初始模式；未预选时沿用用户最后使用的模式（defaultMode）
        const initial = initialMode ?? defaultMode
        sessionInitMode = initial ?? 'chat'
        if (initial && initial !== 'chat') setSessionMode(sid, initial)
      } catch (err) {
        const msg = err instanceof Error ? err.message : '创建会话失败'
        setError(msg)
        setLoading(false)
        return null
      }
    }

    // ── 自动压缩：上下文压力 ≥85% 且距上次自动压缩 >2 分钟 → 先压缩再发 ──
    // 替代"硬丢弃"：记忆被裁剪前先用摘要压缩，避免 agent 对较早内容失忆
    const pressure = sid ? contextBySession[sid] : undefined
    if (
      pressure && pressure.percent >= 80
      && Date.now() - lastAutoCompactRef.current > 2 * 60 * 1000
      && compactContextRef.current
    ) {
      lastAutoCompactRef.current = Date.now()
      try { await compactContextRef.current() } catch { /* 压缩失败不阻塞发送 */ }
    }

    // 收集发送前的历史（不包含当前用户消息）
    const history = sid ? buildHistory(sid) : []

    // 当前会话模式（决定工具/工作区注入，并标记消息）
    // 新会话直接用本地记录的模式（ref 更新异步，getSessionMode 可能读到旧值）；
    // 已有会话从 ref 读取。
    const mode = sessionInitMode ?? getSessionMode(sid) ?? 'chat'

    // 立即显示用户消息（前端只显示用户实际输入的文本+附件卡片，不展开文件内容）
    // rawContent 保存发给后端的完整消息（含文件内容），供 buildHistory 使用
    const userMsg: ChatMessage = {
      id: genMsgId(),
      role: 'user',
      content: displayText !== undefined ? displayText.trim() : text.trim(),
      rawContent: text.trim(),
      ...(attachments && attachments.length > 0 ? { attachments } : {}),
      mode,
    }
    setMessagesBySessionSync(prev => ({
      ...prev,
      [sid!]: [...(prev[sid!] ?? []), userMsg],
    }))

    const mid = modelId ?? selectedModelId

    try {
      // 立即插入一个空的 assistant 消息，流式更新其内容
      setMessagesBySessionSync(prev => ({
        ...prev,
        [sid!]: [...(prev[sid!] ?? []), { id: genMsgId(), role: 'assistant', content: '', mode }],
      }))

      let finalRes: ChatResponse | null = null

      // 取消可能残留的 rAF
      if (streamBufferRef.current.rafId !== null) {
        clearTimeout(streamBufferRef.current.rafId)
        streamBufferRef.current.rafId = null
      }
      streamBufferRef.current = { sid: sid!, text: '', rafId: null }
      isStreamingRef.current = true  // 标记流式开始，加快 localStorage 同步频率
      userStoppedRef.current = false  // 重置用户停止标记

      // 创建本轮流式的 AbortController，支持用户点击停止
      abortControllerRef.current = new AbortController()

      // 记录本轮回流中正在加载的 skill 名称（onToolCall → onToolResult 配对使用）
      // 用于技能加载成功后标记"会话已加载"，让后端后续注入 system prompt
      let pendingSkillName: string | undefined

      await streamChatMessage(
        text.trim(),
        sid,
        mid,
        // onChunk — rAF 节流后更新 state（每帧最多一次，避免每 token 触发全量重渲染）
        (chunkText) => {
          setAgentPhase('answering')  // S1：收到正文 → 生成回答阶段
          // 后端已开始输出 = 技能全文注入成功，标记本会话已注入（后续轮次只注入摘要）
          loadedSkillsInjectedRef.current[sid!] = true
          const buf = streamBufferRef.current
          if (buf.sid !== sid) return
          if (!chunkText) return
          buf.text += chunkText
          if (buf.rafId !== null) return  // 已有待刷新的 rAF
          buf.rafId = requestAnimationFrame(() => {
            buf.rafId = null
            const pending = buf.text
            buf.text = ''
            if (!pending) return
            setMessagesBySessionSync(prev => {
              const msgs = prev[sid!] ?? []
              const last = msgs[msgs.length - 1]
              if (last && last.role === 'assistant') {
                const updated = [...msgs]
                updated[updated.length - 1] = {
                  ...last,
                  content: last.content + pending,
                }
                return { ...prev, [sid!]: updated }
              }
              return prev
            })
          })
        },
        // onDone — 最终结果
        (res) => {
          // 刷新残留的 buffer
          const buf = streamBufferRef.current
          if (buf.rafId !== null) {
            clearTimeout(buf.rafId)
            buf.rafId = null
          }
          const pending = buf.text
          buf.text = ''
          buf.sid = null
          if (pending) {
            setMessagesBySessionSync(prev => {
              const msgs = prev[sid!] ?? []
              const last = msgs[msgs.length - 1]
              if (last && last.role === 'assistant') {
                const updated = [...msgs]
                updated[updated.length - 1] = { ...last, content: last.content + pending }
                return { ...prev, [sid!]: updated }
              }
              return prev
            })
          }
          finalRes = res
          // 方案 B：任务正常完成，清理持久化记录（刷新后无需再查询）
          currentTaskIdRef.current = null
          setAgentTaskStored(sid!, null)
        },
        // onError
        (errMsg) => {
          // 清理 rAF
          const buf = streamBufferRef.current
          if (buf.rafId !== null) {
            clearTimeout(buf.rafId)
            buf.rafId = null
          }
          buf.text = ''
          buf.sid = null
          // 本次发送失败：重置技能全文注入标记，下次重试仍注入完整指令
          loadedSkillsInjectedRef.current[sid!] = false
          // 方案 B：任务失败，清理持久化记录
          currentTaskIdRef.current = null
          setAgentTaskStored(sid!, null)
          throw new Error(errMsg)
        },
        // history — 前端把当前会话历史发给后端
        history,
        // onFile — AI 输出的文件块，追加到当前 assistant 消息
        (file) => {
          setMessagesBySessionSync(prev => {
            const msgs = prev[sid!] ?? []
            const last = msgs[msgs.length - 1]
            if (last && last.role === 'assistant') {
              const updated = [...msgs]
              const existingFiles = last.files ?? []
              updated[updated.length - 1] = {
                ...last,
                files: [...existingFiles, file],
              }
              return { ...prev, [sid!]: updated }
            }
            return prev
          })
        },
        // workspaceContext：仅 Agent 办公模式注入工作区上下文，档位由「自动带入文件内容」开关决定（P4）
        mode === 'agent' && workspaceInjectMode === 'full',
        // workspaceContextMode：三档注入（off / tree 仅目录 / full 全文）；纯聊天模式一律 off
        mode === 'agent' ? workspaceInjectMode : 'off',
        // onToolCall — AI 正在调用工具，追加到当前 assistant 消息的工具列表
        // B2：round/maxRounds 轮次信息，前端工具卡片显示"第 N/M 轮"
        (tool, args, id, round, maxRounds) => {
          setAgentPhase('tool_call')  // S1：调用工具阶段
          setAgentToolName(tool)
          // 记录正在加载的 skill 名称（会话级技能层：加载成功后标记已加载）
          pendingSkillName = tool === 'skill' && args && typeof args.name === 'string'
            ? args.name
            : undefined
          setMessagesBySessionSync(prev => {
            const msgs = prev[sid!] ?? []
            const last = msgs[msgs.length - 1]
            if (last && last.role === 'assistant') {
              const updated = [...msgs]
              const existingTools = last.tools ?? []
              updated[updated.length - 1] = {
                ...last,
                tools: [...existingTools, { id, tool, arguments: args, round, maxRounds }],
              }
              return { ...prev, [sid!]: updated }
            }
            return prev
          })
        },
        // onToolResult — 工具执行结果，按 tool_call_id 更新对应的工具记录
        (tool, result, id) => {
          setMessagesBySessionSync(prev => {
            const msgs = prev[sid!] ?? []
            const last = msgs[msgs.length - 1]
            if (last && last.role === 'assistant' && last.tools) {
              const updated = [...msgs]
              const tools = [...last.tools]
              // 优先按 id 匹配（并行工具正确归属）；找不到时回退"最后一个同名未完成"（兼容）
              let idx = -1
              if (id) idx = tools.findIndex(t => t.id === id)
              if (idx === -1) {
                for (let i = tools.length - 1; i >= 0; i--) {
                  if (tools[i].tool === tool && tools[i].result === undefined) {
                    idx = i
                    break
                  }
                }
              }
              if (idx >= 0) {
                tools[idx] = { ...tools[idx], result }
                updated[updated.length - 1] = { ...last, tools }
                return { ...prev, [sid!]: updated }
              }
            }
            return prev
          })
          // 技能加载成功 → 标记该会话已加载该技能（去重，后端据此注入 system prompt）
          if (tool === 'skill' && pendingSkillName && result && !result.startsWith('错误')) {
            addLoadedSkill(sid!, pendingSkillName)
          }
          pendingSkillName = undefined
          // 工具执行后可能改变了工作区文件，触发刷新
          if (
            tool === 'run_python' || tool === 'write_file' || tool === 'edit_file' ||
            tool === 'revert_file' || tool === 'rename_file' || tool === 'delete_file'
          ) {
            window.dispatchEvent(new CustomEvent('workspace-refresh'))
          }
        },
        // externalSignal — 用户点击停止时 abort
        abortControllerRef.current.signal,
        // enableTools：仅 Agent 办公模式启用工具循环（纯聊天不注入工具定义）
        mode === 'agent',
        // 会话级已加载技能列表 — 后端注入 system prompt
        loadedSkillsBySessionRef.current[sid!] ?? [],
        // 技能全文分级注入：技能加载后首轮 true（注入完整指令），之后 false（只注入摘要）
        !loadedSkillsInjectedRef.current[sid!],
        // 工具实时输出流（run_python stdout）— 按工具 id 归属，截断到 20K 防止 DOM 膨胀
        (text, id) => {
          setAgentPhase('tool_exec')  // S1：工具执行中（收到实时输出）
          const MAX_PROGRESS = 20000
          setMessagesBySessionSync(prev => {
            const msgs = prev[sid!] ?? []
            const last = msgs[msgs.length - 1]
            if (!(last && last.role === 'assistant')) return prev
            const updated = [...msgs]
            const lastMsg = { ...last }
            if (lastMsg.tools && lastMsg.tools.length > 0) {
              const tools = [...lastMsg.tools]
              // 优先按工具 id 归属；找不到（旧后端/未匹配）时归属到最后一个工具
              let idx = -1
              if (id) idx = tools.findIndex(t => t.id === id)
              if (idx === -1) idx = tools.length - 1
              if (idx >= 0) {
                const cur = tools[idx]?.progress ?? ''
                tools[idx] = { ...tools[idx], progress: (cur + text).slice(-MAX_PROGRESS) }
                lastMsg.tools = tools
              }
            } else {
              // 无工具列表时回退消息级 progress（兼容旧数据）
              lastMsg.progress = ((lastMsg.progress ?? '') + text).slice(-MAX_PROGRESS)
            }
            updated[updated.length - 1] = lastMsg
            return { ...prev, [sid!]: updated }
          })
        },
        // 推理内容流（reasoning_content）— 追加到当前 assistant 消息
        (text) => {
          setAgentPhase('thinking')  // S1：深度思考中（收到推理流）
          setMessagesBySessionSync(prev => {
            const msgs = prev[sid!] ?? []
            const last = msgs[msgs.length - 1]
            if (last && last.role === 'assistant') {
              const updated = [...msgs]
              updated[updated.length - 1] = { ...last, reasoning: (last.reasoning ?? '') + text }
              return { ...prev, [sid!]: updated }
            }
            return prev
          })
        },
        // 方案 B：任务 ID（SSE 首事件）— 停止按钮发取消、刷新后凭此查进度
        (taskId) => {
          currentTaskIdRef.current = taskId
          setAgentTaskStored(sid!, taskId)
        },
        // 模型自动降级（401/402/403/429 等）— 同步模型栏到实际调用的模型
        (name, status) => {
          const target = models.find(m => m.name === name)
          if (target && target.id !== selectedModelId) {
            setSelectedModelId(target.id)
          }
          setLastModelSwitch({ name, status })
        },
      )

      // 清理 abort controller（流式已结束）
      abortControllerRef.current = null

      isStreamingRef.current = false  // 流式结束，恢复正常的 localStorage 同步频率

      // 如果 SSE 流异常结束（未收到 done 事件），finalRes 为 null
      if (!finalRes) {
        // 用户主动停止 — 保留已收到的部分回复（即使为空也不报错）
        if (userStoppedRef.current) {
          const currentMsgs = messagesBySessionRef.current[sid!] ?? []
          const lastMsg = currentMsgs[currentMsgs.length - 1]
          const partialContent = (lastMsg && lastMsg.role === 'assistant' && lastMsg.content) ? lastMsg.content : ''
          finalRes = {
            response: partialContent,
            session_id: sid!,
            finish_reason: 'user_stopped',
            context_used_tokens: null,
            context_window: null,
            system_tokens: null,
            tools_tokens: null,
            message_tokens: null,
          } as ChatResponse
        } else {
          // 异常结束（网络断开等）— 清理空占位消息并报错
          setMessagesBySessionSync(prev => {
            const msgs = prev[sid!] ?? []
            if (msgs.length > 0 && msgs[msgs.length - 1].role === 'assistant' && !msgs[msgs.length - 1].content) {
              return { ...prev, [sid!]: msgs.slice(0, -1) }
            }
            return prev
          })
          throw new Error('对话异常结束，请稍后重试')
        }
      }

      const res = finalRes

      // 后端可能因 id collision 返回新的 session_id，需要同步迁移
      if (res.session_id && res.session_id !== sid) {
        const oldSid = sid
        sid = res.session_id
        // 迁移消息到新 session_id
        setMessagesBySessionSync(prev => {
          const msgs = prev[oldSid!] ?? []
          const updated = { ...prev, [sid!]: [...msgs] }
          delete updated[oldSid!]
          return updated
        })
        // 迁移上下文压力
        setContextBySessionSync(prev => {
          const ctx = prev[oldSid!]
          if (!ctx) return prev
          const updated = { ...prev, [sid!]: ctx }
          delete updated[oldSid!]
          return updated
        })
        // 迁移已加载技能（会话级技能层）
        setLoadedSkillsBySession(prev => {
          const skills = prev[oldSid!]
          if (!skills || skills.length === 0) return prev
          const updated = { ...prev, [sid!]: skills }
          delete updated[oldSid!]
          loadedSkillsBySessionRef.current = updated
          saveLoadedSkillsToStorage(updated)
          return updated
        })
        // 更新 activeSessionId
        setActiveSessionId(sid)
        // 更新 sessions 列表中的 session_id
        setSessions(prev => prev.map(s => s.session_id === oldSid ? { ...s, session_id: sid! } : s))
      }

      // 用最终完整文本覆盖流式累积的内容
      // 用户主动停止且无内容时，清理空的 assistant 占位消息
      if (userStoppedRef.current && !res.response) {
        setMessagesBySessionSync(prev => {
          const msgs = prev[sid!] ?? []
          if (msgs.length > 0 && msgs[msgs.length - 1].role === 'assistant' && !msgs[msgs.length - 1].content) {
            return { ...prev, [sid!]: msgs.slice(0, -1) }
          }
          return prev
        })
      } else {
        setMessagesBySessionSync(prev => {
          const msgs = prev[sid!] ?? []
          const last = msgs[msgs.length - 1]
          if (last && last.role === 'assistant') {
            const updated = [...msgs]
            updated[updated.length - 1] = {
              ...last,
              content: res.response,
              session_id: res.session_id,
            }
            return { ...prev, [sid!]: updated }
          }
          return prev
        })
      }

      // 更新上下文压力 — DSH 风格: 使用 provider 校准的压力投影
      if (res.context_used_tokens !== null || res.projected_tokens !== undefined) {
        const usedTokens = res.projected_tokens ?? res.context_used_tokens ?? 0
        const contextWindow = (res.context_window ?? contextBySession[sid!]?.contextWindow ?? 0) as number
        // B：百分比与 token 数同源（校准后的 projected/context_used），
        // 不再用后端发送前估算的 pressure_percent（CHARS_PER_TOKEN=4 对中文低估约 4 倍）
        const pressurePercent = contextWindow > 0 ? Math.min(100, Math.round(usedTokens / contextWindow * 100)) : 0
        setContextBySessionSync(prev => ({
          ...prev,
          [sid!]: {
            percent: pressurePercent,
            usedTokens,
            contextWindow,
            systemTokens: res.system_tokens ?? 0,
            toolsTokens: res.tools_tokens ?? 0,
            messageTokens: res.message_tokens ?? usedTokens,
            // DSH 扩展字段
            pressureTokens: res.pressure_tokens ?? null,
            projectedTokens: res.projected_tokens ?? null,
            historySelected: res.history_selected,
            historyTotal: res.history_total,
            historyDropped: res.history_dropped,
          },
        }))
      }

      // 更新会话列表中的标题和排序
      if (spk) {
        setSessions(prev => {
          const idx = prev.findIndex(s => s.id === spk)
          if (idx === -1) return prev
          const updated = { ...prev[idx], title: smartTitle(text) }
          const next = [...prev]
          next.splice(idx, 1)
          next.unshift(updated)
          return next
        })
      }

      // 刷新配额
      fetchChatQuota().then(setQuota).catch(() => {})

      return res
    } catch (err) {
      // 用户主动停止 — 不显示错误，保留已收到的部分回复
      if (userStoppedRef.current) {
        // 清理空的 assistant 占位消息
        setMessagesBySessionSync(prev => {
          const msgs = prev[sid!] ?? []
          if (msgs.length > 0 && msgs[msgs.length - 1].role === 'assistant' && !msgs[msgs.length - 1].content) {
            return { ...prev, [sid!]: msgs.slice(0, -1) }
          }
          return prev
        })
        return null
      }
      let msg = '对话失败'
      if (err instanceof Error) msg = err.message
      else if (typeof err === 'string') msg = err
      setError(msg)
      return null
    } finally {
      isStreamingRef.current = false  // 确保异常时也重置
      abortControllerRef.current = null  // 清理 abort controller
      setAgentPhase('idle')  // S1：回合结束，重置阶段状态
      setAgentToolName(undefined)
      // 流式结束立即强制保存（节流期间未落盘的内容一次性写入，防止刷新丢失）
      const saved = saveMessagesToStorage(messagesBySessionRef.current)
      if (!saved) setStorageWarning('本地存储空间不足，对话记录可能无法完整保存。建议删除旧会话或清理浏览器数据。')
      setLoading(false)
    }
  }, [loading, selectedModelId, activeSessionId, activeSessionPk, contextBySession, buildHistory, workspaceInjectMode, defaultMode, models])

  const reset = useCallback(() => {
    if (activeSessionId) {
      setMessagesBySessionSync(prev => ({ ...prev, [activeSessionId]: [] }))
      setContextBySessionSync(prev => ({ ...prev, [activeSessionId]: { percent: 0, usedTokens: 0, contextWindow: 0, systemTokens: 0, toolsTokens: 0, messageTokens: 0 } }))
      // 清理 localStorage 中该会话的消息
      removeSessionFromStorage(activeSessionId)
      deleteContextFromStorage(activeSessionId)
      // 清空已加载技能（会话级技能层随会话重置）
      clearLoadedSkills(activeSessionId)
    }
    setError(null)
  }, [activeSessionId, clearLoadedSkills])

  const [compacting, setCompacting] = useState(false)
  // 本地存储状态提示（写入失败/空间不足时显示）
  const [storageWarning, setStorageWarning] = useState<string | null>(null)
  // 自动压缩防抖：距上次自动压缩不足 2 分钟不重复触发
  const lastAutoCompactRef = useRef(0)

  const refreshQuota = useCallback(async () => {
    try {
      const q = await fetchChatQuota()
      setQuota(q)
    } catch { /* 静默 */ }
  }, [])

  // ── DSH 风格上下文压缩 — 7 维度结构化摘要 ──
  const compactContext = useCallback(async () => {
    if (!activeSessionId || compacting) return
    setCompacting(true)
    try {
      // 把当前会话历史发给后端做 DSH 风格压缩
      const history = buildHistory(activeSessionId)
      const res: CompactResponse = await compactChatContext(activeSessionId, selectedModelId, history)

      if (res.compacted && res.response) {
        // 用压缩摘要替换历史消息
        // DSH 风格: 摘要作为一条 user 消息（checkpoint），后续消息保留
        setMessagesBySessionSync(prev => {
          const msgs = prev[activeSessionId] ?? []
          // 用摘要替换全部旧历史，保留最近的消息
          // DSH compaction: shadowed range 被替换为一个 checkpoint 消息
          const shadowedCount = res.shadowed_count ?? msgs.length
          const keptMessages = msgs.slice(shadowedCount)
          const checkpointMsg: ChatMessage = {
            id: genMsgId(),
            role: 'user',
            content: '[上下文已压缩]',
            rawContent: res.response,  // checkpoint 摘要作为 rawContent
            compressedSummary: res.response,  // 前端可展开查看压缩摘要
          }
          return { ...prev, [activeSessionId]: [checkpointMsg, ...keptMessages] }
        })
      }

      // 更新上下文压力
      if (res.context_used_tokens !== null || res.pressure_percent !== undefined) {
        const usedTokens = res.message_tokens ?? res.context_used_tokens ?? 0
        const contextWindow = res.context_window
          ?? contextBySession[activeSessionId]?.contextWindow
          ?? 0
        // B：百分比与 token 数同源（校准后的 projected/context_used），
        // 不再用后端发送前估算的 pressure_percent（CHARS_PER_TOKEN=4 对中文低估约 4 倍）
        const pressurePercent = contextWindow > 0 ? Math.min(100, Math.round(usedTokens / contextWindow * 100)) : 0
        setContextBySessionSync(prev => ({
          ...prev,
          [activeSessionId]: {
            percent: pressurePercent,
            usedTokens,
            contextWindow,
            systemTokens: res.system_tokens ?? 0,
            toolsTokens: res.tools_tokens ?? 0,
            messageTokens: res.message_tokens ?? usedTokens,
          },
        }))
      }
      return res
    } catch (err) {
      const msg = err instanceof Error ? err.message : '上下文压缩失败'
      setError(msg)
      return null
    } finally {
      setCompacting(false)
    }
  }, [activeSessionId, compacting, selectedModelId, contextBySession, buildHistory])

  // 同步 compactContext 到 ref（send 的自动压缩在 compactContext 定义前使用，避免 TDZ）
  useEffect(() => {
    compactContextRef.current = compactContext
  }, [compactContext])

  return {
    // 多会话
    sessions,
    activeSessionId,
    activeSessionPk,
    newSession,
    switchSession,
    removeSession,
    renameSession,
    loadSessions,
    // 消息
    messages,
    loading,
    error,
    // 上下文
    contextPressure,
    // 本地存储状态提示
    storageWarning,
    onDismissStorageWarning: () => setStorageWarning(null),
    // 配额 & 模型
    quota,
    models,
    selectedModelId,
    setSelectedModelId,
    lastModelSwitch,
    send,
    stopGeneration,
    reset,
    refreshQuota,
    loadModels,
    // 上下文压缩
    compactContext,
    compacting,
    // 会话级模式（纯聊天 / Agent 办公）
    sessionModes,
    setSessionMode,
    getSessionMode,
    // 用户最后使用的模式（打开页面/新建对话默认沿用；无记录为 null）
    defaultMode,
    // S1 阶段感知状态机：当前回合的 Agent 阶段（状态条实时显示）
    agentPhase,
    agentToolName,
    // P4 + 注入开关优化3：工作区内容自动注入档位（full 全文 / tree 仅目录 / off 关）
    workspaceInjectMode,
    setWorkspaceInjectMode,
    cycleWorkspaceInjectMode,
    // 会话级已加载技能（技能面板展示/手动加载）
    loadedSkillsBySession,
    addLoadedSkill,
    removeLoadedSkill,
  }
}
