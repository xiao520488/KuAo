# -*- coding: utf-8 -*-
"""给 Header.vue 增加 主题切换按钮（桌面 + 移动端）"""
import io, sys
sys.stdout.reconfigure(encoding='utf-8')

P = 'src/components/Header.vue'
s = io.open(P, encoding='utf-8').read()

# ---------- 1. script: 引入 useTheme ----------
anchor1 = "import { authModalState, openAuthModal } from '@/composables/useAuthModal'"
assert s.count(anchor1) == 1
s = s.replace(
    anchor1,
    anchor1 + "\nimport { isDark, toggleTheme } from '@/composables/useTheme'"
)

# ---------- 2. template: 桌面端切换按钮 ----------
# 插在 <div class="flex items-center gap-2 sm:gap-3"> 之后（登录区容器）
anchor2 = '<div class="flex items-center gap-2 sm:gap-3">'
assert s.count(anchor2) == 1, s.count(anchor2)

btn_desktop = '''<div class="flex items-center gap-2 sm:gap-3">
        <!-- AM-THEME-TOGGLE-START -->
        <button
          type="button"
          class="hidden sm:inline-flex h-9 w-9 items-center justify-center rounded-full
                 text-slate-600 hover:bg-slate-100 hover:text-slate-900
                 dark:text-slate-300 dark:hover:bg-slate-700/60 dark:hover:text-white
                 transition-colors cursor-pointer bg-transparent border-0 p-0
                 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
          :aria-label="isDark ? '切换到浅色模式' : '切换到深色模式'"
          :title="isDark ? '切换到浅色模式' : '切换到深色模式'"
          @click="toggleTheme"
        >
          <svg v-if="isDark" class="h-5 w-5" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="2" stroke-linecap="round"
               stroke-linejoin="round">
            <circle cx="12" cy="12" r="4" />
            <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
          </svg>
          <svg v-else class="h-5 w-5" fill="none" viewBox="0 0 24 24"
               stroke="currentColor" stroke-width="2" stroke-linecap="round"
               stroke-linejoin="round">
            <path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" />
          </svg>
        </button>
        <!-- AM-THEME-TOGGLE-END -->
'''
s = s.replace(anchor2, btn_desktop, 1)

io.open(P, 'w', encoding='utf-8', newline='\n').write(s)
print('desktop toggle inserted')
