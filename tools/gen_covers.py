#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一 AI 封面生成器（库奥平台）。

为每个赛事生成一张风格统一的 16:9 封面，写入 frontend/public/covers/<id>.webp。

用法（在仓库根目录）：
  python tools/gen_covers.py --dry-run            # 只打印提示词，不花钱
  python tools/gen_covers.py --limit 3            # 先跑 3 张样张
  python tools/gen_covers.py --ids 120,10,1       # 指定赛事
  python tools/gen_covers.py                      # 全量（已存在的跳过）
  python tools/gen_covers.py --force --ids 120    # 重生成

生图通道（自动探测）：
  1. image-gen skill：OIL_UI_IMAGE_GEN_SCRIPT 或 skills/image-gen/scripts/image_gen.py
  2. oiloil 积分：skills/oil-ui/scripts/oiloil-image.mjs（需先 npx github:oil-oil/oil-cli login）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "backend" / "competitions.db"
OUT_DIR = ROOT / "frontend" / "public" / "covers"
OILOIL = Path(
    os.environ.get(
        "OILOIL_IMAGE_SCRIPT",
        r"C:\Users\xiao\.dsh\profiles\desktop\node_modules\dsh-oil-ui\skills\oil-ui\scripts\oiloil-image.mjs",
    )
)
IMAGE_GEN = os.environ.get("OIL_UI_IMAGE_GEN_SCRIPT", "")
NODE = os.environ.get("NODE_BIN", r"D:\Tools\node.exe")
SIZE = "1536x864"

# ---------------------------------------------------------------- 统一风格
STYLE_HEAD = (
    "统一品牌视觉体系，横向 16:9 赛事封面海报背景，"
    "深蓝科技渐变底色（#0A1730 → #12285C → 靛青 #1B3A8C），"
    "几何抽象主视觉：细密透视网格、发光几何线框、半透明玻璃质感切面、"
    "柔和体积光与冷调光斑、少量青紫撞色光带，"
    "现代扁平 3D 渲染质感，干净、专业、高级，画面有留白与呼吸感，"
)
STYLE_TAIL = (
    "。构图左侧留出大面积干净渐变留白（后续叠加标题用），主视觉集中在中右侧；"
    "禁止出现任何文字、字母、数字、logo、徽标、水印、边框、二维码；"
    "禁止实拍照片与真实人脸，禁止杂乱堆叠，全图保持同一套配色与光源方向。"
)

# 类别 → 主视觉元素（按顺序匹配，命中即用）
CONCEPTS: list[tuple[tuple[str, ...], str]] = [
    (("人工智能", "算法"), "发光神经网络节点与推理芯片切面"),
    (("机器人", "自动化", "智能制造", "工程机械"), "未来感机械臂与齿轮几何"),
    (("芯片", "集成电路", "半导体", "电子"), "晶圆电路走线与芯片晶格"),
    (("软件", "信息技术", "互联网", "大数据", "云计算"), "悬浮数据方块与流动光带矩阵"),
    (("医学", "护理", "药学", "生物", "健康"), "抽象分子结构与脉搏光带"),
    (("农业", "食品", "林业", "畜牧"), "抽象作物几何与叶脉光纹"),
    (("能源", "电力", "材料", "化工", "环境", "双碳"), "能量环与分子晶格"),
    (("土木", "建筑", "交通", "测绘", "水利"), "抽象桁架桥梁与城市天际线几何"),
    (("数学", "统计", "物理"), "拓扑曲面与几何镶嵌"),
    (("设计", "艺术", "传媒", "广告", "文创"), "几何构成与色彩光带拼贴"),
    (("金融", "经济", "管理", "商务", "创业", "营销"), "上升光轨与多面体宝石切面"),
    (("教育", "师范", "教学"), "抽象书本几何与光点汇聚"),
    (("技能", "职业", "工匠"), "精密工具几何与金属切面"),
    (("体育",), "抽象动感轨迹与光环"),
]
DEFAULT_CONCEPT = "抽象科技几何构成与光带"


