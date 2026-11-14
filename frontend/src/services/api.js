import axios from 'axios'
import { clearUserLocalData } from './localUserData'
import { clearToken, getToken } from './tokenStore'

/**
 * 全局 axios 实例
 * - 生产：默认走同源 /api，由 Nginx 反向代理到后端，避免 CORS 与 Mixed Content
 * - 开发：vite.config.js 已把 /api 代理到 http://127.0.0.1:8000，
 *   也可用 VITE_API_BASE_URL 指向完整地址（需包含 /api 前缀，如 http://127.0.0.1:8000/api）
 */
const api = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL || '/api',
  timeout: 60000,
  headers: {
    'Content-Type': 'application/json'
  }
})

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

/**
 * 请求拦截器：自动携带 Token
 * 登录/注册等公开接口无需鉴权，这里统一判断：有 token 就带上。
 * 使用 config.headers.set 兼容 AxiosHeaders，避免直接赋值被覆盖。
 */
api.interceptors.request.use(
  (config) => {
    const token = getToken()
    if (token) {
      if (typeof config.headers?.set === 'function') {
        config.headers.set('Authorization', `Bearer ${token}`)
      } else {
        config.headers = config.headers || {}
        config.headers.Authorization = `Bearer ${token}`
      }
    }
    return config
  },
  (error) => Promise.reject(error)
)

api.interceptors.response.use(
  (response) => response.data,
  (error) => {
    let message = '网络异常，请稍后重试'

    if (error.response) {
      // 服务端有响应但状态码非 2xx
      const { status, data } = error.response

      // Token 失效 / 未登录：清掉本地凭证与本地用户数据，并通知上层
      if (status === 401) {
        const requestUrl = String(error.config?.url || '')
        // 登录 / 注册接口的 401 表示“账号密码错误”，不是会话失效。
        // 此时不能清本地计划与收藏（用户可能只是重新登录时输错密码），
        // 也不能派发 auth:unauthorized，否则会被上层当成“被踢下线”。
        const isCredentialRequest =
          requestUrl.includes('/auth/login') || requestUrl.includes('/auth/register')

        clearToken()

        if (!isCredentialRequest) {
          // 计划 / 对话 / 收藏都只属于当前登录用户，凭证失效时必须一并清空，
          // 否则换账号或共享设备会读到上一个人的数据。
          clearUserLocalData()
          if (typeof window !== 'undefined') {
            window.dispatchEvent(new CustomEvent('auth:unauthorized'))
          }
        }
      }

      // AI 每日配额用尽：派发全局事件，由 App.vue 里的 Toast 监听并提示
      // 后端此时返回 { detail: "..." }，文案直接用 detail，不在前端硬编码。
      // 攻略生成接口的 429 带的是 { status, reason } 而不是 detail（限流原因
      // 由详情页按钮就地提示），这里用 detail 是否存在来区分，避免误弹 Toast。
      if (status === 429 && normalizeDetail(data?.detail)) {
        if (typeof window !== 'undefined') {
          window.dispatchEvent(
            new CustomEvent('ai:quota-exceeded', {
              detail: { message: normalizeDetail(data?.detail) }
            })
          )
        }
      }

      message = normalizeDetail(data?.detail) || data?.message || `请求失败（${status}）`

      // 把 HTTP 状态码挂到 Error 上：调用方（如攻略生成按钮）需要区分
      // 401 / 409 / 429，而拦截器统一抛的是 Error 文案。
      const err = new Error(message)
      err.status = status
      err.data = data
      return Promise.reject(err)
    } else if (error.code === 'ECONNABORTED') {
      message = '请求超时，请检查后端服务是否已启动'
    } else if (error.request) {
      message = '无法连接服务器，请检查网络或稍后重试'
    }

    return Promise.reject(new Error(message))
  }
)

/**
 * 查询当前用户今日 AI 配额（只读，不消耗次数）
 *
 * 后端返回：{ used, limit, remaining }
 * - remaining 为 null 表示不限次数（后端 AI_DAILY_QUOTA=0）
 * - 该接口可在页面加载时安全调用
 *
 * @returns {Promise<{ used: number, limit: number, remaining: number|null }>}
 */
