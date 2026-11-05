# -*- coding: utf-8 -*-
import os, re, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

bad = re.compile(r'viewBox="([^"]*)"')
n = 0
for dp, _, fns in os.walk('src'):
    for fn in fns:
        if not fn.endswith('.vue'):
            continue
        p = os.path.join(dp, fn)
        for i, line in enumerate(open(p, encoding='utf-8'), 1):
            for m in bad.finditer(line):
                v = m.group(1)
                if v.strip() != '0 0 24 24':
                    n += 1
                    print('%s:%d  viewBox="%s"' % (p, i, v))
print('total suspicious viewBox:', n)
