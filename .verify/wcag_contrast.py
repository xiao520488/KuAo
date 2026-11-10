# -*- coding: utf-8 -*-
"""校验封面兜底（赛事徽记）配色的 WCAG 对比度（纯计算，不需要看图）。

口径与 frontend/src/assets/main.css 一一对应：
  - 标识方块：白字 on hsl(--cover-hue --mark-s --mark-l)，逐色相校准
  - 分类标签：hsl(h 52% 30%) on 画布 hsl(h 74% 97%)
"""
import colorsys

AA_TEXT = 4.5


def _lum(rgb):
    def f(c):
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (f(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def hsl(h, s, l):
    return colorsys.hls_to_rgb(h / 360.0, l / 100.0, s / 100.0)


def ratio(a, b):
    la, lb = _lum(hsl(*a)), _lum(hsl(*b))
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


WHITE = (0, 0, 100)

# tone, 名称, 色相, 方块 S/L（校准时定死）, 标签 S/L, 画布 S/L(亮/暗)
TONES = [
    ("工科", "蓝", 214, 62, 46, 52, 30, (74, 97), (30, 20)),
    ("理科", "绿", 158, 62, 28, 52, 30, (74, 97), (30, 20)),
    ("商科", "橙", 30, 62, 36, 52, 30, (74, 97), (30, 20)),
    ("文科", "紫", 268, 62, 46, 52, 30, (74, 97), (30, 20)),
    ("未分类", "灰", 215, 14, 34, 16, 36, (12, 96), (18, 20)),
]

print("== 亮色：方块白字 / 标签字 ==")
for tone, name, h, ms, ml, ls, ll, cs, _ in TONES:
    mark = ratio(WHITE, (h, ms, ml))
    label = ratio((h, ls, ll), (h, cs[0], cs[1]))
    flag = lambda v: "OK " if v >= AA_TEXT else "FAIL"
    print(
        f"  {tone}({name}) 方块 {mark:5.2f}:1 {flag(mark)}"
        f"   标签 {label:5.2f}:1 {flag(label)}"
    )

print("== 暗色：方块白字（同实色）/ 标签字 ==")
for tone, name, h, ms, ml, ls, ll, _, cd in TONES:
    dark_label = ratio((h, 62 if tone != "未分类" else 20, 78), (h, cd[0], cd[1]))
    mark = ratio(WHITE, (h, ms, ml))
    flag = lambda v: "OK " if v >= AA_TEXT else "FAIL"
    print(f"  {tone}({name}) 方块 {mark:5.2f}:1 {flag(mark)}   标签 {dark_label:5.2f}:1 {flag(dark_label)}")