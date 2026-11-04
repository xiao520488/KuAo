/**
 * 竞赛数据映射工具
 *
 * 后端 /api/competitions 返回的真实字段：
 *   id, name, category, level, level_label, reg_start_month, reg_end_month,
 *   reg_cross_year, official_url, intro, summary, tech_stack, sort_order, created_at
 *
 * 注意：后端没有 title / signUpTime / contestTime / image 这些字段，
 * 必须由本模块把后端字段转换成 CompetitionCard 需要的 props。
 */

// 只取月份数字：兼容 "2025-03-01" / "2025.03.01" / "2025/3/5" / 纯数字月份 "3" / Date 字符串
export function toMonth(value) {
  if (value === null || value === undefined || value === '') return null
  const str = String(value).trim()

  // 纯数字：可能是月份(1-12)或时间戳
  if (/^\d{1,2}$/.test(str)) {
    const n = parseInt(str, 10)
    return n >= 1 && n <= 12 ? n : null
  }

  // 带分隔符的日期：按 - . / 拆分，从候选中挑出 1-12 的月份
  // 形如 2025-03-01：年份是 4 位，月份是第 2 段
  const parts = str.split(/[-/.\sT:]+/).filter(Boolean)
  if (parts.length >= 2) {
    if (parts[0].length === 4) {
      const m = parseInt(parts[1], 10)
      if (m >= 1 && m <= 12) return m
    }
    // 形如 03-01（无年份）：第 1 段即月份
    const m = parseInt(parts[0], 10)
    if (m >= 1 && m <= 12) return m
  }

  // 兜底交给 Date 解析
  const d = new Date(str)
  if (!Number.isNaN(d.getTime())) return d.getMonth() + 1
  return null
}

// 报名时间格式化为 "X 月 - Y 月"
export function formatSignUp(item) {
  const start = toMonth(item.reg_start_month)
  const end = toMonth(item.reg_end_month)

  if (start && end) {
    // 跨年报名：结束月份小于开始月份，例如 12 月 - 次年 1 月
    // 后端 reg_cross_year 为真时也按跨年处理
    const crossYear =
      item.reg_cross_year === true ||
      item.reg_cross_year === 1 ||
      item.reg_cross_year === '1' ||
      item.reg_cross_year === 'true' ||
      end < start

    if (crossYear && end < start) return `${start} 月 - 次年 ${end} 月`
    return `${start} 月 - ${end} 月`
  }
  if (start) return `${start} 月起`
  if (end) return `截止 ${end} 月`
  return '待公布'
}

// 级别兜底：后端 level 是枚举 national/provincial，中文标签在 level_label
export const LEVEL_FALLBACK = {
  national: '国家级',
  provincial: '省级',
  municipal: '市级',
  regional: '区域级',
  international: '国际级'
}

/**
 * 后端字段 -> CompetitionCard props
 *
 * 安全约定：id 必须是后端给出的真实主键。
 * 若缺失/非法，返回 null 由调用方过滤掉，
 * 绝不用数组下标伪造 id（否则点进详情会请求到错误的竞赛）。
 *
 * @param {Object} item 后端返回的竞赛对象
 * @returns {Object|null} 卡片 props，id 非法时返回 null
 */
export function mapToCard(item) {
  if (!item || typeof item !== 'object') return null

  const rawId = item.id
  const id =
    typeof rawId === 'number' && Number.isFinite(rawId)
      ? rawId
      : typeof rawId === 'string' && rawId.trim() !== '' && Number.isFinite(Number(rawId))
        ? Number(rawId)
        : null
  if (id === null) return null

  const level = item.level_label || LEVEL_FALLBACK[item.level] || item.level || ''
  return {
    id,
    title: item.name || '未命名竞赛',
    level,
    category: item.category || item.subject || '',
    image: item.image ?? item.cover ?? '',
    signUpTime: formatSignUp(item),
    contestTime: '待定'
  }
}
