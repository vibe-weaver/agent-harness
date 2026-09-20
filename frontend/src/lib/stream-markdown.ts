// 流式 Markdown 未闭合语法兜底
// 流式期间模型输出在半截截断（``` 未闭合、** 未闭合、]( 未闭合等），
// 直接交给 react-markdown 会把语法符号当纯文本渲染，闭合瞬间又跳变。
// 此函数对尾部做探测补全，让中间帧也渲染成"最终形态"的前缀。
// 仅在流式期间调用；流结束后内容自然完整，不走这里。

const FENCE_RE = /^ {0,3}(`{3,}|~{3,})/

export function completeStreamingMarkdown(src: string): string {
  if (!src) return src

  const lines = src.split('\n')

  // ── 1) 围栏代码块配对（CommonMark 简化：同符号、闭合不短于开启）──
  let inFence = false
  let fenceMarker = ''
  let tailStart = 0
  for (let i = 0; i < lines.length; i++) {
    const m = FENCE_RE.exec(lines[i])
    if (!m) continue
    const marker = m[1]
    if (!inFence) {
      inFence = true
      fenceMarker = marker
    } else if (marker[0] === fenceMarker[0] && marker.length >= fenceMarker.length) {
      inFence = false
      tailStart = i + 1
    }
  }

  // 流截断在代码块内部 → 补上配对闭合围栏（代码块内其他符号都是字面量，不处理）
  if (inFence) {
    const sep = src.endsWith('\n') ? '' : '\n'
    return src + sep + fenceMarker
  }

  // ── 2) 代码块之外的尾部正文：行内语法探测 ──
  const tail = lines.slice(tailStart).join('\n')

  // 行内代码：反引号奇数个 → 未闭合，补一个
  const backticks = (tail.match(/`/g) || []).length
  if (backticks % 2 === 1) {
    return src + '`'
  }

  // 加粗：先剔除已成对的行内代码再数 **（避免代码里的星号干扰）
  const noCode = tail.replace(/`[^`\n]*`/g, '')
  const bolds = (noCode.match(/\*\*/g) || []).length
  if (bolds % 2 === 1) {
    return src + '**'
  }

  // 链接/图片：]( 之后没有 ) → 补右括号（未闭合时整段含括号文本会当纯字渲染，最扎眼）
  const linkOpen = noCode.lastIndexOf('](')
  if (linkOpen !== -1 && !noCode.slice(linkOpen).includes(')')) {
    return src + ')'
  }

  return src
}
