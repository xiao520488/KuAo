// 时间格式化（双轨制：精确日期优先，缺失时降级到月份）
//
// 数据来源说明：后端 competitions 同时保留两套时间字段——
//   * 月份轨 reg_start_month / reg_end_month / contest_start_month / contest_end_month（老数据）
//   * 日期轨 reg_start_date / reg_end_date / contest_start_date / contest_end_date（'YYYY-MM-DD' 或 'MM-DD'）
// 日期为空是常态（原文只写了月份），因此这里必须先试日期、再退回月份，
// 绝不反过来用月份拼日期。

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

const pad2 = (n) => String(n).padStart(2, '0')

// 解析日期轨字段：'YYYY-MM-DD' / 'MM-DD'，其余一律视为无效（返回 null）
export function parseDate(value) {
  if (value === null || value === undefined || value === '') return null
  const str = String(value).trim()
  let m = str.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/)
  if (m) {
    const month = Number(m[2])
    const day = Number(m[3])
    if (month < 1 || month > 12 || day < 1 || day > 31) return null
    return { text: `${m[1]}-${pad2(month)}-${pad2(day)}`, month, day, hasYear: true }
  }
  m = str.match(/^(\d{1,2})-(\d{1,2})$/)
  if (m) {
    const month = Number(m[1])
    const day = Number(m[2])
    if (month < 1 || month > 12 || day < 1 || day > 31) return null
    return { text: `${pad2(month)}-${pad2(day)}`, month, day, hasYear: false }
  }
  return null
}

const DAY_MS = 24 * 60 * 60 * 1000

// 中文日期："2026年9月10日"（无年份时 "9月10日"）
export function formatCnDate(value) {
  const parsed = parseDate(value)
  if (!parsed) return ''
  const year = parsed.hasYear ? `${String(value).trim().slice(0, 4)}年` : ''
  return `${year}${parsed.month}月${parsed.day}日`
}

// 阶段的起止区间："2026年9月10日 – 11月30日" / "2027年1月1日 – 3月5日"
// 同年省略后半段年份；只有一端时只显示那一端；都没有返回空串。
export function formatStageRange(startValue, endValue) {
  const start = parseDate(startValue)
  const end = parseDate(endValue)
  if (!start && !end) return ''
  if (start && end) {
    if (start.text === end.text) return formatCnDate(startValue)
    const startYear = start.hasYear ? String(startValue).trim().slice(0, 4) : ''
    const endYear = end.hasYear ? String(endValue).trim().slice(0, 4) : ''
    if (startYear && startYear === endYear) return `${formatCnDate(startValue)} – ${end.month}月${end.day}日`
    return `${formatCnDate(startValue)} – ${formatCnDate(endValue)}`
  }
  return formatCnDate(start ? startValue : endValue)
}

// 阶段表 -> 顶部总跨度文案（带年份，如「2025年11月 – 2026年3月」）。
// 与后端 stage_schedule.derive_span 同口径：报名不参与；精确到日优先，只到月用备注里的「YYYY年M月」。
export function formatStageSpan(stages) {
  if (!Array.isArray(stages)) return ''
  const items = []
  for (const row of stages) {
    if (!row || String(row.stage || '') === '报名') continue
    for (const key of ['start_date', 'end_date']) {
      const value = String(row[key] || '').trim()
      if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
        items.push({ key: value, kind: 0, label: formatCnDate(value) })
      }
    }
    const note = String(row.note || '').replace(/\s+/g, '')
    if (!note || /\d+\s*[日号]/.test(note)) continue
    const hit = note.match(/(20\d{2})\s*年\s*(\d{1,2})\s*月/)
    if (!hit) continue
    const year = Number(hit[1])
    const month = Number(hit[2])
    if (month < 1 || month > 12) continue
    items.push({
      key: `${year}-${String(month).padStart(2, '0')}-01`,
      kind: 1,
      label: `${year}年${month}月`
    })
  }
  if (!items.length) return ''
  items.sort((a, b) => (a.key === b.key ? a.kind - b.kind : a.key < b.key ? -1 : 1))
  const start = items[0]
  const end = items[items.length - 1]
  if (start.label === end.label) return start.label
  return `${start.label} – ${end.label}`
}

// 本地零点，避免「今天 23 点 vs 明天 0 点」被算成 1 天
function startOfDay(date) {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate())
}

