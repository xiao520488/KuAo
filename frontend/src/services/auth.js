import { reactive } from 'vue'
import api, { refreshAccessToken } from './api'
import { clearUserLocalData } from './localUserData'
import {
  clearToken,
  getToken,
  getTokenUserId,
  purgeLegacyToken,
  setToken
} from './tokenStore'

/**
 * 鉴权服务
 * ------------------------------------------------------------------
 * 后端契约（已与后端确认）：
 *   GET  /api/auth/captcha  -> { captcha_key, captcha_image(data URL), expires_in }
 *   POST /api/auth/login    body: { username, password, captcha_key, captcha_code } -> { access_token, ... }
 *   POST /api/auth/register body: { username, password, captcha_key, captcha_code } -> { access_token, ... }
 *   GET  /api/auth/me       header: Authorization: Bearer <token> -> { ...用户信息 }
 *
 * Token 存 sessionStorage（按标签页隔离），请求拦截器（api.js）自动注入。
 * 旧版曾存 localStorage，会跨窗口共享导致账号串号，详见 tokenStore.js。
 */
export { clearToken, getToken, getTokenUserId, purgeLegacyToken, setToken }

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

/**
 * 归一化登录 / 注册入参，并做前端必填校验，避免把空值发给后端。
 * @returns {{ username: string, password: string, captchaKey: string, captchaCode: string }}
 */
function readCredentialPayload(payload = {}) {
  const username = typeof payload.username === 'string' ? payload.username.trim() : ''
  const password = typeof payload.password === 'string' ? payload.password : ''
  const captchaKey = typeof payload.captchaKey === 'string' ? payload.captchaKey : ''
  const captchaCode = typeof payload.captchaCode === 'string' ? payload.captchaCode.trim() : ''

  if (!username) throw new Error('请输入用户名')
  if (!password) throw new Error('请输入密码')

  return { username, password, captchaKey, captchaCode }
}

/* ------------------------------------------------------------------ */
/* 接口调用                                                            */
/* ------------------------------------------------------------------ */

/**
 * 登录
 * @param {{ username: string, password: string, captchaKey: string, captchaCode: string }} payload
 */
export async function login(payload = {}) {
  const { username, password, captchaKey, captchaCode } = readCredentialPayload(payload)
  if (!captchaKey) throw new Error('验证码已失效，请点击图片刷新')
  if (!captchaCode) throw new Error('请输入验证码')

  const data = await api.post('/auth/login', {
    username,
    password,
    captcha_key: captchaKey,
    captcha_code: captchaCode
  })
  return applyAuthResult(data)
}

/**
 * 注册（后端注册成功直接返回 token，无需再调 login）
 * @param {{ username: string, password: string, captchaKey: string, captchaCode: string }} payload
 */
export async function register(payload = {}) {
  const { username, password, captchaKey, captchaCode } = readCredentialPayload(payload)
  if (!captchaKey) throw new Error('验证码已失效，请点击图片刷新')
  if (!captchaCode) throw new Error('请输入验证码')

  const data = await api.post('/auth/register', {
    username,
    password,
    captcha_key: captchaKey,
    captcha_code: captchaCode
  })
  return applyAuthResult(data)
}

/**
 * 领取一张图形验证码
 *
 * 后端返回 { captcha_key, captcha_image, expires_in }：
 * captcha_image 是 data:image/png;base64,...，可直接绑到 <img :src>，
 * 因此不需要额外的图片域名，也不会有跨域 / 混合内容问题。
 *
 * @returns {Promise<{ captcha_key: string, captcha_image: string, expires_in: number }>}
 */
export async function fetchCaptcha() {
  const data = await api.get('/auth/captcha')
  if (!data || typeof data.captcha_image !== 'string' || !data.captcha_key) {
    throw new Error('验证码加载失败，请重试')
  }
  return data
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
 *
 * 并发去重：main.js 启动时会调一次，路由守卫进入 /admin 时也可能调一次，
 * 两边若各发一次请求，后发的那次会因 loading=true 提前返回 null，
 * 让守卫误判「未登录」把管理员挡在门外。这里用 inflight 共享同一个 Promise，
 * 所有调用方都等到真实结果。
 */
let inflightRestore = null

export async function restoreSession() {
  if (authState.initialized || authState.user) return authState.user
  if (inflightRestore) return inflightRestore

  authState.loading = true
  inflightRestore = (async () => {
    try {
      // 没有 access token 不代表没登录：refresh Cookie 还在就能静默换新。
      // 这正是「关掉浏览器再打开不用重新登录」的实现点。
      let token = getToken()
      if (!token) token = await refreshAccessToken()
      if (!token) {
        authState.user = null
        authState.initialized = true
        return null
      }
      return await fetchMe()
    } catch {
      clearToken()
      authState.user = null
      authState.initialized = true
      return null
    } finally {
      authState.loading = false
      inflightRestore = null
    }
  })()

  return inflightRestore
}

/**
 * 退出登录：清 token + 清内存状态 + 清本地用户数据
 *
 * 本地缓存（备赛计划 / 对话 / 灵感收藏）都只属于当前账号，
 * 退出时必须一并删除，避免下一位使用者或下一账号读到。
 */
export function logout() {
  // 通知后端清掉 refresh Cookie（失败不影响本地登出）
  api.post('/auth/logout').catch(() => {})
  clearToken()
  authState.user = null
  authState.initialized = true
  clearUserLocalData()
}
