// 封面兜底：按赛事名生成稳定的色相 + 首字
// 用途：没有封面图、或图片加载失败时，卡片/详情页显示同色系渐变 + 赛事首字，
// 避免出现灰白破图占位，也保证不同赛事之间的视觉区分。
const LEADING_NOISE = /^[\s"'“”‘’《》〈〉【】\[\]（）()\-—·]+/
const GLYPH = /[\u3400-\u9fffA-Za-z0-9]/

// 取赛事名里第一个有意义的字符（跳过引号、括号、序号等），英文转大写
export function coverGlyph(title) {
  const raw = String(title || '').replace(/【[^】]*】/g, '')
  const cleaned = raw.replace(LEADING_NOISE, '')
  const matched = cleaned.match(GLYPH)
  return matched ? matched[0].toUpperCase() : '赛'
}

// 同一赛事名永远得到同一个色相（0-359），刷新/换页面都不会变
export function coverFallback(title) {
  const raw = String(title || '竞赛')
  let hue = 0
  for (let i = 0; i < raw.length; i += 1) {
    hue = (hue * 31 + raw.charCodeAt(i)) % 360
  }
  return { hue, glyph: coverGlyph(raw) }
}
