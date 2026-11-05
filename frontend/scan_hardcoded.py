# -*- coding: utf-8 -*-
"""扫描 src 下所有 vue/css 中的写死颜色与可疑 <style> 覆盖"""
import os, re, sys, io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

PATTERNS = [
    ('hardcoded-hex-class', re.compile(r'\b(?:bg|text|border|from|to|via|ring|divide|placeholder|shadow)-\[#[0-9a-fA-F]{3,8}\]')),
    ('hardcoded-hex-any', re.compile(r'#[0-9a-fA-F]{6}\b')),
    ('rgb-rgba', re.compile(r'\brgba?\s*\(')),
    ('important-color', re.compile(r'!\s*(?:bg|text|border)-')),
]

def walk(root='src'):
    for dp, dn, fn in os.walk(root):
        for f in fn:
            if f.endswith(('.vue', '.css')):
                yield os.path.join(dp, f)

for path in sorted(walk()):
    with open(path, encoding='utf-8') as fh:
        lines = fh.readlines()
    hits = []
    for i, line in enumerate(lines, 1):
        for name, pat in PATTERNS[:4]:
            if pat.search(line):
                hits.append((i, name, line.strip()[:150]))
    has_style = any(re.search(r'<style', l) for l in lines)
    if hits or has_style:
        print('=' * 70)
        print(path, '  | <style> block:', has_style)
        for i, name, s in hits:
            print('  L%-4d [%s] %s' % (i, name, s))
