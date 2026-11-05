# -*- coding: utf-8 -*-
"""修复审计 v3 报出的 20 处暗黑模式亮块遗漏。
规则：只用纯 ASCII 唯一锚点做精确文本替换，每处先 assert count==1。
"""
import io, sys
sys.stdout.reconfigure(encoding='utf-8')

REPL = [
    # (file, old, new, expect)
    # --- CompetitionCard.vue
    ('src/components/CompetitionCard.vue',
     'hover:ring-slate-300',
     'hover:ring-slate-300 dark:hover:ring-slate-600', 1),
    ('src/components/CompetitionCard.vue',
     'hover:bg-rose-50 hover:text-rose-600',
     'hover:bg-rose-50 dark:hover:bg-rose-500/10 hover:text-rose-600', 1),
    # --- AuthModal.vue
    ('src/components/AuthModal.vue',
     'ring-1 ring-slate-900/5"',
     'ring-1 ring-slate-900/5 dark:ring-white/10"', 1),
    ('src/components/AuthModal.vue',
     'focus:bg-white focus:ring-2',
     'focus:bg-white dark:focus:bg-slate-800 focus:ring-2', 3),
    # --- CompetitionsView.vue
    ('src/views/CompetitionsView.vue',
     'focus:bg-white focus:ring-blue-500',
     'focus:bg-white dark:focus:bg-slate-800 focus:ring-blue-500', 2),
    # --- HomeView.vue
    ('src/views/HomeView.vue',
     'rounded-full bg-white/90 backdrop-blur',
     'rounded-full bg-white/90 dark:bg-slate-800/90 backdrop-blur', 1),
    ('src/views/HomeView.vue',
     'bg-red-50/60 px-6 py-12',
     'bg-red-50/60 dark:bg-red-500/10 px-6 py-12', 1),
    ('src/views/HomeView.vue',
     'rounded-full bg-red-100"',
     'rounded-full bg-red-100 dark:bg-red-500/15"', 1),
    # --- CompetitionDetailView.vue
    ('src/views/CompetitionDetailView.vue',
     'border-rose-200 bg-rose-50/40',
     'border-rose-200 dark:border-rose-500/30 bg-rose-50/40 dark:bg-rose-500/10', 1),
    # --- PreparationWorkspaceView.vue
    ('src/views/PreparationWorkspaceView.vue',
     'hover:ring-slate-300\'"',
     'hover:ring-slate-300 dark:hover:ring-slate-600\'"', 1),
    ('src/views/PreparationWorkspaceView.vue',
     'bg-white/90 px-4 py-4 backdrop-blur',
     'bg-white/90 dark:bg-slate-900/90 px-4 py-4 backdrop-blur', 1),
    ('src/views/PreparationWorkspaceView.vue',
     'rounded-xl bg-slate-50/70 p-5 ring-1 ring-slate-200/60',
     'rounded-xl bg-slate-50/70 dark:bg-slate-800/60 p-5 ring-1 ring-slate-200/60 dark:ring-slate-700', 1),
    ('src/views/PreparationWorkspaceView.vue',
     'hover:bg-rose-100"',
     'hover:bg-rose-100 dark:hover:bg-rose-500/20"', 1),
    ('src/views/PreparationWorkspaceView.vue',
     'rounded-full bg-amber-100 px-3 py-1',
     'rounded-full bg-amber-100 dark:bg-amber-500/20 px-3 py-1', 1),
    ('src/views/PreparationWorkspaceView.vue',
     'rounded-full bg-indigo-50 px-3 py-1',
     'rounded-full bg-indigo-50 dark:bg-indigo-500/15 px-3 py-1', 1),
    ('src/views/PreparationWorkspaceView.vue',
     'border-amber-200/70 bg-amber-50/50',
     'border-amber-200/70 dark:border-amber-500/30 bg-amber-50/50 dark:bg-amber-500/10', 1),
    ('src/views/PreparationWorkspaceView.vue',
     'border-slate-200/80 bg-white dark:bg-slate-800',
     'border-slate-200/80 dark:border-slate-700 bg-white dark:bg-slate-800', 1),
]

cache = {}
for path, old, new, expect in REPL:
    if path not in cache:
        cache[path] = io.open(path, encoding='utf-8').read()
    s = cache[path]
    cnt = s.count(old)
    assert cnt == expect, 'ANCHOR MISMATCH %r in %s: found %d, expect %d' % (old, path, cnt, expect)
    cache[path] = s.replace(old, new)
    print('OK  %-8s %s' % (path.split('/')[-1], old[:60]))

for path, s in cache.items():
    io.open(path, 'w', encoding='utf-8', newline='\n').write(s)
print('written', len(cache), 'files')
