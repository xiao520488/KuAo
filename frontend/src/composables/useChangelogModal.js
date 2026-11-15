import { reactive, readonly } from 'vue'

/**
 * 全局「库奥 · 更新日志」弹窗状态（单例）
 *
 * Header 只负责触发打开，ChangelogModal 挂在 App.vue 根部，
 * 这样无论当前在哪个路由、页面是否重新挂载，弹窗都能正常显示。
 */
const state = reactive({
  visible: false
})

/** 只读状态，供组件渲染 */
export const changelogModalState = readonly(state)

/** 打开更新日志弹窗 */
export function openChangelogModal() {
  state.visible = true
}

/** 关闭更新日志弹窗 */
export function closeChangelogModal() {
  state.visible = false
}