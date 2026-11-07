/**
 * 备赛计划本地持久化（localStorage）
 *
 * key = plan_${competitionId}
 * 存 { plan, messages, history, savedAt }：
 *   - plan     ：当前完整计划 Markdown 正文
 *   - messages ：对话气泡（仅追问 / 提示，不含计划正文）
 *   - history  ：多轮对话压缩成的 { user, assistant } 轮次
 *   - savedAt  ：写入时间戳，用于展示"已保存到本地 · x 分钟前"
 *
 * 与 AI 解析结果不同，备赛计划仍保存在前端本地：
 * 隐私模式 / 配额满 / 数据腐坏一律静默降级为"无缓存"，
 * 绝不能因为缓存异常阻断主流程。
 */

const PLAN_PREFIX = 'plan_'

/** 单条记录最多保留的对话数量，避免 localStorage 被超长对话撑爆 */
const MAX_MESSAGES = 200

/** @returns {Storage|null} */
function store() {
  try {
    if (typeof localStorage === 'undefined') return null
    // 探测可用性：Safari 隐私模式下 setItem 会抛异常
    const probe = '__plan_probe__'
    localStorage.setItem(probe, '1')
    localStorage.removeItem(probe)
    return localStorage
  } catch (e) {
    return null
  }
}

/** 只保留结构合法的对话气泡（role + 非空 content） */
function normalizeMessages(raw) {
  if (!Array.isArray(raw)) return []
  return raw
    .filter(
      (m) =>
        m &&
        (m.role === 'user' || m.role === 'assistant') &&
        typeof m.content === 'string' &&
        m.content.trim()
    )
    .slice(-MAX_MESSAGES)
    .map((m) => ({ role: m.role, content: m.content }))
}

/** 只保留结构合法的轮次记录（user + assistant 均为字符串） */
function normalizeHistory(raw) {
  if (!Array.isArray(raw)) return []
  return raw
    .filter((h) => h && typeof h.user === 'string' && typeof h.assistant === 'string')
    .slice(-MAX_MESSAGES)
    .map((h) => ({ user: h.user, assistant: h.assistant }))
}

export function planKey(competitionId) {
  return `${PLAN_PREFIX}${competitionId}`
}

/**
 * 读取某场竞赛的备赛记录
 * @param {string|number} competitionId
 * @returns {{ plan: string, messages: Array, history: Array, savedAt: number }|null}
 */
export function getPlanCache(competitionId) {
  if (competitionId === null || competitionId === undefined || competitionId === '') return null
  const ls = store()
  if (!ls) return null
  try {
    const raw = ls.getItem(planKey(competitionId))
    if (!raw) return null
    const parsed = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object') return null

    const plan = typeof parsed.plan === 'string' ? parsed.plan : ''
    const messages = normalizeMessages(parsed.messages)
    const history = normalizeHistory(parsed.history)

    // 全部为空视为无效缓存（例如写入过程中被中断）
    if (!plan.trim() && !messages.length && !history.length) return null

    return {
      plan,
      messages,
      history,
      savedAt: Number.isFinite(parsed.savedAt) ? parsed.savedAt : 0
    }
  } catch (e) {
    return null
  }
}

/**
 * 写入某场竞赛的备赛记录（超配额时静默失败）
 * @param {string|number} competitionId
 * @param {{ plan?: string, messages?: Array, history?: Array }} data
 * @returns {boolean} 是否写入成功
 */
export function setPlanCache(competitionId, data = {}) {
  if (competitionId === null || competitionId === undefined || competitionId === '') return false
  const ls = store()
  if (!ls) return false

  const payload = {
    plan: typeof data.plan === 'string' ? data.plan : '',
    messages: normalizeMessages(data.messages),
    history: normalizeHistory(data.history),
    savedAt: Date.now()
  }

  if (!payload.plan.trim() && !payload.messages.length && !payload.history.length) return false

  try {
    ls.setItem(planKey(competitionId), JSON.stringify(payload))
    return true
  } catch (e) {
    return false
  }
}

/**
 * 清空本机全部备赛缓存（退出登录 / 401 时使用）
 *
 * 先收集 key 再统一删除，避免边遍历边删除导致索引错位漏删。
 * 存储不可用或读取异常时静默返回 false，不阻断登出主流程。
 * @returns {boolean} 是否执行成功
 */
export function clearAllPlanCaches() {
  const ls = store()
  if (!ls) return false
  try {
    const keys = []
    for (let i = 0; i < ls.length; i++) {
      const key = ls.key(i)
      if (typeof key === 'string' && key.startsWith(PLAN_PREFIX)) keys.push(key)
    }
    keys.forEach((key) => ls.removeItem(key))
    return true
  } catch (e) {
    return false
  }
}

/** 清空某场竞赛的备赛记录（重新生成计划时使用） */
export function clearPlanCache(competitionId) {
  if (competitionId === null || competitionId === undefined || competitionId === '') return false
  const ls = store()
  if (!ls) return false
  try {
    ls.removeItem(planKey(competitionId))
    return true
  } catch (e) {
    return false
  }
}
