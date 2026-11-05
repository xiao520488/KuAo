# -*- coding: utf-8 -*-
"""
暗黑模式覆盖率审计（固定校验脚本）

用法：
    venv\\Scripts\\python.exe audit_dark.py            # 扫描 src 下所有 .vue
    venv\\Scripts\\python.exe audit_dark.py a.vue b.vue # 只扫指定文件
    npm run audit:dark                                 # 同上（走 package.json）

判定规则（关键，避免误报）：
    同一 class 属性内，只要存在「相同变体前缀（如 hover:/focus:）+ 相同 CSS 属性」
    的任意 dark: 声明，即视为该亮色 token 已被覆盖。
    例：bg-slate-100 被 dark:bg-slate-700/60 覆盖（同属性、不同色阶，算覆盖）

退出码：0 = 无遗漏；1 = 存在遗漏（可挂到 CI / prebuild 卡口）
"""
import io
import os
import re
import sys
import collections

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')

# 需要被暗色覆盖的亮色 token：(CSS 属性, 正则)
BRIGHT = [
    ('bg', r'bg-white|bg-slate-(?:50|100|200)'
           r'|bg-(?:blue|emerald|amber|rose|red|indigo|violet|sky|teal|purple)-(?:50|100)'),
    ('text', r'text-slate-(?:900|800|700|600)'),
    ('border', r'border-slate-(?:100|200|300)'),
    ('ring', r'ring-slate-\d+'),
]
PATS = [(prop, re.compile('^(?:' + p + r')(?:/\d+)?$')) for prop, p in BRIGHT]

CLASS_RE = re.compile(r'(?::?class)="([^"]*)"', re.S)


def collect_files(argv):
    if argv:
        return argv
    out = []
    for root, _dirs, files in os.walk(SRC):
        for f in files:
            if f.endswith('.vue'):
                out.append(os.path.join(root, f))
    return sorted(out)


def scan(path):
    """返回 [(token, 片段)]"""
    s = io.open(path, encoding='utf-8').read()
    misses = []
    for m in CLASS_RE.finditer(s):
        toks = m.group(1).split()
        # 已有 dark: 覆盖的 (变体前缀, CSS 属性) 集合
        covered = set()
        for t in toks:
            if not t.startswith('dark:'):
                continue
            body = t[len('dark:'):]
            pre = ''
            if ':' in body:
                pre, body = body.rsplit(':', 1)
            covered.add((pre, body.split('-')[0]))

        for t in toks:
            if t.startswith('dark:'):
                continue
            pre, base = ('', t)
            if ':' in t:
                pre, base = t.rsplit(':', 1)
            for prop, pat in PATS:
                if pat.fullmatch(base):
                    if (pre, prop) not in covered:
                        misses.append((t, m.group(1)[:110]))
                    break
    return misses


def main():
    files = collect_files(sys.argv[1:])
    total = 0
    bad_files = 0
    agg = collections.Counter()

    for path in files:
        misses = scan(path)
        if not misses:
            continue
        bad_files += 1
        total += len(misses)
        rel = os.path.relpath(path, os.getcwd())
        print('== %s' % rel)
        cnt = collections.Counter(t for t, _ in misses)
        for t, n in cnt.most_common():
            agg[t] += n
            print('   %3d  %s  (e.g. %s)' % (n, t, next(f for x, f in misses if x == t)))

    print('-' * 60)
    print('scanned %d files, %d with gaps, %d missing tokens'
          % (len(files), bad_files, total))
    if total:
        print('[FAIL] 请给上述 token 补 dark: 变体后再提交')
        return 1
    print('[OK] 暗黑模式覆盖检查通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
