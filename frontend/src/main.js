import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import './assets/main.css'
import { restoreSession, logout } from './services/auth'

const app = createApp(App)
app.use(router)
app.mount('#app')

// 启动时若本地存有 token，则向后端校验一次，恢复登录态（失败静默降级为未登录）
restoreSession()

// api.js 响应拦截器捕获 401 时派发该事件，这里统一清理本地登录态
window.addEventListener('auth:unauthorized', () => {
  logout()
})
