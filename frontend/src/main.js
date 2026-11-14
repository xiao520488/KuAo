import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './assets/main.css'
import './assets/theme-official.css'
import { restoreSession, logout } from './services/auth'
import { purgeLegacyToken } from './services/tokenStore'
import { purgeLegacyPlanCaches } from './services/planCache'
import { purgeLegacyFavorites } from './services/favorites'

const app = createApp(App)
app.use(router)
app.mount('#app')

// P0 账号串号止血：旧版把 token 存在 localStorage，所有窗口共享。
// 启动时立即删除旧 token，并清掉未按用户分区的计划 / 收藏缓存，
// 强制所有人重新登录，避免升级后旧 token 继续被其他窗口复用。
purgeLegacyToken()
purgeLegacyPlanCaches()
purgeLegacyFavorites()

// 启动时若当前标签页存有 token，则向后端校验一次，恢复登录态
// （失败静默降级为未登录）。token 存 sessionStorage，A/B 窗口互不影响。
restoreSession()

// api.js 响应拦截器捕获 401 时派发该事件，这里统一清理本地登录态
window.addEventListener('auth:unauthorized', () => {
  logout()
})
