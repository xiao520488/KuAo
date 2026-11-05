import typography from '@tailwindcss/typography'

/** @type {import('tailwindcss').Config} */
export default {
  // 暗黑模式走 class 策略：由 <html class="dark"> 控制，
  // 便于手动切换 + localStorage 持久化，而不是跟随系统 prefers-color-scheme
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
