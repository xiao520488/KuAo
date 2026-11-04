import { createRouter, createWebHistory } from 'vue-router'
import HomeView from '@/views/HomeView.vue'

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
      component: () => import('@/views/PreparationView.vue')
    },
    {
      path: '/preparation/workspace/:id',
      name: 'preparation-workspace',
      component: () => import('@/views/PreparationWorkspaceView.vue')
    },
    // 404 兜底：后续可替换为专门的 NotFound 页面
    {
      path: '/:pathMatch(.*)*',
      redirect: '/'
    }
  ]
})

export default router
