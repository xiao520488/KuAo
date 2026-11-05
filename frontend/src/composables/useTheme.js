import { ref, watch } from 'vue'

const STORAGE_KEY = 'theme'

/**
 * 全局主题（暗黑模式）状态
 *
 * 设计要点：
 * 1. 以 <html class="dark"> 为唯一真相源，Tailwind darkMode: 'class' 据此生效；
 * 2. 首屏不闪白：index.html 里有内联脚本在 CSS 加载前就写入 class，
 *    这里只负责读取当前状态并同步 Vue 响应式变量；
 * 3. 用户点过切换后写入 localStorage，刷新后保持；
 *    未手动设置过时默认跟随系统 prefers-color-scheme。
 */
function readInitial() {
  if (typeof document === 'undefined') return false
  if (document.documentElement.classList.contains('dark')) return true
  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved === 'dark') return true
    if (saved === 'light') return false
  } catch (e) {
    /* localStorage 不可用（隐私模式）时静默降级 */
  }
  return (
    window.matchMedia &&
    window.matchMedia('(prefers-color-scheme: dark)').matches
  )
}

// 模块级单例：所有组件共享同一份状态
export const isDark = ref(readInitial())

/** 把状态应用到 <html> 并持久化 */
export function applyTheme(dark) {
  const root = document.documentElement
  root.classList.toggle('dark', dark)
  // 让浏览器原生控件（滚动条、输入框自动填充）跟随主题
  root.style.colorScheme = dark ? 'dark' : 'light'
  try {
    localStorage.setItem(STORAGE_KEY, dark ? 'dark' : 'light')
  } catch (e) {
    /* ignore */
  }
}

/** 切换主题 */
export function toggleTheme() {
  isDark.value = !isDark.value
}

// 状态变化时立即落盘（切换与初始化共用一条路径）
watch(isDark, (v) => applyTheme(v), { immediate: true })
