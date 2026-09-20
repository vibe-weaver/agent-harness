// 共享的 highlight.js 实例 —— 在一处注册语言，AIChat 的代码块渲染与
// 工作区预览面板的代码高亮复用同一份配置（ES 模块单例，import 即生效）。
//
// 设计：registerLanguage 是幂等的，多次 import 本模块只会注册一次。
// 选型对齐 AIChat 原内联注册的语言集合，保证两端高亮范围一致。

import hljs from 'highlight.js/lib/core'
import javascript from 'highlight.js/lib/languages/javascript'
import typescript from 'highlight.js/lib/languages/typescript'
import python from 'highlight.js/lib/languages/python'
import json from 'highlight.js/lib/languages/json'
import yaml from 'highlight.js/lib/languages/yaml'
import xml from 'highlight.js/lib/languages/xml'        // html / xml
import css from 'highlight.js/lib/languages/css'
import bash from 'highlight.js/lib/languages/bash'
import sql from 'highlight.js/lib/languages/sql'
import markdown from 'highlight.js/lib/languages/markdown'
import java from 'highlight.js/lib/languages/java'
import cpp from 'highlight.js/lib/languages/cpp'
import go from 'highlight.js/lib/languages/go'
import rust from 'highlight.js/lib/languages/rust'

hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('typescript', typescript)
hljs.registerLanguage('python', python)
hljs.registerLanguage('json', json)
hljs.registerLanguage('yaml', yaml)
hljs.registerLanguage('xml', xml)
hljs.registerLanguage('html', xml)
hljs.registerLanguage('css', css)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('shell', bash)
hljs.registerLanguage('sql', sql)
hljs.registerLanguage('markdown', markdown)
hljs.registerLanguage('md', markdown)
hljs.registerLanguage('java', java)
hljs.registerLanguage('cpp', cpp)
hljs.registerLanguage('c', cpp)
hljs.registerLanguage('go', go)
hljs.registerLanguage('rust', rust)

/** 按文件名推断 hljs 语言名（无则返回 null，调用方走 highlightAuto） */
export function langFromName(name: string): string | null {
  const ext = name.split('.').pop()?.toLowerCase() || ''
  const map: Record<string, string> = {
    js: 'javascript', mjs: 'javascript', cjs: 'javascript',
    ts: 'typescript', tsx: 'typescript', cts: 'typescript',
    jsx: 'javascript',
    py: 'python',
    json: 'json', jsonc: 'json',
    yaml: 'yaml', yml: 'yaml',
    xml: 'xml', svg: 'xml', html: 'html', htm: 'html',
    css: 'css',
    sh: 'bash', bash: 'bash', zsh: 'bash',
    sql: 'sql',
    md: 'markdown', markdown: 'markdown',
    java: 'java',
    cpp: 'cpp', cc: 'cpp', cxx: 'cpp', h: 'cpp', hpp: 'cpp',
    c: 'c',
    go: 'go',
    rs: 'rust',
  }
  return map[ext] ?? null
}

/** 高亮代码，返回 HTML 字符串（已转义）；未知语言走自动检测，失败回退纯文本转义 */
export function highlightCode(code: string, lang: string | null): string {
  try {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(code, { language: lang }).value
    }
    return hljs.highlightAuto(code).value
  } catch {
    return escapeHtml(code)
  }
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
}

export default hljs