// 日期轨字段 -> 本地零点 Date（无效返回 null）
// 无年份的 "MM-DD"：先按今年算；若已过去半年以上，则认为指的是明年（跨年赛事）
export function parseDateObject(value, today = new Date()) {
  const parsed = parseDate(value)
  if (!parsed) return null
  const { month, day, hasYear } = parsed
  let date = hasYear
    ? new Date(Number(String(value).trim().slice(0, 4)), month - 1, day)
    : new Date(today.getFullYear(), month - 1, day)

  // Date 会把 2 月 30 日滚到 3 月，这里回滚校验，非法日期直接判无效
  if (date.getMonth() !== month - 1 || date.getDate() !== day) return null

  if (!hasYear) {
    if (date.getTime() - startOfDay(today).getTime() < -180 * DAY_MS) {
      date = new Date(date.getFullYear() + 1, month - 1, day)
    }
  }
  return startOfDay(date)
}

const oneDay = DAY_MS

// 倒计时状态（卡片右上角徽章用）：返回 null 表示不展示
//   tone: 'urgent' 7 天内截止 | 'open' 报名中 | 'soon' 还没开报 | 'contest' 等开赛 | 'over' 已截止
//   days: 距离目标日期的自然日数（0 = 今天）
export function countdownState(item = {}, today = new Date()) {
  const now = startOfDay(today)
  const start = parseDateObject(item.reg_start_date, today)
  const end = parseDateObject(item.reg_end_date, today)
  const contestStart = parseDateObject(item.contest_start_date, today)
  const diffDays = (target) => Math.round((target.getTime() - now.getTime()) / oneDay)

  if (end) {
    const days = diffDays(end)
    if (days < 0) return { tone: 'over', text: '已截止', days }
    if (start && diffDays(start) > 0) {
      const until = diffDays(start)
      return { tone: 'soon', text: `距开报 ${until} 天`, days: until }
    }
    if (days === 0) return { tone: 'urgent', text: '今天截止', days: 0 }
    return {
      tone: days <= 7 ? 'urgent' : 'open',
      text: `报名倒计时 ${days} 天`,
      days
    }
  }

  if (start) {
    const until = diffDays(start)
    if (until > 0) return { tone: 'soon', text: `距开报 ${until} 天`, days: until }
  }

  if (contestStart) {
    const days = diffDays(contestStart)
    if (days > 0) return { tone: 'contest', text: `距开赛 ${days} 天`, days }
    if (days === 0) return { tone: 'contest', text: '今天开赛', days: 0 }
  }

  return null
}

// 报名时间：有日期用日期（"2026-03-24 ~ 2026-06-30" / "截止 2026-06-30"），
// 否则降级到月份（"3 月 - 6 月" / "3 月起" / "截止 6 月"）
export function formatSignUp(item = {}) {
  const startDate = parseDate(item.reg_start_date)
  const endDate = parseDate(item.reg_end_date)
  if (startDate || endDate) {
    if (startDate && endDate) {
      return startDate.text === endDate.text
        ? startDate.text
        : `${startDate.text} ~ ${endDate.text}`
    }
    if (startDate) return `${startDate.text} 起`
    return `截止 ${endDate.text}`
  }

  const start = toMonth(item.reg_start_month)
  const end = toMonth(item.reg_end_month)

  if (start && end) {
    // 跨年报名：结束月份小于开始月份，例如 12 月 - 次年 1 月
    // 后端 reg_cross_year 为真时也按跨年处理
    const crossYear =
      item.reg_cross_year === true || item.reg_cross_year === 1 ||
      item.reg_cross_year === '1' || item.reg_cross_year === 'true' ||
      end < start

    if (crossYear && end < start) return `${start} 月 - 次年 ${end} 月`
    return `${start} 月 - ${end} 月`
  }
  if (start) return `${start} 月起`
  if (end) return `截止 ${end} 月`
  return '待公布'
}

// 比赛时间：有日期用日期（"2026-11-27 ~ 2026-11-29"），否则降级到月份
// 同月 -> “11 月”；跨月 -> “10 月 - 11 月”；跨年 -> “12 月 - 次年 1 月”
export function formatContest(item = {}) {
  const startDate = parseDate(item.contest_start_date)
  const endDate = parseDate(item.contest_end_date)
  if (startDate || endDate) {
    if (startDate && endDate) {
      return startDate.text === endDate.text
        ? startDate.text
        : `${startDate.text} ~ ${endDate.text}`
    }
    return (startDate || endDate).text
  }

  const start = toMonth(item.contest_start_month)
  const end = toMonth(item.contest_end_month)

  if (start && end) {
    if (end < start) return `${start} 月 - 次年 ${end} 月`
    if (start === end) return `${start} 月`
    return `${start} 月 - ${end} 月`
  }
  if (start) return `${start} 月`
  if (end) return `${end} 月`
  return '待定'
}
