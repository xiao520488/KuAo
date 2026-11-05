# -*- coding: utf-8 -*-
"""精确打印行号范围：Header 太阳/月亮 svg、两处搜索 svg"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

def show(path, a, b):
    print('=' * 20, '%s  L%d-%d' % (path, a, b), '=' * 20)
    lines = open(path, encoding='utf-8').read().split('\n')
    for i in range(a - 1, min(b, len(lines))):
        print('%4d| %s' % (i + 1, lines[i]))

# Header 主题切换（记忆里 L283-292 附近）
show('src/components/Header.vue', 276, 300)

# 全局找所有 <path 的 d 值所在行号，方便定位搜索图标
for path in ['src/components/Header.vue', 'src/views/HomeView.vue']:
    print('=' * 20, 'PATH lines in', path, '=' * 20)
    lines = open(path, encoding='utf-8').read().split('\n')
    for i, l in enumerate(lines, 1):
        if '<path' in l or ('d="' in l and 'path' not in l):
            print('%4d| %s' % (i, l.strip()[:170]))
