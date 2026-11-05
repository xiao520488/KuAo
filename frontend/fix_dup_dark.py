# -*- coding: utf-8 -*-
"""修改 fix_dup_dark.py，支持多文件参数"""
import io, re, sys
sys.stdout.reconfigure(encoding='utf-8')

STR_RE = re.compile(r"""(['"])([^'"\n]*)\1""")


def dedupe_string(m):
    q, body = m.group(1), m.group(2)
    toks = body.split()
    if not toks:
        return m.group(0)
    seen, out = set(), []
    for t in toks:
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    if len(out) == len(toks):
        return m.group(0)
    return q + ' '.join(out) + q


for P in sys.argv[1:]:
    s = io.open(P, encoding='utf-8').read()
    s2, n = STR_RE.subn(dedupe_string, s)
    if s2 != s:
        io.open(P, 'w', encoding='utf-8', newline='\n').write(s2)
    print('%-50s rewritten=%d' % (P, n))
