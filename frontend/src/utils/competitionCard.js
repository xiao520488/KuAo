import { formatSignUp, formatContest } from './timeFormat'

/**
 * 封面图兜底：只接受 http(s) 绝对地址，
 * 避免相对路径 / 协议相对地址 / 空串打坏卡片占位图。
 */
export function resolveCardImage(item) {
  const raw = (item && (item.image ?? item.cover ?? item.image_url)) || ''
  const url = typeof raw === 'string' ? raw.trim() : ''
  return /^https?:\/\//i.test(url) ? url : ''
}

/**
 * 后端竞赛对象 -> CompetitionCard 的 props。
 *
 * 首页、搜索页、备赛页共用同一套映射，避免各页面各写一份、
 * 字段一改就出现「某个页面卡片时间显示不对」的分裂。
 *
 * @param {Object} item 后端返回的竞赛对象
 * @param {number} [index=0] 无 id 时的兜底 key
 */
export function mapCompetitionToCard(item, index = 0) {
  const data = item || {}
  return {
    id: data.id ?? index,
    title: data.name || data.title || '未命名竞赛',
    category: data.category || data.subject || '',
    image: resolveCardImage(data),
    shortName: data.short_name || '',
    signUpTime: formatSignUp(data),
    contestTime: formatContest(data),
    // 日期轨原始值：只喂给倒计时徽章
    regStartDate: data.reg_start_date || '',
    regEndDate: data.reg_end_date || '',
    contestStartDate: data.contest_start_date || ''
  }
}