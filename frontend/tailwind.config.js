import typography from '@tailwindcss/typography'

/** @type {import('tailwindcss').Config} */
export default {
  // 深色走 class 策略：由 <html class="dark"> 控制。
  // 三套主题 = 官方风（默认）/ 极简风 / 科技蓝，其中只有「科技蓝」会挂 .dark，
  // 由 src/composables/useTheme.js 统一切换并持久化到 localStorage。
  darkMode: 'class',
  content: [
    './index.html',
    './src/**/*.{vue,js,ts,jsx,tsx}'
  ],
  theme: {
    extend: {}
  },
  plugins: [typography]
}
