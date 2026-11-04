import axios from 'axios'

/**
 * 全局 axios 实例
 * 后端 FastAPI 服务地址：http://127.0.0.1:8000
 */
const api = axios.create({
  baseURL: 'http://127.0.0.1:8000',
  timeout: 60000,
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器（预留：后续可注入 token）
api.interceptors.request.use(
  (config) => config,
  (error) => Promise.reject(error)
)

/**
 * 把 FastAPI 的错误 detail 归一化成可读字符串。
 * detail 可能是字符串（业务自定义），也可能是 422 校验错误数组，
 * 直接塞进 Error 会变成 "[object Object]"，这里逐项提取 msg。
 */
function normalizeDetail(detail) {
  if (typeof detail === 'string' && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => (typeof d === 'string' ? d : d?.msg))
      .filter((m) => typeof m === 'string' && m.trim())
    if (msgs.length) return msgs.join('；')
  }
  return ''
}

// 响应拦截器：统一剥掉 axios 的 data 包装，并归一化错误信息
api.interceptors.response.use(
  (response) => response.data,
  (error) => {
    let message = '网络异常，请稍后重试'

    if (error.response) {
      // 服务端有响应但状态码非 2xx
      const { status, data } = error.response
      message = normalizeDetail(data?.detail) || data?.message || `请求失败（${status}）`
    } else if (error.code === 'ECONNABORTED') {
      message = '请求超时，请检查后端服务是否已启动'
    } else if (error.request) {
      message = '无法连接服务器，请确认后端已在 127.0.0.1:8000 启动'
    }

    return Promise.reject(new Error(message))
  }
)

/**
 * 把外部传入的竞赛 ID 规范化为正整数。
 * 路由参数是字符串（如 "10"），也可能拿到 undefined / "undefined" / NaN，
 * 这些脏值一旦透传给后端，接口会返回错误的竞赛数据，因此在出入口统一拦截。
 * @param {*} id
 * @returns {number|null} 合法返回数字，非法返回 null
 */
export function normalizeCompetitionId(id) {
  if (id === null || id === undefined || id === '') return null
  const n = typeof id === 'number' ? id : Number(String(id).trim())
  return Number.isInteger(n) && n > 0 ? n : null
}

/**
 * 获取竞赛列表
 * @param {Object} params 可选查询参数
 * @returns {Promise<Array>} 竞赛数组
 */
export function getCompetitions(params = {}) {
  return api.get('/api/competitions', { params }).then((data) => {
    // 兼容后端返回 { data: [...] } / { items: [...] } 或直接数组
    if (Array.isArray(data)) return data
    if (Array.isArray(data?.data)) return data.data
    if (Array.isArray(data?.items)) return data.items
    return []
  })
}

/**
 * AI 语义搜索赛事
 * POST /api/ai/search  body: { query }
 *
 * 后端返回：{ query, total, items: [{ id, name, category, level, level_label,
 *            summary, official_url, score, matched_awards }] }
 *
 * @param {string} query 用户输入的自然语言描述
 * @returns {Promise<{ query: string, total: number, items: Array }>}
 */
export function searchCompetitions(query) {
  const q = typeof query === 'string' ? query.trim() : ''
  // 后端对空白 query 返回 422，本地先拦一道，省一次无效往返
  if (!q) return Promise.reject(new Error('请输入要搜索的赛事描述'))

  return api.post('/api/ai/search', { query: q }).then((data) => {
    const items = Array.isArray(data?.items)
      ? data.items
      : Array.isArray(data?.data)
        ? data.data
        : Array.isArray(data)
          ? data
          : []

    // 只保留能定位的条目，避免卡片 key 缺失
    const clean = items.filter(
      (it) => it && typeof it === 'object' && it.id !== null && it.id !== undefined
    )

    return {
      query: typeof data?.query === 'string' ? data.query : q,
      total: Number.isInteger(data?.total) ? data.total : clean.length,
      items: clean
    }
  })
}

/**
 * 获取竞赛详情
 * @param {string|number} id 竞赛 ID
 * @returns {Promise<Object>} 竞赛详情对象
 */
export function getCompetitionDetail(id) {
  const cid = normalizeCompetitionId(id)
  if (cid === null) return Promise.reject(new Error('竞赛 ID 无效'))
  return api.get(`/api/competitions/${cid}`).then((data) => {
    if (data && typeof data === 'object') {
      if (data.data && typeof data.data === 'object') return data.data
      return data
    }
    return {}
  })
}

/**
 * 单个获奖作品 AI 解析
 * POST /api/ai/analyze/work  body: { work_id, competition_id }
 *
 * @param {string|number} workId 作品 ID
 * @param {Object} [options]
 * @param {string|number} [options.competitionId] 所属竞赛 ID
 * @returns {Promise<{ analysis: string }>}
 */
export function analyzeWork(workId, options = {}) {
  const wid = normalizeCompetitionId(workId)
  if (wid === null) return Promise.reject(new Error('作品 ID 无效'))

  const cid = normalizeCompetitionId(options.competitionId)
  if (options.competitionId !== undefined && options.competitionId !== null && cid === null) {
    return Promise.reject(new Error('竞赛 ID 无效'))
  }

  const body = { work_id: wid }
  if (cid !== null) body.competition_id = cid

  return api.post('/api/ai/analyze/work', body).then((data) => ({
    analysis: typeof data?.analysis === 'string' ? data.analysis : ''
  }))
}

/**
 * 竞赛级获奖作品聚合分析（一键分析全部获奖作品的共性）
 * POST /api/ai/analyze/competition  body: { competition_id }
 *
 * @param {string|number} competitionId 竞赛 ID
 * @returns {Promise<{ analysis: string }>}
 */
export function analyzeWorks(competitionId) {
  const cid = normalizeCompetitionId(competitionId)
  if (cid === null) return Promise.reject(new Error('竞赛 ID 无效'))

  return api.post('/api/ai/analyze/competition', { competition_id: cid }).then((data) => ({
    analysis: typeof data?.analysis === 'string' ? data.analysis : ''
  }))
}

/**
 * 获取某竞赛的往年获奖作品
 * GET /api/competitions/:id/awards?limit=20
 *
 * @param {string|number} competitionId 竞赛 ID
 * @param {Object} [options]
 * @param {number} [options.limit] 返回条数上限
 * @returns {Promise<{ items: Array }>}
 */
export function getCompetitionAwards(competitionId, options = {}) {
  const cid = normalizeCompetitionId(competitionId)
  if (cid === null) return Promise.reject(new Error('竞赛 ID 无效'))

  const params = {}
  const limit = Number(options.limit)
  if (Number.isInteger(limit) && limit > 0) params.limit = limit

  return api.get(`/api/competitions/${cid}/awards`, { params }).then((data) => {
    const items = Array.isArray(data?.items)
      ? data.items
      : Array.isArray(data?.data)
        ? data.data
        : Array.isArray(data)
          ? data
          : []
    return { items }
  })
}

/**
 * 生成 / 优化备赛计划
 * POST /api/ai/plan  body: { competition_id, user_input, current_plan }
 *
 * @param {Object} payload
 * @param {string|number} payload.competitionId 竞赛 ID
 * @param {string} [payload.userInput] 用户本轮输入
 * @param {string|null} [payload.currentPlan] 当前计划正文（用于 refine）
 * @returns {Promise<{ status: 'plan'|'refine'|'ask', reply: string }>}
 */
export function generatePlan(payload = {}) {
  const cid = normalizeCompetitionId(payload.competitionId)
  if (cid === null) return Promise.reject(new Error('竞赛 ID 无效'))

  const body = {
    competition_id: cid,
    user_input: typeof payload.userInput === 'string' ? payload.userInput : '',
    current_plan:
      typeof payload.currentPlan === 'string' && payload.currentPlan.trim()
        ? payload.currentPlan
        : null
  }

  return api.post('/api/ai/plan', body).then((data) => {
    const status = typeof data?.status === 'string' ? data.status : 'plan'
    return {
      status,
      reply: typeof data?.reply === 'string' ? data.reply : ''
    }
  })
}

export default api
