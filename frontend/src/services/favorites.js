/**
 * 备赛灵感收藏的本地持久化
 *
 * 统一收口 key 与读写逻辑，避免散落在组件内：
 * - 读取时做结构校验，脏数据（对象/非字符串项）一律丢弃，防止渲染层 .includes 崩溃；
 * - 写入时只保留非空字符串并去重；
 * - 退出登录 / 401 时由 clearFavorites() 统一清理。
 */

const FAVORITES_KEY = 'prep_favorites_v1'

/** @returns {string[]} */
export function loadFavoriteIds() {
  try {
    if (typeof localStorage === 'undefined') return []
    const raw = localStorage.getItem(FAVORITES_KEY)
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
    localStorage.setItem(FAVORITES_KEY, JSON.stringify(clean))
    return true
  } catch (e) {
    return false
  }
}

/** 清空本机收藏（退出登录 / 401 时调用） */
export function clearFavorites() {
  try {
    if (typeof localStorage === 'undefined') return false
    localStorage.removeItem(FAVORITES_KEY)
    return true
  } catch (e) {
    return false
  }
}
