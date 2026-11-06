import { ref, watch } from 'vue'

// 键名带版本号：旧版「暗黑模式」把 'dark' / 'light' 存在 'theme' 里，
// 直接沿用会把旧偏好误认成新主题（旧 dark 一进来就变科技蓝），
// 因此换用新键，用户首次进入统一落到默认的「官方风」。
const STORAGE_KEY = 'theme-v2'

/**
 * 全局主题状态（三套主题）
 *
 * 设计要点：
 * 1. 主题 id 是唯一真相源，同步到 <html> 的 class：
 *    - theme-official / theme-minimal / theme-tech（给自定义样式用）
 *    - tech 额外挂 .dark，继续沿用 Tailwind darkMode: 'class' 的全部 dark: 工具类；
 * 2. 首屏不闪白：index.html 里有内联脚本在 CSS 加载前就写入 class，
 *    这里只负责读取当前状态并同步 Vue 响应式变量；
 * 3. 用户选择写入 localStorage，刷新后保持；
 *    未设置过时使用默认主题「官方风」，不再跟随系统 prefers-color-scheme；
 * 4. 只认新版主题 id，旧版的 'dark' / 'light' 一律落到默认主题。
 */
export const THEMES = [
  {
    id: 'official',
    label: '官方风',
    hint: '校园主题背景图 + 半透明白色遮罩'
  },
  {
    id: 'minimal',
    label: '极简风',
    hint: '纯白留白，专注内容本身'
  },
  {
    id: 'tech',
    label: '科技蓝',
    hint: '深色底 + 蓝色高光的科技感'
  }
]

export const DEFAULT_THEME = 'official'

const THEME_IDS = THEMES.map((t) => t.id)

/** 归一化为当前主题 id；旧版 dark/light 不参与映射，直接回落默认主题 */
function normalizeTheme(value) {
  return THEME_IDS.includes(value) ? value : DEFAULT_THEME
}

function readInitial() {
  if (typeof document === 'undefined') return DEFAULT_THEME

  // 内联脚本已在首帧前写好 data-theme 时以它为准（避免重复判断）
  const domTheme = document.documentElement.dataset.theme
  if (THEME_IDS.includes(domTheme)) return domTheme

  try {
    const saved = localStorage.getItem(STORAGE_KEY)
    if (saved) return normalizeTheme(saved)
  } catch (e) {
    /* localStorage 不可用（隐私模式）时静默降级 */
  }

  return DEFAULT_THEME
}

// 模块级单例：所有组件共享同一份状态
export const theme = ref(readInitial())

/** 当前是否是深色主题（科技蓝） */
export const isDark = ref(theme.value === 'tech')

/** 把主题应用到 <html> 并持久化 */
export function applyTheme(id) {
  if (typeof document === 'undefined') return

  const next = normalizeTheme(id)
  const root = document.documentElement

  root.classList.remove('theme-official', 'theme-minimal', 'theme-tech')
  root.classList.add(`theme-${next}`)
  // 科技蓝沿用原有 dark: 工具类，其余主题一律移除，保证浅色底
  root.classList.toggle('dark', next === 'tech')
  root.dataset.theme = next
  // 让浏览器原生控件（滚动条、输入框自动填充）跟随主题
  root.style.colorScheme = next === 'tech' ? 'dark' : 'light'

  try {
    localStorage.setItem(STORAGE_KEY, next)
  } catch (e) {
    /* ignore */
  }
}

/** 切换主题：传入 official / minimal / tech */
export function setTheme(id) {
  if (THEME_IDS.includes(id)) theme.value = id
}

/** 按顺序循环切换（供快捷键等场景使用） */
export function cycleTheme() {
  const index = THEME_IDS.indexOf(theme.value)
  theme.value = THEME_IDS[(index + 1) % THEME_IDS.length]
}

// 状态变化时立即生效并落盘（切换与初始化共用一条路径）
watch(
  theme,
  (v) => {
    isDark.value = v === 'tech'
    applyTheme(v)
  },
  { immediate: true }
)
