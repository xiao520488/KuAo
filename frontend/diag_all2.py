# -*- coding: utf-8 -*-
"""补齐诊断：
A. 跨行 path d 属性解析（把开标签整体抽取再看 d 值）
B. 全局搜索写死的深色渐变 / 深色背景类
C. 定位 App.vue 模板部分（根 div 的 class）
"""
import io, os, re, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

VUE = []
for dp, _, fns in os.walk('src'):
    for fn in fns:
        if fn.endswith('.vue'):
            VUE.append(os.path.join(dp, fn))

print('=' * 20, 'A. multi-line <path> / <svg> tag scan', '=' * 20)
ok_cmd = set('MmLlHhVvCcSsQqTtAaZz')
bad = 0
for p in VUE:
    src = open(p, encoding='utf-8').read()
    # 抽取所有 <path ...> 开标签（允许跨行）
    for m in re.finditer(r'<path\b([^>]*)>', src, re.S):
        attrs = m.group(1)
        line = src[:m.start()].count('\n') + 1
        dm = re.search(r'\bd\s*=\s*(["\'])(.*?)\1', attrs, re.S)
        if not dm:
            bad += 1
            print('%s:%d  NO-VALID-D  attrs=%r' % (p, line, attrs.strip()[:120]))
            continue
        val = dm.group(2)
        illegal = [ch for ch in val if not (ch.isdigit() or ch in ' .,-' or ch in ok_cmd)]
        if illegal:
            bad += 1
            print('%s:%d  ILLEGAL=%r  d=%r' % (p, line, ''.join(sorted(set(illegal))), val[:100]))
print('bad multi-line path:', bad)

print('=' * 20, 'B. hardcoded dark bg / gradient', '=' * 20)
# 关注：不在 dark: 前缀下的 slate-8xx/9xx、from-/via-/to- 深色、bg-[#0..] 等
pat = re.compile(r'(?<!dark:)bg-(?:slate|gray|zinc|navy|indigo|blue)-(?:800|900|950)\b|'
                 r'(?:from|via|to)-(?:slate|gray|indigo|blue)-(?:800|900|950)\b|'
                 r'bg-\[#0[0-9a-fA-F]{5}\]')
for p in VUE:
    for i, line in enumerate(open(p, encoding='utf-8'), 1):
        for m in pat.finditer(line):
            print('%s:%d  %s   |  %s' % (p, i, m.group(0), line.strip()[:140]))

print('=' * 20, 'C. App.vue template', '=' * 20)
t = open('src/App.vue', encoding='utf-8').read()
i = t.find('<template>')
print(t[i:] if i != -1 else '(no <template> found)')
