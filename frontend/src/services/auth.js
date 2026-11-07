import { reactive } from 'vue'
import api from './api'
import { clearUserLocalData } from './localUserData'

/**
 * 鉴权服务
 * ------------------------------------------------------------------
 * 后端契约（已与后端确认）：
 *   POST /api/auth/login    body: { username, password }  -> { access_token, token_type, expires_in }
 *   POST /api/auth/register body: { username, password }  -> { access_token, token_type, expires_in }
 *   GET  /api/auth/me       header: Authorization: Bearer <token> -> { ...用户信息 }
 * Token 放 localStorage，请求拦截器（api.js）自动注入 Authorization 头。
 */

const TOKEN_KEY = 'access_token'

/* ------------------------------------------------------------------ */
/* localStorage 读写（统一入口，方便日后改造）                          */
/* ------------------------------------------------------------------ */

export function getToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || ''
  } catch {
    // 隐私模式 / 禁用存储时兜底，避免整个应用崩掉
    return ''
  }
}

export function setToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token)
    else localStorage.removeItem(TOKEN_KEY)
  } catch {
    /* 忽略存储异常 */
  }
}

export function clearToken() {
  setToken('')
}

/* ------------------------------------------------------------------ */
/* 全局登录态：跨组件共享，Header / AuthModal 都读它                     */
/* ------------------------------------------------------------------ */

export const authState = reactive({
  user: null,          // /api/auth/me 返回的用户信息
  initialized: false,  // 是否已尝试过恢复登录态（避免重复请求 me）
  loading: false
})

/**
 * 本地是否有 token（同步、不发请求）
 * 用于路由守卫快速判断；token 是否真实有效由 api.js 的 401 拦截器兜底。
 */
export function hasToken() {
  return Boolean(getToken())
}

export function isLoggedIn() {
  return Boolean(authState.user) && Boolean(getToken())
}

/**
 * 登录成功后统一处理：落库 token + 拉取用户信息
 * @param {{ access_token: string }} data
 */
async function applyAuthResult(data) {
  const token = typeof data?.access_token === 'string' ? data.access_token : ''
  if (!token) throw new Error('登录响应缺少 access_token')

  setToken(token)
  await fetchMe()
  return authState.user
}

/* ------------------------------------------------------------------ */
/* 接口调用                                                            */
/* ------------------------------------------------------------------ */

/**
 * 登录
 * @param {{ username: string, password: string }} payload
 */
export async function login(payload = {}) {
  const username = typeof payload.username === 'string' ? payload.username.trim() : ''
  const password = typeof payload.password === 'string' ? payload.password : ''

  if (!username) throw new Error('请输入用户名')
  if (!password) throw new Error('请输入密码')

  const data = await api.post('/auth/login', { username, password })
  return applyAuthResult(data)
}

/**
 * 注册（后端注册成功直接返回 token，无需再调 login）
 * @param {{ username: string, password: string }} payload
 */
export async function register(payload = {}) {
  const username = typeof payload.username === 'string' ? payload.username.trim() : ''
  const password = typeof payload.password === 'string' ? payload.password : ''

  if (!username) throw new Error('请输入用户名')
  if (!password) throw new Error('请输入密码')

  const data = await api.post('/auth/register', { username, password })
  return applyAuthResult(data)
}

/**
 * 拉取当前用户信息（依赖拦截器注入的 token）
 */
export async function fetchMe() {
  const data = await api.get('/auth/me')
  authState.user = data && typeof data === 'object' ? data : null
  authState.initialized = true
  return authState.user
}

/**
 * 应用启动时恢复登录态：
 * 有 token 就调 /api/auth/me 验证一次，失效则清掉。
 * 失败不抛错（静默降级为未登录），避免影响首屏。
 */
export async function restoreSession() {
  if (authState.initialized || authState.loading) return authState.user

  const token = getToken()
  if (!token) {
    authState.initialized = true
    return null
  }

  authState.loading = true
  try {
    return await fetchMe()
  } catch {
    clearToken()
    authState.user = null
    authState.initialized = true
    return null
  } finally {
    authState.loading = false
  }
}

/**
 * 退出登录：清 token + 清内存状态 + 清本地用户数据
 *
 * 本地缓存（备赛计划 / 对话 / 灵感收藏）都只属于当前账号，
 * 退出时必须一并删除，避免下一位使用者或下一账号读到。
 */
export function logout() {
  clearToken()
  authState.user = null
  authState.initialized = true
  clearUserLocalData()
}
