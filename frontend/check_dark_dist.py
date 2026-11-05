# -*- coding: utf-8 -*-
import glob, re, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

for f in glob.glob('dist/assets/*.css'):
    s = open(f, encoding='utf-8').read()
    total = len(re.findall(r'\.dark\\:', s))
    print(f, '-> .dark\\: rules =', total)
    # 抽取几个样本
    for m in re.findall(r'\.dark\\:[^{]{0,60}\{[^}]{0,80}\}', s)[:3]:
        print('   ', m)

# 源码里 dark: 出现次数
import os
cnt = 0
for dp, _, fns in os.walk('src'):
    for fn in fns:
        if fn.endswith('.vue'):
            t = open(os.path.join(dp, fn), encoding='utf-8').read()
            cnt += len(re.findall(r'\bdark:(?:bg|text|border|ring|divide|from|to|via|placeholder|hover|focus|shadow)', t))
print('source dark: tokens =', cnt)
