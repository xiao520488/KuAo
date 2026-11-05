# -*- coding: utf-8 -*-
import io, sys
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass
targets = sys.argv[1:]
for P in ['src/App.vue', 'src/components/Header.vue']:
    for i, line in enumerate(io.open(P, encoding='utf-8').read().split('\n'), 1):
        for t in targets:
            if t in line:
                print('%-12s %4d | %s' % (P.split('/')[-1], i, line.strip()[:200]))
                break
