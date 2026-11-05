# -*- coding: utf-8 -*-
import os, re, io, sys, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# 收集源码中所有 dark: 附近的 token，统计异常形态
bad = collections.Counter()
samples = collections.defaultdict(list)
good = collections.Counter()

DARK = re.compile(r'([\w:/\[\]\.\-%!]*dark:[\w:/\[\]\.\-%!]*)')

for dp, _, fns in os.walk('src'):
    for fn in fns:
        if not fn.endswith('.vue'):
            continue
        p = os.path.join(dp, fn)
        for i, line in enumerate(open(p, encoding='utf-8'), 1):
            for m in DARK.finditer(line):
                tok = m.group(1)
                if tok.startswith('dark:') and tok.count('dark:') == 1:
                    # 正常形态：dark:bg-slate-900 等
                    if re.fullmatch(r'dark:[a-z-]+-[a-z]+-?\d*(/\d+)?', tok):
                        good[tok] += 1
                    else:
                        bad['odd-shape'] += 1
                        if len(samples['odd-shape']) < 30:
                            samples['odd-shape'].append((p, i, tok))
                else:
                    key = 'multi-dark' if tok.count('dark:') > 1 else 'prefixed'
                    bad[key] += 1
                    if len(samples[key]) < 30:
                        samples[key].append((p, i, tok))

print('good-looking dark tokens:', sum(good.values()), ' unique:', len(good))
for k, v in bad.items():
    print('\n[%s] %d' % (k, v))
    for p, i, t in samples[k]:
        print('   %s:%d  %s' % (p, i, t))
