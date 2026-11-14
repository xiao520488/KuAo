// 封面兜底：赛事徽记（App 图标式色块 + 分类标签）
// ------------------------------------------------------------------
// 取色规则（与产品约定一致）：颜色只由「赛事大类」决定，颜色本身承载信息。
//   工科 -> 蓝   理科 -> 绿   商科 -> 橙   文科 -> 紫   未分类 -> 中性灰
// 不再用赛事名哈希：哈希会让相邻卡片撞色/近似色（真实数据里 29 张卡只有 27 个色相，
// 出现同字同色的情况），反而变成视觉噪音，而且用户读不出任何含义。
//
// 具体明度/饱和度不在这里定，交给 CSS 统一控制，保证任何色相下的对比度达标。
import { resolveCategory } from './competitionCategory'

// 是否允许用爬来的外站海报当封面。false = 全站只用统一的程序化封面（CoverArt），
// 保证 85 张卡片风格一致；以后要接 AI 生成的本地图片，改成 true 并写 image_url 即可。
export const COVER_USE_REMOTE = false

const LEADING_NOISE = /^[\s"'“”‘’《》〈〉【】\[\]（）()\-—·]+/
const GLYPH = /[\u3400-\u4dbf\u4e00-\u9fffA-Za-z0-9]/
const HAN = /[\u3400-\u4dbf\u4e00-\u9fff]/g

/** 大类 -> 色相。同一大类永远同色，跨大类色相至少间隔 30° 以上。 */
export const MAJOR_HUE = {
  工科: 214, // 蓝
  理科: 158, // 绿
  商科: 30, // 橙
  文科: 268, // 紫
  未分类: 215 // 中性灰（CSS 里饱和度降到 8%）
}

// 取赛事名里第一个有意义的字符（跳过引号、括号、序号等），英文转大写
export function coverGlyph(title) {
  const raw = String(title || '').replace(/【[^】]*】/g, '')
  const cleaned = raw.replace(LEADING_NOISE, '')
  const matched = cleaned.match(GLYPH)
  return matched ? matched[0].toUpperCase() : '赛'
}

// 简称归一化：只保留汉字，取前 2 个；不足 2 个视为无有效简称
// （2026-11-14 放宽：4 字简称如「金种子杯」取前两字「金种」；此前要求恰好 2 字，
//  4 字简称会退化成名称首字，封面看起来只有「一个字」）
export function coverShortName(shortName) {
  if (typeof shortName !== 'string') return ''
  const chars = shortName.normalize('NFKC').match(HAN) || []
  if (chars.length < 2) return ''
  return chars.slice(0, 2).join('')
}

/**
 * 生成封面兜底所需的一切。
 * @param {{ title?: string, shortName?: string, category?: string }} input
 * @returns {{
 *   tone: string, hue: number, glyph: string, isShortName: boolean,
 *   major: string, minor: string, catLabel: string
 * }}
 */
export function coverFallback({ title = '', shortName = '', category = '' } = {}) {
  const { major, minor } = resolveCategory(category)
  const tone = major || '未分类'
  const twoChars = coverShortName(shortName)

  let catLabel = '未分类'
  if (major) catLabel = minor ? `${major} · ${minor}` : major

  return {
    tone,
    hue: MAJOR_HUE[tone] ?? MAJOR_HUE.未分类,
    glyph: twoChars || coverGlyph(title),
    isShortName: Boolean(twoChars),
    major,
    minor,
    catLabel
  }
}