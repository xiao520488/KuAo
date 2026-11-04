import { reactive, readonly, nextTick } from 'vue'

/**
 * 全局登录弹窗状态（单例）
 *
 * 为什么放在模块作用域而不是组件内：
 * - Header.vue 需要用它渲染 AuthModal；
 * - router 守卫需要用它「拦截并唤起弹窗」；
 * 二者不在同一条组件链上，无法通过 props/emit 通信，
 * 因此抽成模块级响应式对象，谁 import 都拿到同一份状态。
 */
const state = reactive({
  visible: false,
  mode: 'login' // 'login' | 'register'
})

/** 只读状态，供组件使用，避免被随意篡改 */
export const authModalState = readonly(state)

/**
 * 打开登录/注册弹窗
 *
 * 注意：mode 与 visible 必须「先定内容、后开开关」。
 * 若先开开关再改 mode，弹窗会有一帧显示上一次的 Tab，视觉上会闪。
 *
 * @param {'login'|'register'} mode
 */
export function openAuthModal(mode = 'login') {
  state.mode = mode === 'register' ? 'register' : 'login'
  state.visible = true
}

/**
 * 延迟到下一轮微任务再打开弹窗
 *
 * 为什么需要它：
 * 首次进入页面（尤其地址栏直接回车）时，router 的首次导航解析、
 * app.mount、restoreSession() 几乎在同一时序内完成。
 * 守卫里同步 openAuthModal 可能被紧随其后的初始化流程覆盖，
 * 表现为「被拦截了但弹窗没弹出来」。
 * 放到 nextTick 之后执行，可确保初始化状态已稳定。
 */
export function openAuthModalDeferred(mode = 'login') {
  nextTick(() => openAuthModal(mode))
}

/** 关闭弹窗 */
export function closeAuthModal() {
  state.visible = false
}