export function getAiQuota() {
  return api.get('/ai/quota').then((data) => ({
    used: Number(data?.used) || 0,
    limit: Number(data?.limit) || 0,
    remaining:
      data?.remaining === null || data?.remaining === undefined
        ? null
        : Number(data.remaining)
  }))
}

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
  return api.get('/competitions', { params }).then((data) => {
    // 兼容后端返回 { data: [...] } / { items: [...] } 或直接数组
    if (Array.isArray(data)) return data
    if (Array.isArray(data?.data)) return data.data
    if (Array.isArray(data?.items)) return data.items
    return []
  })
}

/**
 * 获取首页热门竞赛（首页专属接口）
 *
 * GET /api/competitions/hot?limit=8
 *
 * 后端已按「国家级 + 报名中/未开始 + 越早截止越靠前」排好序并限流，
 * 前端**不要**再做二次排序或截断，直接按返回顺序渲染。
 *
 * @param {number} [limit=8] 返回条数（1~50）
 * @returns {Promise<Array>} 竞赛数组（顺序即展示顺序）
 */
export function getHotCompetitions(limit = 8) {
  const n = Number(limit)
  const params = Number.isInteger(n) && n > 0 ? { limit: n } : {}
  return api.get('/competitions/hot', { params }).then((data) => {
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

  return api.post('/ai/search', { query: q }).then((data) => {
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
  return api.get(`/competitions/${cid}`).then((data) => {
    if (data && typeof data === 'object') {
      if (data.data && typeof data.data === 'object') return data.data
      return data
    }
    return {}
  })
}

/**
 * 按需生成参赛攻略（登录 + 异步）
 * POST /api/competitions/:id/generate_guide
 *
 * 后端语义：
 *   - 200 { status: 'ready', guide_length }        已有攻略，无需生成
 *   - 202 { status: 'queued'|'running', job_id }   已入队/已在跑（幂等）
 *   - 409 { status: 'failed', reason: 'cooldown' } 刚失败过，冷却中
 *   - 429 { status: 'throttled', reason }          预算/队列限流
 *   - 401                                           未登录（拦截器会清 token）
 *
 * @param {string|number} competitionId
 * @returns {Promise<{status:string, job_id:number|null, poll_after_ms:number}>}
 */
export function requestGuide(competitionId) {
  const cid = normalizeCompetitionId(competitionId)
  if (cid === null) return Promise.reject(new Error('竞赛 ID 无效'))
  return api.post(`/competitions/${cid}/generate_guide`).then((data) => ({
    status: String(data?.status || 'queued'),
    job_id: data?.job_id ?? null,
    guide_length: data?.guide_length ?? 0,
    reason: data?.reason ?? null,
    poll_after_ms: Number(data?.poll_after_ms) || 0
  }))
}

/**
 * 查询攻略生成状态（登录，只读）
 * GET /api/competitions/:id/generate_guide
 * @param {string|number} competitionId
 * @returns {Promise<{status:string, job_id:number|null, guide_length:number}>}
 */
export function getGuideStatus(competitionId) {
  const cid = normalizeCompetitionId(competitionId)
  if (cid === null) return Promise.reject(new Error('竞赛 ID 无效'))
  return api.get(`/competitions/${cid}/generate_guide`).then((data) => ({
    status: String(data?.status || 'idle'),
    job_id: data?.job_id ?? null,
    attempts: Number(data?.attempts) || 0,
    guide_length: Number(data?.guide_length) || 0,
    generated_at: data?.generated_at ?? null
  }))
}

/**
 * 按需生成专属灵感（登录 + 异步）
 * POST /api/competitions/:id/generate_inspirations
 *
 * 灵感全部来自该赛事获奖作品，后端在入库前就会检查 awards 数量：
 *   - 200 { status: 'ready' }                          已有灵感，无需生成
 *   - 202 { status: 'queued'|'running', job_id }       已入队/已在跑（幂等）
 *   - 409 { status: 'no_awards', reason: 'no_awards' } 没有获奖作品，无素材可提炼
 *   - 409 { reason: 'cooldown' }                       刚失败过，冷却中
 *   - 429 { reason: 'queue_full'|'daily_budget' }      限流
 *   - 401                                              未登录（拦截器会清 token）
 *
 * @param {string|number} competitionId
 * @returns {Promise<{status:string, job_id:number|null, awards_count:number, reason:string|null, poll_after_ms:number}>}
 */
export function requestInspirations(competitionId) {
  const cid = normalizeCompetitionId(competitionId)
  if (cid === null) return Promise.reject(new Error('竞赛 ID 无效'))
  return api.post(`/competitions/${cid}/generate_inspirations`).then((data) => ({
    status: String(data?.status || 'queued'),
    job_id: data?.job_id ?? null,
    awards_count: Number(data?.awards_count) || 0,
    insights_count: Number(data?.insights_count) || 0,
    ideas_count: Number(data?.ideas_count) || 0,
    reason: data?.reason ?? null,
    poll_after_ms: Number(data?.poll_after_ms) || 0
  }))
}

/**
 * 查询灵感生成状态（登录，只读）
 * GET /api/competitions/:id/generate_inspirations
 * @param {string|number} competitionId
 * @returns {Promise<{status:string, job_id:number|null, awards_count:number, insights_count:number, ideas_count:number}>}
 */
export function getInspirationStatus(competitionId) {
  const cid = normalizeCompetitionId(competitionId)
  if (cid === null) return Promise.reject(new Error('竞赛 ID 无效'))
  return api.get(`/competitions/${cid}/generate_inspirations`).then((data) => ({
    status: String(data?.status || 'idle'),
    job_id: data?.job_id ?? null,
    attempts: Number(data?.attempts) || 0,
    awards_count: Number(data?.awards_count) || 0,
    insights_count: Number(data?.insights_count) || 0,
    ideas_count: Number(data?.ideas_count) || 0,
    error: data?.error ?? null,
    generated_at: data?.generated_at ?? null
  }))
}

/**
 * 单个获奖作品 AI 解析
 * POST /api/ai/analyze-work  body: { work_id, competition_id }
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

  return api.post('/ai/analyze-work', body).then((data) => ({
    analysis: typeof data?.analysis === 'string' ? data.analysis : ''
  }))
}

/**
 * 竞赛级获奖作品聚合分析（一键分析全部获奖作品的共性）
 * POST /api/ai/analyze  body: { competition_id }
 *
 * @param {string|number} competitionId 竞赛 ID
 * @returns {Promise<{ analysis: string }>}
 */
export function analyzeWorks(competitionId) {
  const cid = normalizeCompetitionId(competitionId)
  if (cid === null) return Promise.reject(new Error('竞赛 ID 无效'))

  return api.post('/ai/analyze', { competition_id: cid }).then((data) => ({
    analysis: typeof data?.analysis === 'string' ? data.analysis : ''
  }))
}

/**
 * 获取指定赛事的灵感数据（洞察 + 选题）
 * GET /api/ai/inspirations?competition_id=...
 *
 * 后端强制要求 competition_id，并按该赛事过滤全部 insights / ideas；
 * 不传或传非法 ID 会直接报错，绝不会返回全库混合数据。
 *
 * @param {string|number} competitionId 竞赛 ID
 * @returns {Promise<{ competition_id:number, competition_name:string, work_count:number, insights:Array, ideas:Array }>}
 */
export function getInspirations(competitionId) {
  const cid = normalizeCompetitionId(competitionId)
  if (cid === null) return Promise.reject(new Error('竞赛 ID 无效'))

  return api.get('/ai/inspirations', { params: { competition_id: cid } }).then((data) => ({
    competition_id: Number(data?.competition_id) || cid,
    competition_name: typeof data?.competition_name === 'string' ? data.competition_name : '',
    work_count: Number(data?.work_count) || 0,
    insights: Array.isArray(data?.insights) ? data.insights : [],
    ideas: Array.isArray(data?.ideas) ? data.ideas : []
  }))
}

/**
 * 获取某竞赛的往年获奖作品
 * GET /api/competitions/:id/awards?limit=20&offset=0
 *
 * @param {string|number} competitionId 竞赛 ID
 * @param {Object} [options]
 * @param {number} [options.limit] 返回条数上限
 * @param {number} [options.offset] 跳过条数（「加载更多」分页用）
 * @returns {Promise<{ items: Array, total: number, hasMore: boolean }>}
 */
export function getCompetitionAwards(competitionId, options = {}) {
  const cid = normalizeCompetitionId(competitionId)
  if (cid === null) return Promise.reject(new Error('竞赛 ID 无效'))

  const params = {}
  const limit = Number(options.limit)
  if (Number.isInteger(limit) && limit > 0) params.limit = limit
  const offset = Number(options.offset)
  if (Number.isInteger(offset) && offset > 0) params.offset = offset

  return api.get(`/competitions/${cid}/awards`, { params }).then((data) => {
    const items = Array.isArray(data?.items)
      ? data.items
      : Array.isArray(data?.data)
        ? data.data
        : Array.isArray(data)
          ? data
          : []
    const total = Number(data?.total)
    const safeTotal = Number.isFinite(total) && total >= 0 ? total : items.length
    return { items, total: safeTotal, hasMore: items.length > 0 }
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

  return api.post('/ai/plan', body).then((data) => {
    const status = typeof data?.status === 'string' ? data.status : 'plan'
    return {
      status,
      reply: typeof data?.reply === 'string' ? data.reply : ''
    }
  })
}

/**
 * 提交用户反馈
 * POST /api/feedback  body: { content }
 *
 * 注意：axios 实例的 baseURL 已经是 '/api'，这里只能写 '/feedback'，
 * 写成 '/api/feedback' 会被拼成 '/api/api/feedback' -> 404 Not Found。
 *
 * @param {string} content 反馈正文
 * @returns {Promise<{ message: string, id?: number }>}
 */
export function submitFeedback(content) {
  const text = typeof content === 'string' ? content.trim() : ''
  if (!text) return Promise.reject(new Error('请输入反馈内容'))
  if (text.length > 1000) return Promise.reject(new Error('反馈内容不能超过 1000 字'))

  return api.post('/feedback', { content: text }).then((data) => ({
    message: typeof data?.message === 'string' ? data.message : '感谢你的反馈',
    id: Number.isInteger(data?.id) ? data.id : undefined
  }))
}

// ---------------------------------------------------------------- preparations

/**
 * List the current user's prep items (newest first).
 * Requires auth; the interceptor already unwraps to the response body.
 */
export function getMyPreparations() {
  return api.get('/preparations').then((data) => ({
    total: Number.isFinite(data?.total)
      ? data.total
      : Array.isArray(data?.items)
        ? data.items.length
        : 0,
    items: Array.isArray(data?.items) ? data.items : []
  }))
}

/** Whether a competition is already in my prep list. Always resolves to a boolean. */
export function checkPrepared(competitionId) {
  const cid = normalizeCompetitionId(competitionId)
  if (cid === null) return Promise.resolve(false)
  return api
    .get(`/preparations/check/${cid}`)
    .then((data) => data?.prepared === true)
    .catch(() => false)
}

/** Add a competition to my prep list (idempotent on the server). */
export function addPreparation(competitionId) {
  const cid = normalizeCompetitionId(competitionId)
  if (cid === null) return Promise.reject(new Error('竞赛 ID 无效'))
  return api.post(`/preparations/${cid}`)
}

/** Remove a competition from my prep list. */
export function removePreparation(competitionId) {
  const cid = normalizeCompetitionId(competitionId)
  if (cid === null) return Promise.reject(new Error('竞赛 ID 无效'))
  return api.delete(`/preparations/${cid}`)
}

// ---------------------------------------------------------------- admin

/**
 * 管理后台：待审候选赛事列表
 * GET /api/admin/candidates?limit=&offset=
 *
 * 后端已用 get_current_admin 把关，401 = 未登录，403 = 不是管理员。
 *
 * @param {{ limit?: number, offset?: number }} [params]
 * @returns {Promise<{ total: number, items: Array }>}
 */
export function getAdminCandidates(params = {}) {
  const query = {}
  if (Number.isInteger(params.limit)) query.limit = params.limit
  if (Number.isInteger(params.offset)) query.offset = params.offset

  return api.get('/admin/candidates', { params: query }).then((data) => ({
    total: Number.isFinite(data?.total) ? data.total : 0,
    items: Array.isArray(data?.items) ? data.items : []
  }))
}

/** 管理后台：通过候选（转入正式赛事） POST /api/admin/candidates/:id/approve */
export function approveAdminCandidate(candidateId) {
  const id = Number(candidateId)
  if (!Number.isInteger(id) || id <= 0) return Promise.reject(new Error('候选 ID 无效'))
  // 通过后会同步补全「2 字简称 + 赛氪详情（抓取 + AI 提纯）」，常见 20~60 秒，
  // 因此单独放宽超时，避免默认 60s 把已经入库成功的请求判成失败。
  return api.post(`/admin/candidates/${id}/approve`, null, { timeout: 180000 })
}

/** 管理后台：拒绝候选 POST /api/admin/candidates/:id/reject */
export function rejectAdminCandidate(candidateId) {
  const id = Number(candidateId)
  if (!Number.isInteger(id) || id <= 0) return Promise.reject(new Error('候选 ID 无效'))
  return api.post(`/admin/candidates/${id}/reject`)
}

/**
 * 管理后台：全部用户反馈
 * GET /api/admin/feedback?limit=&offset=
 *
 * @returns {Promise<{ items: Array, unread: number }>}
 */
export function getAdminFeedback(params = {}) {
  const query = {}
  if (Number.isInteger(params.limit)) query.limit = params.limit
  if (Number.isInteger(params.offset)) query.offset = params.offset

  return api.get('/admin/feedback', { params: query }).then((data) => ({
    items: Array.isArray(data?.items) ? data.items : [],
    unread: Number.isFinite(data?.unread) ? data.unread : 0
  }))
}

/** 管理后台：标记反馈已读 PUT /api/admin/feedback/:id/read */
export function markAdminFeedbackRead(feedbackId) {
  const id = Number(feedbackId)
  if (!Number.isInteger(id) || id <= 0) return Promise.reject(new Error('反馈 ID 无效'))
  return api.put(`/admin/feedback/${id}/read`)
}

/**
 * 管理后台：正式赛事列表（「赛事管理」模块）
 * GET /api/admin/competitions?limit=&offset=&keyword=
 *
 * @param {{ limit?: number, offset?: number, keyword?: string }} [params]
 * @returns {Promise<{ total: number, items: Array }>}
 */
export function getAdminCompetitions(params = {}) {
  const query = {}
  if (Number.isInteger(params.limit)) query.limit = params.limit
  if (Number.isInteger(params.offset)) query.offset = params.offset
  if (typeof params.keyword === 'string' && params.keyword.trim()) {
    query.keyword = params.keyword.trim()
  }

  return api.get('/admin/competitions', { params: query }).then((data) => ({
    total: Number.isFinite(data?.total) ? data.total : 0,
    items: Array.isArray(data?.items) ? data.items : []
  }))
}

/**
 * 管理后台：删除赛事（级联清理关联数据）
 * DELETE /api/admin/competitions/:id
 *
 * @returns {Promise<{ name: string, deleted: object, message: string }>}
 */
export function deleteAdminCompetition(competitionId) {
  const id = Number(competitionId)
  if (!Number.isInteger(id) || id <= 0) return Promise.reject(new Error('赛事 ID 无效'))
  return api.delete(`/admin/competitions/${id}`)
}

export default api
