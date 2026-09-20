/**
 * 文档生成工具 — 将 AI 输出的纯文本内容转为真正的 PDF / Word 二进制文件
 *
 * AI 通过 <file-download name="xxx.pdf">文本内容</file-download> 输出文件时，
 * 标签内的内容是纯文本。本模块负责将纯文本转为对应格式的二进制 Blob，
 * 让用户下载到的是真正可用 .pdf / .docx 文件。
 *
 * PDF 生成调用后端 API（reportlab + 中文字体），避免前端 jsPDF 中文乱码问题。
 * Word 生成在前端用 docx 库完成。
 */

import { getToken } from './auth-api'

const API_BASE = '/api/v1'

// ── PDF 生成（调用后端 API）──

/**
 * 调用后端 API 将纯文本转为 PDF Blob
 * 后端使用 reportlab + 中文字体（微软雅黑/黑体），完美支持中文
 */
export async function textToPdfBlob(text: string, fileName: string): Promise<Blob> {
  const baseName = fileName.replace(/\.pdf$/i, '')
  const token = getToken()
  console.log('[doc-generate] PDF request:', { textLength: text.length, baseName, hasToken: !!token })
  const res = await fetch(`${API_BASE}/workspace/generate-doc`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { 'Authorization': `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({ text, format: 'pdf', filename: baseName }),
  })
  console.log('[doc-generate] PDF response:', res.status, res.statusText, res.headers.get('content-type'))
  if (!res.ok) {
    // 先尝试读取原始文本用于调试
    const rawText = await res.text()
    console.error('[doc-generate] PDF error response body:', rawText)
    let err: { detail: string }
    try {
      err = JSON.parse(rawText)
    } catch {
      err = { detail: `PDF 生成失败 (HTTP ${res.status})` }
    }
    throw new Error(typeof err.detail === 'string' ? err.detail : 'PDF 生成失败')
  }
  return res.blob()
}

// ── Word (.docx) 生成 (docx 库) ──

/**
 * 将纯文本转为 Word .docx Blob
 * 保留段落结构，支持 Markdown 标题行（# 开头）转为 Word 标题样式
 */
export async function textToDocxBlob(text: string, _fileName: string): Promise<Blob> {
  // Word 也调后端 API 生成，保持一致性
  const res = await fetch(`${API_BASE}/workspace/generate-doc`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Authorization': `Bearer ${getToken()}`,
    },
    body: JSON.stringify({ text, format: 'docx', filename: _fileName.replace(/\.docx$/i, '') }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Word 生成失败' }))
    throw new Error(typeof err.detail === 'string' ? err.detail : 'Word 生成失败')
  }
  return res.blob()
}

// ── 统一入口 ──

/** 需要转换为二进制文档的扩展名 */
export const CONVERTIBLE_EXTENSIONS = new Set(['pdf', 'docx'])

/**
 * 判断文件是否需要二进制转换
 */
export function needsConversion(fileName: string): boolean {
  const ext = fileName.split('.').pop()?.toLowerCase() || ''
  return CONVERTIBLE_EXTENSIONS.has(ext)
}

/**
 * 将纯文本内容转为对应格式的二进制 Blob
 * @param text 纯文本内容
 * @param fileName 文件名（用于判断格式）
 * @returns 二进制 Blob
 */
export async function convertToBlob(text: string, fileName: string): Promise<Blob> {
  const ext = fileName.split('.').pop()?.toLowerCase() || ''

  if (ext === 'pdf') {
    return textToPdfBlob(text, fileName)
  }

  if (ext === 'docx') {
    return textToDocxBlob(text, fileName)
  }

  // 不需要转换的文件，返回纯文本 Blob
  return new Blob([text], { type: 'text/plain;charset=utf-8' })
}
