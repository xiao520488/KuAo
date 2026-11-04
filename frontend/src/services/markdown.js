import { marked } from 'marked'
import DOMPurify from 'dompurify'

// 基础 Markdown 配置：支持换行、GFM 表格等
marked.setOptions({
  gfm: true,
  breaks: true
})

/**
 * 将 Markdown 文本渲染为安全的 HTML 字符串。
 * 先经 marked 转为 HTML，再用 DOMPurify 过滤 XSS，最后交给 v-html。
 * @param {string} text Markdown 源文本
 * @returns {string} 净化后的 HTML
 */
export function renderMarkdown(text) {
  if (!text) return ''
  const rawHtml = marked.parse(String(text))
  return DOMPurify.sanitize(rawHtml)
}
