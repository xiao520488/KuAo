/**
 * localStorage 缓存层（AI 结果本地缓存）
 *
 * 两类缓存：
 *   1) 竞赛级：这批获奖作品的「共性分析」报告
 *      key = common_${competitionId}
 *   2) 作品级：单篇作品的「AI 解析」结果
 *      key = work_${competitionId}_${workId}
 *
 * 统一存为 { text, savedAt }，savedAt 为毫秒时间戳，用于展示"缓存于 x 分钟前"。
 * 任何 localStorage 异常（隐私模式 / 配额满 / 数据腐坏）一律静默降级为"无缓存"，
 * 绝不能因为缓存问题阻断主流程。
 */

const COMMON_PREFIX = 'common_'
const WORK_PREFIX = 'work_'

/** @returns {Storage|null} */
function store() {
  try {
    if (typeof localStorage === 'undefined') return null
    // 探测可用性：Safari 隐私模式下 setItem 会抛异常
    const probe = '__probe__'
    localStorage.setItem(probe, '1')
    localStorage.removeItem(probe)
    return localStorage
  } catch (e) {
    return null
  }
}

/**
 * 读缓存
 * @param {string} key
 * @returns {{ text: string, savedAt: number }|null}
 */
function read(key) {
  const ls = store()
  if (!ls) return null
  try {
    const raw = ls.getItem(key)
    if (!raw) return null
    const parsed = JSON.parse(raw)
    // 结构校验：只有 text 非空才算有效缓存，腐坏数据直接忽略
    if (!parsed || typeof parsed.text !== 'string' || !parsed.text.trim()) return null
    return {
      text: parsed.text,
      savedAt: Number.isFinite(parsed.savedAt) ? parsed.savedAt : 0
    }
  } catch (e) {
    return null
  }
}

/**
 * 写缓存（超配额时静默失败，不影响主流程）
 * @param {string} key
 * @param {string} text
 * @returns {boolean} 是否写入成功
 */
function write(key, text) {
  const ls = store()
  if (!ls) return false
  if (typeof text !== 'string' || !text.trim()) return false
  try {
    ls.setItem(key, JSON.stringify({ text, savedAt: Date.now() }))
    return true
  } catch (e) {
    return false
  }
}

function remove(key) {
  const ls = store()
  if (!ls) return false
  try {
    ls.removeItem(key)
    return true
  } catch (e) {
    return false
  }
}

/* ---------------- 竞赛级：获奖作品共性分析 ---------------- */

export function commonKey(competitionId) {
  return `${COMMON_PREFIX}${competitionId}`
}

export function getCommonAnalysis(competitionId) {
  if (competitionId === null || competitionId === undefined || competitionId === '') return null
  return read(commonKey(competitionId))
}

export function setCommonAnalysis(competitionId, text) {
  if (competitionId === null || competitionId === undefined || competitionId === '') return false
  return write(commonKey(competitionId), text)
}

/* ---------------- 作品级：单篇作品 AI 解析 ---------------- */

export function workKey(competitionId, workId) {
  return `${WORK_PREFIX}${competitionId}_${workId}`
}

export function getWorkAnalysis(competitionId, workId) {
  if (competitionId === null || competitionId === undefined || competitionId === '') return null
  if (workId === null || workId === undefined || workId === '') return null
  return read(workKey(competitionId, workId))
}

export function setWorkAnalysis(competitionId, workId, text) {
  if (competitionId === null || competitionId === undefined || competitionId === '') return false
  if (workId === null || workId === undefined || workId === '') return false
  return write(workKey(competitionId, workId), text)
}

/**
 * 相对时间描述，用于 UI 上标注"缓存于 x 分钟前"
 * @param {number} savedAt 毫秒时间戳
 */
export function formatSavedAt(savedAt) {
  if (!savedAt) return ''
  const diff = Date.now() - savedAt
  if (diff < 0) return '刚刚'
  const min = Math.floor(diff / 60000)
  if (min < 1) return '刚刚'
  if (min < 60) return `${min} 分钟前`
  const hour = Math.floor(min / 60)
  if (hour < 24) return `${hour} 小时前`
  const day = Math.floor(hour / 24)
  if (day < 30) return `${day} 天前`
  return new Date(savedAt).toLocaleDateString()
}