def concept_for(category: str, name: str) -> str:
    text = f"{category or ''}{name or ''}"
    for keys, concept in CONCEPTS:
        if any(k in text for k in keys):
            return concept
    return DEFAULT_CONCEPT


def build_prompt(category: str, name: str, tech: str = "") -> str:
    concept = concept_for(category, name)
    extra = ""
    if tech and tech.strip().startswith("["):
        try:
            tech = "、".join(str(x) for x in json.loads(tech))
        except Exception:  # noqa: BLE001
            tech = tech.strip("[]")
    if tech:
        words = [w.strip().strip('"\'') for w in re.split(r"[,，、;；/|]", tech) if w.strip()][:3]
        if words:
            extra = "，点缀元素可呼应：" + "、".join(words)
    return f"{STYLE_HEAD}主视觉元素为{concept}{extra}{STYLE_TAIL}"


def load_rows(ids: list[int] | None, limit: int | None) -> list[dict]:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    sql = "SELECT id, name, category, tech_stack FROM competitions WHERE is_active IS NOT 0"
    rows = [dict(r) for r in con.execute(sql)]
    con.close()
    rows.sort(key=lambda r: r["id"])
    if ids:
        want = set(ids)
        rows = [r for r in rows if r["id"] in want]
    elif limit:
        rows = rows[:limit]
    return rows


def pick_channel() -> tuple[str, list[str]]:
    if IMAGE_GEN and Path(IMAGE_GEN).exists():
        return "image-gen", [IMAGE_GEN]
    if OILOIL.exists():
        return "oiloil", [NODE, str(OILOIL)]
    raise SystemExit("没有可用的生图通道：既没找到 image-gen 脚本，也没找到 oiloil-image.mjs")


def gen_one(channel: str, cmd: list[str], prompt: str, out: Path, force: bool) -> None:
    if channel == "image-gen":
        args = cmd + ["--prompt", prompt, "--size", SIZE, "--out", str(out)]
    else:
        args = cmd + ["image", prompt, "--size", SIZE, "--out", str(out)]
    if force:
        args.append("--force")
    r = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or "").strip()[:400])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    ids = [int(x) for x in re.split(r"[,，\s]+", args.ids) if x.strip().isdigit()]
    rows = load_rows(ids or None, args.limit or None)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    channel, cmd = ("dry", []) if args.dry_run else pick_channel()

    report = []
    ok = 0
    for i, row in enumerate(rows, 1):
        cid = row["id"]
        out = OUT_DIR / f"{cid}.webp"
        prompt = build_prompt(row["category"] or "", row["name"] or "", row["tech_stack"] or "")
        rec = {"id": cid, "name": row["name"], "out": str(out.relative_to(ROOT)), "prompt": prompt}
        if out.exists() and not args.force:
            rec["status"] = "skip"
        elif args.dry_run:
            rec["status"] = "dry"
            print(f"[{i}/{len(rows)}] {cid} {row['name'][:26]}\n    {prompt}\n")
        else:
            try:
                gen_one(channel, cmd, prompt, out, args.force)
                rec["status"] = "ok"
                ok += 1
                print(f"[{i}/{len(rows)}] {cid} ok {out.name} {out.stat().st_size // 1024}KB")
            except Exception as e:  # noqa: BLE001
                rec["status"] = "fail"
                rec["error"] = str(e)
                print(f"[{i}/{len(rows)}] {cid} FAIL {e}")
            time.sleep(args.sleep)
        report.append(rec)

    ws = ROOT / "workspace"
    ws.mkdir(exist_ok=True)
    (ws / "cover-gen-report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    done = sum(1 for r in report if r["status"] in ("ok", "skip"))
    print(f"\n完成 {done}/{len(rows)}（新生成 {ok}）；报告：workspace/cover-gen-report.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
