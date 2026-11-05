# -*- coding: utf-8 -*-
"""补 App.vue / Header.vue 的暗色遗漏（纯 ASCII 唯一锚点 + assert count）"""
import io, sys
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

REPL = [
    ('src/App.vue',
     'min-h-full flex flex-col bg-slate-50 text-slate-800',
     'min-h-full flex flex-col bg-slate-50 dark:bg-slate-900 text-slate-800 dark:text-slate-100', 1),
    ('src/components/Header.vue',
     'bg-white/60 backdrop-blur-xl',
     'bg-white/60 dark:bg-slate-900/70 backdrop-blur-xl', 1),
    ('src/components/Header.vue',
     'focus-within:bg-white focus-within:ring-blue-500/40',
     'focus-within:bg-white dark:focus-within:bg-slate-800 focus-within:ring-blue-500/40', 2),
    ('src/components/Header.vue',
     'shadow-xl shadow-slate-900/10 ring-1 ring-slate-900/5"',
     'shadow-xl shadow-slate-900/10 ring-1 ring-slate-900/5 dark:ring-white/10"', 1),
]

cache = {}
for path, old, new, expect in REPL:
    s = cache.setdefault(path, io.open(path, encoding='utf-8').read())
    cnt = s.count(old)
    assert cnt == expect, 'MISMATCH %r in %s: %d != %d' % (old, path, cnt, expect)
    cache[path] = s.replace(old, new)
    print('OK  %-12s %s' % (path.split('/')[-1], old[:55]))

for path, s in cache.items():
    io.open(path, 'w', encoding='utf-8', newline='\n').write(s)
print('done')
