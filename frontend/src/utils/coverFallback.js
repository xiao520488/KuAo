// 封面兜底：按赛事名生成稳定的色相 + Logo 式字符
// 用途：没有封面图、或图片加载失败时，卡片/详情页显示同色系渐变 + 赛事标识，
// 避免出现灰白破图占位，也保证不同赛事之间的视觉区分。
//
// 显示优先级（与产品红线一致）：
//   1. 真实图片：由组件负责，只有 image 为空或加载失败才走到这里；
//   2. AI 提取的 2 字赛事简称（short_name，如「计设」「数模」）；
//   3. 简称为空 / 不合规时，回退到赛事名称首字。
const LEADING_NOISE = /^[\s"'“”‘’《》〈〉【】\[\]（）()\-—·]+/
const GLYPH = /[\u3400-\u9fffA-Za-z0-9]/
const HAN = /[\u3400-\u4dbf\u4e00-\u9fff]/g

// 取赛事名里第一个有意义的字符（跳过引号、括号、序号等），英文转大写
export function coverGlyph(title) {
  const raw = String(title || '').replace(/【[^】]*】/g, '')
  const cleaned = raw.replace(LEADING_NOISE, '')
  const matched = cleaned.match(GLYPH)
  return matched ? matched[0].toUpperCase() : '赛'
}

// 简称归一化：只保留汉字，必须恰好 2 个；否则视为无有效简称
export function coverShortName(shortName) {
  if (typeof shortName !== 'string') return ''
  const chars = shortName.normalize('NFKC').match(HAN) || []
  return chars.length === 2 ? chars.join('') : ''
}

// 同一赛事名永远得到同一个色相（0-359），刷新/换页面都不会变。
// isShortName = true 时前端使用两字排版（更小字号 + 字间距），避免拥挤溢出。
export function coverFallback(title, shortName = '') {
  const raw = String(title || '竞赛')
  let hue = 0
  for (let i = 0; i < raw.length; i += 1) {
    hue = (hue * 31 + raw.charCodeAt(i)) % 360
  }
  const twoChars = coverShortName(shortName)
  return {
    hue,
    glyph: twoChars || coverGlyph(raw),
    isShortName: Boolean(twoChars)
  }
}
