# -*- coding: utf-8 -*-
"""修复被脚本损坏的 viewBox="0 24" -> viewBox="0 0 24 24"，仅替换精确形态"""
import os, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

BAD = 'viewBox="0 24"'
GOOD = 'viewBox="0 0 24 24"'
total = 0
for dp, _, fns in os.walk('src'):
    for fn in fns:
        if not fn.endswith('.vue'):
            continue
        p = os.path.join(dp, fn)
        s = open(p, encoding='utf-8').read()
        c = s.count(BAD)
        if c:
            # 额外保险：BAD 后面紧跟的应是空白或换行（属性边界），避免误伤
            s2 = s.replace(BAD, GOOD)
            open(p, 'w', encoding='utf-8', newline='').write(s2)
            total += c
            print('%s: fixed %d' % (p, c))
print('total fixed:', total)
