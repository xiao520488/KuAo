import { clearAllPlanCaches } from './planCache'
import { clearFavorites } from './favorites'

/**
 * 清空当前浏览器里属于“本地用户”的全部数据。
 *
 * 触发时机：
 * - 用户主动退出登录（services/auth.js → logout）
 * - 接口返回 401（services/api.js 响应拦截器）
 *
 * 清理内容：
 * - 所有 plan_* 备赛计划 / 对话缓存
 * - prep_favorites_v1 灵感收藏
 *
 * 清理完成后派发 auth:local-data-cleared，通知仍挂载的页面丢弃内存中的计划与收藏，
 * 避免“本地已清空、内存仍展示”的假象，也避免后续操作把旧数据重新写回。
 */
export function clearUserLocalData() {
  try {
    clearAllPlanCaches()
  } catch (e) {
    /* 存储异常不影响登出流程 */
  }

  try {
    clearFavorites()
  } catch (e) {
    /* 同上 */
  }

  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('auth:local-data-cleared'))
  }
}
