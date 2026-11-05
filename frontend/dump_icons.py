# -*- coding: utf-8 -*-
"""定位：HomeView 搜索图标、Header 搜索图标、Header 太阳/月亮图标 的完整 svg 片段"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

t = open('src/views/HomeView.vue', encoding='utf-8').read()
i = t.find('<template>')
i = t.find('搜索框')
print('=' * 25, 'HomeView search area', '=' * 25)
print(t[i - 100:i + 1400])

t2 = open('src/components/Header.vue', encoding='utf-8').read()
j = t2.find('aria-label="isDark')
print('=' * 25, 'Header theme toggle (L280-)', '=' * 25)
print(t2[j - 900:j + 1400])
