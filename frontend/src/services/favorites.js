import { getTokenUserId } from './tokenStore'

/**
 * 备赛灵感收藏的本地持久化
 *
 * key = prep_favorites_v1_${userId}
 * ------------------------------------------------------------------
 * 旧版 key 是固定的 prep_favorites_v1，同一浏览器的所有账号共享，
 * A 收藏的灵感会出现在 B 的「我的收藏」里。现在按用户 ID 分区，
 * 并在启动 / 退出时清理旧版未分区的数据。
 *
 * 统一收口 key 与读写逻辑，避免散落在组件内：
 * - 读取时做结构校验，脏数据（对象/非字符串项）一律丢弃，防止渲染层 .includes 崩溃；
 * - 写入时只保留非空字符串并去重；
 * - 退出登录 / 401 时由 clearFavorites() 统一清理。
 */

const FAVORITES_PREFIX = 'prep_favorites_v1'

function favoritesKey() {
  const uid = getTokenUserId() || 'anon'
  return `${FAVORITES_PREFIX}_${uid}`
}

/** @returns {string[]} */
export function loadFavoriteIds() {
  try {
    if (typeof localStorage === 'undefined') return []
    const raw = localStorage.getItem(favoritesKey())
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return [...new Set(parsed.filter((id) => typeof id === 'string' && id.trim()))]
  } catch (e) {
    // 隐私模式 / JSON 损坏：降级为空收藏，绝不阻断页面
    return []
  }
}

/** @param {string[]} ids */
export function saveFavoriteIds(ids) {
  try {
    if (typeof localStorage === 'undefined') return false
    const clean = Array.isArray(ids)
      ? [...new Set(ids.filter((id) => typeof id === 'string' && id.trim()))]
      : []
    localStorage.setItem(favoritesKey(), JSON.stringify(clean))
    return true
  } catch (e) {
    return false
  }
}

/**
 * 清空本机收藏（退出登录 / 401 时调用）。
 * 删除所有 prep_favorites_v1* key，包含旧版未分区 key，避免残留。
 */
export function clearFavorites() {
  try {
    if (typeof localStorage === 'undefined') return false
    const keys = []
    for (let i = 0; i < localStorage.length; i++) {
      const key = localStorage.key(i)
      if (typeof key === 'string' && key.startsWith(FAVORITES_PREFIX)) keys.push(key)
    }
    keys.forEach((key) => localStorage.removeItem(key))
    return true
  } catch (e) {
    return false
  }
}

/** 清理旧版未按用户分区的固定 key（应用启动时调用一次）。 */
export function purgeLegacyFavorites() {
  try {
    if (typeof localStorage === 'undefined') return false
    localStorage.removeItem(FAVORITES_PREFIX)
    return true
  } catch (e) {
    return false
  }
}
