# -*- coding: utf-8 -*-
"""对比：源码中出现的 dark: 类 vs 产物 CSS 中实际生成的 dark 类"""
import os, re, io, sys, glob
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

src_dark = set()
occ = 0
for dp, _, fns in os.walk('src'):
    for fn in fns:
        if not fn.endswith('.vue'):
            continue
        p = os.path.join(dp, fn)
        t = open(p, encoding='utf-8').read()
        t = re.sub(r'<style.*?</style>', '', t, flags=re.S)
        # 按空白/引号/标签边界切分为"类 token"候选
        for chunk in re.split(r'[\s"\'`><{}=,()]+', t):
            if 'dark:' in chunk and chunk.startswith('dark:'):
                occ += 1
                src_dark.add(chunk)

print('source dark tokens distinct:', len(src_dark), 'occurrences:', occ)

css_dark = set()
for f in glob.glob('dist/assets/*.css'):
    s = open(f, encoding='utf-8').read()
    for m in re.finditer(r'\.((?:[a-zA-Z0-9_-]+\\?:)+[a-zA-Z0-9_./\[\]%-]+)', s):
        cls = m.group(1).replace('\\', '')
        if 'dark:' in cls:
            css_dark.add(cls)

print('generated dark classes in css:', len(css_dark))
print()
print('sample generated:', sorted(css_dark)[:15])
missing = sorted(src_dark - css_dark)
print()
print('--- missing (%d) ---' % len(missing))
for t in missing:
    print('  ', t)
