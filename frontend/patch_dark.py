# -*- coding: utf-8 -*-
"""全局暗黑模式：颜色 token 批量注入脚本（分文件执行，可重复运行幂等）"""
import io, re, sys, collections, json

sys.stdout.reconfigure(encoding='utf-8')

# ---------------------------------------------------------------- 映射规则
# 依据：项目基色是 slate 系。
# 顺序敏感：先处理更具体的（含变体的），再处理基础类。
RULES = [
    # --- 变体：hover / focus ---
    ('hover:bg-blue-700',        'hover:bg-blue-600'),
    ('hover:bg-slate-50',        'hover:bg-slate-800'),
    ('hover:bg-slate-100',       'hover:bg-slate-700/60'),
    ('hover:bg-blue-50',         'hover:bg-blue-500/15'),
    ('hover:bg-blue-100',        'hover:bg-blue-500/25'),
    ('hover:bg-red-50',          'hover:bg-red-500/15'),
    ('hover:text-slate-900',     'hover:text-white'),
    ('hover:text-slate-700',     'hover:text-slate-200'),
    ('hover:text-slate-600',     'hover:text-slate-300'),
    ('hover:text-blue-700',      'hover:text-blue-300'),
    ('hover:text-blue-600',      'hover:text-blue-400'),
    ('hover:border-slate-300',   'hover:border-slate-600'),

    # --- 变体：focus / placeholder ---
    ('focus:border-blue-400',    'focus:border-blue-500'),
    ('focus:ring-blue-500/20',   'focus:ring-blue-400/30'),
    ('placeholder:text-slate-400', 'placeholder:text-slate-500'),

    # --- 背景：slate 阶梯整体下沉 ---
    ('bg-white',                 'bg-slate-800'),   # 卡片 / 面板
    ('bg-slate-50/60',           'bg-slate-800/60'),
    ('bg-slate-50',              'bg-slate-900'),   # 主背景
    ('bg-slate-100/80',          'bg-slate-700/60'),
    ('bg-slate-100',             'bg-slate-700/60'),
    ('bg-slate-200/80',          'bg-slate-700'),
    ('bg-slate-200/60',          'bg-slate-700'),
    ('bg-slate-200',             'bg-slate-700'),

    # --- 背景：彩色淡底 → 半透明彩色叠深灰 ---
    ('bg-blue-50/70',            'bg-blue-500/15'),
    ('bg-blue-50',               'bg-blue-500/15'),
    ('bg-emerald-50',            'bg-emerald-500/15'),
    ('bg-amber-50',              'bg-amber-500/15'),
    ('bg-rose-50',               'bg-rose-500/15'),
    ('bg-red-50',                'bg-red-500/15'),

    # --- 文字：slate 阶梯整体上浮 ---
    ('text-slate-900',           'text-slate-100'),
    ('text-slate-800',           'text-slate-100'),
    ('text-slate-700',           'text-slate-200'),
    ('text-slate-600',           'text-slate-300'),
    ('text-slate-500',           'text-slate-400'),
    ('text-slate-400',           'text-slate-400'),  # 保持：深色下仍然可读

    # --- 文字：彩色加深色号 → 浅色号 ---
    ('text-blue-700',            'text-blue-300'),
    ('text-blue-600',            'text-blue-400'),
    ('text-emerald-700',         'text-emerald-300'),
    ('text-amber-700',           'text-amber-300'),
    ('text-rose-700',            'text-rose-300'),
    ('text-red-600',             'text-red-400'),
    ('text-red-500',             'text-red-400'),

    # --- 边框 ---
    ('border-slate-100',         'border-slate-700'),
    ('border-slate-200',         'border-slate-700'),
    ('border-slate-300',         'border-slate-600'),
    ('border-red-100',           'border-red-500/30'),
    ('border-blue-200',          'border-blue-500/30'),

    # --- ring（描边阴影） ---
    ('ring-slate-200/80',        'ring-slate-700'),
    ('ring-slate-200/70',        'ring-slate-700'),
    ('ring-slate-200',           'ring-slate-700'),
    ('ring-slate-300',           'ring-slate-600'),
    ('ring-blue-100',            'ring-blue-500/30'),
    ('ring-blue-200',            'ring-blue-500/30'),
    ('ring-emerald-200',         'ring-emerald-500/30'),
    ('ring-amber-200',           'ring-amber-500/30'),
    ('ring-rose-200',            'ring-rose-500/30'),
]

# 这些 token 在暗色下不做改动（黑底白字 UI 元素本身通用）
SKIP_PREFIX = ('dark:',)

CLASS_ATTR_RE = re.compile(r'class="([^"]*)"', re.S)
CLASS_BIND_RE = re.compile(r':class="([^"]*)"', re.S)
# 匹配一个完整 token（含任意变体前缀）
TOKEN_RE = re.compile(r'[A-Za-z0-9_\-\[\]/.%:]+')


def map_token(tok: str):
    """返回 (dark_token 或 None, 是否命中规则)"""
    base = tok
    if base.startswith(SKIP_PREFIX):
        return None, False
    for light, dark in RULES:
        if base == light:
            return dark, True
    return None, False


def process_class_string(s: str):
    """处理一段 class 属性文本，返回 (新文本, 命中列表)"""
    hits = []
    out = []
    pos = 0
    for m in TOKEN_RE.finditer(s):
        out.append(s[pos:m.start()])
        tok = m.group(0)
        dark, ok = map_token(tok)
        out.append(tok)
        if ok and dark is not None and dark != tok:
            out.append(' dark:' + dark)
            hits.append((tok, dark))
        pos = m.end()
    out.append(s[pos:])
    return ''.join(out), hits


def patch_text(text: str):
    """处理整文件：静态 class 与三元表达式里的字面量类名"""
    total = collections.Counter()

    def repl_static(m):
        new, hits = process_class_string(m.group(1))
        total.update(hits)
        return 'class="' + new + '"'

    text = CLASS_ATTR_RE.sub(repl_static, text)

    # :class 里通常是对象/三元/数组，逐段处理其中出现的裸字符串
    def repl_bind(m):
        new, hits = process_class_string(m.group(1))
        total.update(hits)
        return ':class="' + new + '"'

    text = CLASS_BIND_RE.sub(repl_bind, text)
    return text, total


if __name__ == '__main__':
    targets = sys.argv[1:]
    if not targets:
        print('usage: python patch_dark.py <file...>')
        sys.exit(1)
    grand = collections.Counter()
    for p in targets:
        src = io.open(p, encoding='utf-8').read()
        new, hits = patch_text(src)
        if new != src:
            io.open(p, 'w', encoding='utf-8', newline='\n').write(new)
        grand.update(hits)
        print('%-45s hits=%d' % (p, sum(hits.values())))
    print('---- 规则命中明细 ----')
    for (light, dark), n in grand.most_common():
        print('%3d  %-26s -> dark:%s' % (n, light, dark))
