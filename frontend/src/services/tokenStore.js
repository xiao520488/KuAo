/**
 * JWT 访问令牌存储（P0 账号串号修复）
 * ------------------------------------------------------------------
 * 旧实现把 access_token 放在 localStorage：
 *   - localStorage 按「源」共享，同一浏览器的所有标签页 / 窗口（包括
 *     同一无痕会话里的多个无痕窗口）读到的是同一个 token；
 *   - 朋友 A 登录后，朋友 B 新开窗口访问站点，会直接复用 A 的 token，
 *     调 /api/auth/me 拿到的就是 A 的身份 —— 表现即「B 登入了 A 的账号」。
 *
 * 现在改为 sessionStorage（按标签页隔离）：
 *   - 每个标签页 / 窗口各自持有 token，A、B 可以同时登录不同账号；
 *   - 刷新页面仍保持登录；关闭标签页即失效（安全优先）；
 *   - sessionStorage 不可用时退化为当前模块内存变量，至少不跨窗口串号。
 *
 * 同时每次读写都会清掉旧版留在 localStorage 的 access_token，避免升级后
 * 旧 token 继续被任意窗口复用（也起到强制全员重新登录的止血作用）。
 */

const TOKEN_KEY = 'access_token'

/** sessionStorage 不可用时的兜底（仅当前标签页内存有效） */
let memoryToken = ''

function getSessionStorage() {
  try {
    if (typeof sessionStorage === 'undefined') return null
    return sessionStorage
  } catch (e) {
    // 隐私模式 / 禁用存储
    return null
  }
}

/** 读取当前标签页的 token；没有则返回空串。 */
export function getToken() {
  const ss = getSessionStorage()
  if (ss) {
    try {
      return ss.getItem(TOKEN_KEY) || memoryToken
    } catch (e) {
      return memoryToken
    }
  }
  return memoryToken
}

/**
 * 写入 / 清除当前标签页的 token。
 * 同时清理旧版 localStorage token，杜绝跨窗口复用。
 */
export function setToken(token) {
  const value = typeof token === 'string' ? token : ''
  memoryToken = value

  const ss = getSessionStorage()
  if (ss) {
    try {
      if (value) ss.setItem(TOKEN_KEY, value)
      else ss.removeItem(TOKEN_KEY)
    } catch (e) {
      /* 存储异常时保留内存 token，不阻断登录流程 */
    }
  }

  purgeLegacyToken()
}

export function clearToken() {
  setToken('')
}

/** 删除旧版 localStorage 中的 token（升级 / 启动 / 登录时调用）。 */
export function purgeLegacyToken() {
  try {
    if (typeof localStorage !== 'undefined') localStorage.removeItem(TOKEN_KEY)
  } catch (e) {
    /* 忽略存储异常 */
  }
}

/**
 * 从 JWT payload 读取 sub（用户 ID），仅用于「本地缓存按用户分区」，
 * 不做签名校验（签名由后端负责；这里只决定浏览器本地数据的命名空间）。
 *
 * 不用 /api/auth/me 是因为它异步：页面初始化时本地缓存可能先于它读取。
 */
export function getTokenUserId() {
  const token = getToken()
  if (!token) return ''

  const parts = token.split('.')
  if (parts.length < 2) return ''

  try {
    if (typeof atob !== 'function') return ''
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const padded = base64 + '='.repeat((4 - (base64.length % 4)) % 4)
    const payload = JSON.parse(atob(padded))
    const sub = payload && payload.sub
    return sub === null || sub === undefined ? '' : String(sub)
  } catch (e) {
    return ''
  }
}
