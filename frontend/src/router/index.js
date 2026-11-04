import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '@/views/HomeView.vue'
import { hasToken } from '@/services/auth'
import { openAuthModal, openAuthModalDeferred } from '@/composables/useAuthModal'

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
    // 404 兜底：后续可替换为专门的 NotFound 页面
    {
      path: '/:pathMatch(.*)*',
      redirect: '/'
    }
  ]
})

/**
 * 全局前置守卫：登录鉴权
 *
 * 判定规则：
 * - 目标路由（含父级）声明了 requiresAuth，且本地无 token → 拦截
 * - 拦截动作：唤起 AuthModal 登录弹窗，并取消本次导航（留在当前页）
 *
 * 说明：
 * - 只校验「有没有 token」，不做 API 请求。token 是否真的失效由
 *   api.js 的 401 拦截器兜底（会清 token 并派发 auth:unauthorized）。
 *   若在这里 await 校验接口，会让每次跳转都多一次网络往返，得不偿失。
 * - 子路由若未显式声明，使用 to.matched.some() 向上匹配父级 meta。
 */
router.beforeEach((to, from) => {
  const needAuth = to.matched.some((record) => record.meta?.requiresAuth)
  if (!needAuth) return true

  if (hasToken()) return true

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
})

export default router
