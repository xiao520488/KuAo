# -*- coding: utf-8 -*-
"""1) 打印 App.vue 全文  2) 打印 HomeView.vue 前 60 行模板  3) 全局扫描 path d 属性异常"""
import io, os, re, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

print('=' * 25, 'App.vue', '=' * 25)
print(open('src/App.vue', encoding='utf-8').read())

print('=' * 25, 'HomeView.vue head', '=' * 25)
t = open('src/views/HomeView.vue', encoding='utf-8').read()
print(t[:1800])

print('=' * 25, 'SVG path d scan', '=' * 25)
# 合法 d 只应包含 数字/空格/逗号/小数点/负号/命令字母(MmLlHhVvCcSsQqTtAaZz)
ok_cmd = set('MmLlHhVvCcSsQqTtAaZz')
bad = 0
for dp, _, fns in os.walk('src'):
    for fn in fns:
        if not fn.endswith('.vue'):
            continue
        p = os.path.join(dp, fn)
        src = open(p, encoding='utf-8').read()
        for m in re.finditer(r'\bd="([^"]*)"', src):
            val = m.group(1)
            line = src[:m.start()].count('\n') + 1
            illegal = [ch for ch in val if not (ch.isdigit() or ch in ' .,-' or ch in ok_cmd)]
            if illegal:
                bad += 1
                print('%s:%d  ILLEGAL=%r  d=%r' % (p, line, ''.join(sorted(set(illegal))), val[:90]))
print('bad path count:', bad)

print('=' * 25, 'viewBox scan', '=' * 25)
vb = 0
for dp, _, fns in os.walk('src'):
    for fn in fns:
        if not fn.endswith('.vue'):
            continue
        p = os.path.join(dp, fn)
        src = open(p, encoding='utf-8').read()
        for m in re.finditer(r'viewBox="([^"]*)"', src):
            if m.group(1) != '0 0 24 24':
                vb += 1
                print('%s  viewBox=%r' % (p, m.group(1)))
print('non-standard viewBox:', vb)
