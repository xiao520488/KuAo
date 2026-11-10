# -*- coding: utf-8 -*-
"""审计封面兜底（两字简称 + 哈希渐变）在真实数据上的表现。

回答三个问题：
  1. 有多少卡片真的会走到兜底（image 为空）；
  2. 兜底的字号/字形是否唯一（撞简称、撞首字）；
  3. 哈希色相会不会撞色（两张卡同色）。
"""
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(r"D:\agent-lab\KuAo-Platform")
DB = ROOT / "backend" / "competitions.db"
if not DB.exists():
    cands = sorted(ROOT.rglob("competitions.db"))
    if not cands:
        sys.exit("competitions.db not found")
    DB = cands[0]

HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
LEADING_NOISE = re.compile(r"""^[\s"'“”‘’《》〈〉【】\[\]（）()\-—·]+""")
GLYPH = re.compile(r"[\u3400-\u9fffA-Za-z0-9]")


def cover_glyph(title):
    raw = re.sub(r"【[^】]*】", "", str(title or ""))
    cleaned = LEADING_NOISE.sub("", raw)
    m = GLYPH.search(cleaned)
    return m.group(0).upper() if m else "赛"


def cover_short_name(sn):
    if not isinstance(sn, str):
        return ""
    chars = HAN.findall(sn)
    return "".join(chars) if len(chars) == 2 else ""


def hue_of(title):
    raw = str(title or "竞赛")
    h = 0
    for ch in raw:
        h = (h * 31 + ord(ch)) % 360
    return h


con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT id, name, image_url AS image, short_name, category, level, "
    "reg_end_date, reg_end_month FROM competitions"
).fetchall()

total = len(rows)
fallback_rows = []
img_counts = Counter()
for r in rows:
    img = (r["image"] or "").strip()
    if img:
        img_counts["has_image"] += 1
    else:
        img_counts["no_image"] += 1
        fallback_rows.append(r)

glyphs = Counter()
hues = defaultdict(list)
bad_short = []
for r in fallback_rows:
    sn = cover_short_name(r["short_name"] or "")
    g = sn or cover_glyph(r["name"])
    glyphs[g] += 1
    hues[hue_of(r["name"])].append((r["id"], r["name"]))
    if (r["short_name"] or "") and not sn:
        bad_short.append((r["id"], r["short_name"]))

glyph_dup = {k: v for k, v in glyphs.items() if v > 1}
hue_dup = {k: v for k, v in hues.items() if len(v) > 1}
# 色相相近（<=8 度）也算撞色：人眼在小图上分不出来
near = []
keys = sorted(hues)
for i, a in enumerate(keys):
    for b in keys[i + 1:]:
        d = min(abs(a - b), 360 - abs(a - b))
        if 0 < d <= 8:
            near.append((a, b, d, [n for _, n in hues[a]], [n for _, n in hues[b]]))

out = {
    "db": str(DB),
    "total_rows": total,
    "with_cover": img_counts["has_image"],
    "fallback_cards": img_counts["no_image"],
    "fallback_ratio": round(img_counts["no_image"] / total * 100, 1) if total else 0,
    "short_name_ok": sum(1 for r in fallback_rows if cover_short_name(r["short_name"] or "")),
    "glyph_first_char_mode": sum(
        1 for r in fallback_rows if not cover_short_name(r["short_name"] or "")
    ),
    "short_name_rejected": bad_short,
    "distinct_glyphs": len(glyphs),
    "glyph_collisions": {k: v for k, v in sorted(glyph_dup.items(), key=lambda kv: -kv[1])},
    "distinct_hues": len(hues),
    "exact_hue_collisions": {str(k): [n for _, n in v] for k, v in hue_dup.items()},
    "near_hue_pairs_le8": [
        {"hue_a": a, "hue_b": b, "delta": d, "a_names": na, "b_names": nb}
        for a, b, d, na, nb in near
    ],
}
print(json.dumps(out, ensure_ascii=False, indent=2))