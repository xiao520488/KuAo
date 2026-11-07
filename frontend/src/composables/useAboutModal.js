import { reactive, readonly } from 'vue'

/**
 * 全局「关于我们」弹窗状态（单例）
 *
 * Header 只负责触发打开，AboutModal 挂在 App.vue 根部，
 * 这样无论当前在哪个路由、页面是否重新挂载，弹窗都能正常显示。
 */
const state = reactive({
  visible: false
})

/** 只读状态，供组件渲染 */
export const aboutModalState = readonly(state)

/** 打开「关于我们」弹窗 */
export function openAboutModal() {
  state.visible = true
}

/** 关闭「关于我们」弹窗 */
export function closeAboutModal() {
  state.visible = false
}
