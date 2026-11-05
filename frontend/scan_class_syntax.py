# -*- coding: utf-8 -*-
"""检查 :class / class 属性是否有引号不配对、嵌套引号等语法问题"""
import os, re, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

problems = 0
for dp, _, fns in os.walk('src'):
    for fn in fns:
        if not fn.endswith('.vue'):
            continue
        p = os.path.join(dp, fn)
        lines = open(p, encoding='utf-8').read().split('\n')
        for i, line in enumerate(lines, 1):
            # 统计一行内双引号数量：奇数说明属性引号未配对
            dq = line.count('"')
            sq = line.count("'")
            # :class="..." 中出现裸双引号包裹的字符串，典型损坏形态
            if re.search(r':class="[^"]*"[^"]*"', line):
                problems += 1
                print('%s:%d  DQ-ODD? %s' % (p, i, line.strip()[:160]))
            elif dq % 2 == 1 and (':class' in line or 'class=' in line):
                problems += 1
                print('%s:%d  ODD-DQ  %s' % (p, i, line.strip()[:160]))
print('total suspicious lines:', problems)
