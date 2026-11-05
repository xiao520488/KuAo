# -*- coding: utf-8 -*-
import io, os, re, sys, collections
sys.stdout.reconfigure(encoding='utf-8')

files = []
for r, d, f in os.walk('src'):
    d[:] = [x for x in d if x not in ('node_modules', 'dist')]
    for n in f:
        if n.endswith(('.vue', '.js', '.css')):
            files.append(os.path.join(r, n).replace('\\', '/'))

# 统计颜色 token
pat = re.compile(r'(?:dark:)?(?:bg|text|border|ring|divide|from|to|via|placeholder|hover:bg|hover:text|hover:border|focus:ring|focus:border)-[a-z]+-\d{2,3}(?:/\d{1,3})?')
cnt = collections.Counter()
for p in files:
    t = io.open(p, encoding='utf-8').read()
    for m in pat.finditer(t):
        cnt[m.group(0)] += 1

print('=== 颜色 token 统计 (top 60) ===')
for k, v in cnt.most_common(60):
    print(v, k)

print()
print('=== 各文件行数 ===')
for p in files:
    t = io.open(p, encoding='utf-8').read()
    print(len(t.splitlines()), p)
