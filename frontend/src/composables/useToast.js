import { reactive, readonly } from 'vue'

/**
 * 全局轻提示（Toast）状态（单例）
 *
 * 为什么放在模块作用域：
 * - api.js 的响应拦截器不是组件，无法调用 useXxx()；
 * - Toast.vue 挂在 App.vue，与调用方不在同一条组件链上；
 * 二者只能通过模块级共享状态通信，谁 import 都拿到同一份。
 */

/** 自动关闭的默认时长（ms） */
const DEFAULT_DURATION = 2000

const state = reactive({
  visible: false,
  message: ''
})

/** 只读状态，供组件渲染，避免被随意篡改 */
export const toastState = readonly(state)

/** 计时器句柄：连续触发时复用/重置，避免多条 Toast 叠着闪 */
let timer = null

/**
 * 显示一条 Toast
 *
 * 注意：先定内容、后开开关。
 * 若先开开关再改 message，因为 visible 为 true 时再次置 true 不触发过渡，
 * 会出现「文案已经换了但动画不重放」的观感问题；这里统一先写 message。
 *
 * @param {string} message 提示文案
 * @param {number} [duration] 显示时长（ms），默认 2000
 */
export function showToast(message, duration = DEFAULT_DURATION) {
  const text = typeof message === 'string' ? message.trim() : ''
  if (!text) return

  // 清掉上一条的计时器，避免旧计时器提前把新 Toast 关掉
  if (timer !== null) {
    clearTimeout(timer)
    timer = null
  }

  state.message = text
  state.visible = true

  timer = setTimeout(() => {
    state.visible = false
    timer = null
  }, duration)
}

/** 立即关闭 Toast（组件卸载等场景使用） */
export function hideToast() {
  if (timer !== null) {
    clearTimeout(timer)
    timer = null
  }
  state.visible = false
}
