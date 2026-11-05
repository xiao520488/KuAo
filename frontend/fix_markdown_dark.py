# -*- coding: utf-8 -*-
"""在 PreparationWorkspaceView.vue 的 <style scoped> 末尾追加 .dark .markdown-body 覆盖块"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

P = 'src/views/PreparationWorkspaceView.vue'
src = open(P, encoding='utf-8').read()

ANCHOR = '.markdown-body hr { border: 0; border-top: 1px solid #e2e8f0; margin: 1.2em 0; }'
assert src.count(ANCHOR) == 1, 'anchor count = %d' % src.count(ANCHOR)

DARK = ANCHOR + """

/* ===== 暗色模式覆盖：AI 解析区在深色下必须保持足够对比度 ===== */
.dark .markdown-body { color: #cbd5e1; }
.dark .markdown-body h1,
.dark .markdown-body h2,
.dark .markdown-body h3,
.dark .markdown-body h4,
.dark .markdown-body strong { color: #f1f5f9; }
.dark .markdown-body a { color: #60a5fa; }
.dark .markdown-body code { background: #334155; color: #e2e8f0; }
.dark .markdown-body pre { background: #020617; color: #e2e8f0; border: 1px solid #334155; }
.dark .markdown-body pre code { background: transparent; color: inherit; }
.dark .markdown-body blockquote { border-left-color: #3b82f6; color: #94a3b8; }
.dark .markdown-body th,
.dark .markdown-body td { border-color: #334155; }
.dark .markdown-body th { background: #1e293b; color: #f1f5f9; }
.dark .markdown-body hr { border-top-color: #334155; }"""

src = src.replace(ANCHOR, DARK)
open(P, 'w', encoding='utf-8', newline='').write(src)
print('OK: dark markdown-body override appended')
print('checks ->', src.count('.dark .markdown-body'))
