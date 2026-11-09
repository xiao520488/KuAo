import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '@/views/HomeView.vue'
import { authState, hasToken, restoreSession } from '@/services/auth'
import { openAuthModal, openAuthModalDeferred } from '@/composables/useAuthModal'
import { showToast } from '@/composables/useToast'

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes: [
    {
      path: '/',
      name: 'home',
      component: HomeView
    },
    {
      path: '/competitions',
      name: 'competitions',
      // 路由懒加载，避免首屏体积变大
      component: () => import('@/views/CompetitionsView.vue')
    },
    {
      path: '/competition/:id',
      name: 'competition-detail',
      component: () => import('@/views/CompetitionDetailView.vue')
    },
    {
      path: '/profile',
      name: 'profile',
      meta: { requiresAuth: true },
      component: () => import('@/views/ProfileView.vue')
    },
    {
      path: '/preparation',
      name: 'preparation',
      meta: { requiresAuth: true },
      component: () => import('@/views/PreparationView.vue')
    },
    {
      path: '/preparation/workspace/:id',
      name: 'preparation-workspace',
      meta: { requiresAuth: true },
      component: () => import('@/views/PreparationWorkspaceView.vue')
    },
    {
      // 管理后台：需登录 + 管理员身份。两道拦截，
      // 前端守卫只负责体验，真正的权限由后端 get_current_admin 把关。
      path: '/admin',
      name: 'admin',
      meta: { requiresAuth: true, requiresAdmin: true },
      component: () => import('@/views/AdminView.vue')
    },
    // 404 兜底：后续可替换为专门的 NotFound 页面
    {
      path: '/:pathMatch(.*)*',
      redirect: '/'
    }
  ],

  /**
   * 滚动行为：控制路由切换后页面的滚动位置
   *
   * - 浏览器前进 / 后退：Vue Router 会提供 savedPosition，原样返回以恢复离开该页时的位置；
   * - 普通跳转（如首页底部「查看全部竞赛」→ /competitions）：强制平滑回到页面顶部，
   *   否则新页面会继承上一页的滚动高度，出现「一打开就停在半截」的问题。
   */
  scrollBehavior(to, from, savedPosition) {
    if (savedPosition) return savedPosition
    return { top: 0, left: 0, behavior: 'smooth' }
  }
})

/**
 * 全局前置守卫：登录鉴权 + 管理员鉴权
 *
 * 判定规则：
 * - 目标路由（含父级）声明了 requiresAuth，且本地无 token → 唤起登录弹窗并拦截
 * - 声明了 requiresAdmin（必然同时 requiresAuth）：
 *     · 内存里已有用户信息 → 直接看 is_admin
 *     · 内存为空（整页刷新直达 /admin）→ await restoreSession() 拉一次 /api/auth/me
 *     · 不是管理员 → Toast 提示无权限，踢回首页
 *
 * 说明：
 * - 普通路由只校验「有没有 token」，不做 API 请求，token 是否真的失效由
 *   api.js 的 401 拦截器兜底（会清 token 并派发 auth:unauthorized）。
 * - 只有 /admin 这一层会额外等待一次用户信息，代价可接受且只发生在进入后台时。
 * - 子路由若未显式声明，使用 to.matched.some() 向上匹配父级 meta。
 */
router.beforeEach(async (to) => {
  const needAuth = to.matched.some((record) => record.meta?.requiresAuth)
  if (!needAuth) return true

  if (!hasToken()) {
    // 未登录：唤醒登录弹窗 + 阻断跳转
    //
    // 这里同时用同步与延迟两次调用，原因见 useAuthModal.js：
    // 地址栏直接回车（整页刷新）时，首次导航的守卫早于 app.mount，
    // 同步设置的状态可能被紧随其后的初始化流程覆盖，导致「拦住了但没弹窗」。
    //   - 同步调用：常规的页内跳转立即生效，无延迟感；
    //   - 延迟调用：兜住整页刷新场景，等初始化完成后再确保弹窗可见。
    // 两者幂等，重复置 true 无副作用。
    openAuthModal('login')
    openAuthModalDeferred('login')
    return false
  }

  const needAdmin = to.matched.some((record) => record.meta?.requiresAdmin)
  if (!needAdmin) return true

  // 管理员校验：内存里没用户就补拉一次（整页刷新场景）
  let user = authState.user
  if (!user) {
    try {
      user = await restoreSession()
    } catch {
      user = null
    }
  }

  // token 失效：restoreSession 内部会清掉 token，这里按未登录处理
  if (!user) {
    openAuthModal('login')
    openAuthModalDeferred('login')
    return false
  }

  if (user.is_admin) return true

  showToast('无权限访问管理后台')
  // 已在首页时避免重复导航（会抛冗余导航错误）
  if (to.path === '/') return false
  return { path: '/' }
})

export default router
