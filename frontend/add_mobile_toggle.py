# -*- coding: utf-8 -*-
"""移动端菜单加主题切换行"""
import io, sys
sys.stdout.reconfigure(encoding='utf-8')

P = 'src/components/Header.vue'
s = io.open(P, encoding='utf-8').read()

anchor = '<div class="mt-3 border-t border-slate-200 dark:border-slate-700 pt-3">'
assert s.count(anchor) == 1

block = '''<div class="mt-3 border-t border-slate-200 dark:border-slate-700 pt-3">
          <!-- AM-MOBILE-THEME-START -->
          <button
            type="button"
            class="w-full rounded-xl px-4 py-3 flex items-center justify-between
                   text-left text-base font-bold text-slate-700 dark:text-slate-200
                   hover:bg-slate-100 dark:hover:bg-slate-700/60 transition-colors"
            @click="toggleTheme"
          >
            <span>{{ isDark ? '浅色模式' : '深色模式' }}</span>
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
          <!-- AM-MOBILE-THEME-END -->
'''
s = s.replace(anchor, block, 1)
io.open(P, 'w', encoding='utf-8', newline='\n').write(s)
print('mobile toggle inserted')
