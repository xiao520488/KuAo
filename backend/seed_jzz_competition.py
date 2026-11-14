# -*- coding: utf-8 -*-
"""seed_jzz_competition.py —— 特批赛事「湖南省“金种子杯”大学生创业大赛」入库（幂等）。

用法：
    venv\\Scripts\\python.exe seed_jzz_competition.py            # dry-run，只看将要写入什么
    venv\\Scripts\\python.exe seed_jzz_competition.py --apply    # 真写库

说明：
  * 该赛事不在教育部 84 项目录里（省级），已在 competition_whitelist.SPECIAL_APPROVED
    里特批，因此 gate()/清理脚本不会再把它降级或删除；
  * 幂等：按 name 命中就更新关键字段，不重复插入；
  * 只写 competitions 表，不碰 awards / users 等数据。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "competitions.db"

NAME = "湖南省“金种子杯”大学生创业大赛"
SHORT_NAME = "金种子杯"
OFFICIAL_URL = "https://jyt.hunan.gov.cn/sjyt/xxgk/tzgg/202510/t20251014_1167745.html"
NOTICE_2026 = "https://xsc.jyt.hunan.gov.cn/c/2026-07-08/1180348.shtml"

INTRO = (
    "湖南省人民政府主办的省级大学生创业赛事，2025 年创办（首届），是湖南支持大学生创业"
    "「七个一」行动的核心抓手。面向全国乃至全球大学生（含毕业三年内），不限地域户籍国籍；"
    "设传统/优势/新兴/未来产业赛道（首届），第二届改为先进制造与高端装备、人工智能与信息技术、"
    "新能源与新材料、空天海洋与低空经济、现代农业与大健康、新消费与 AI+文化科技 6 个主赛道"
    "及 AI 3D 大模型应用专项赛。金奖 10 万元奖励，获奖项目视同通过湖南省大学生创业投资基金"
    "立项评审、可直接进入尽职调查，有机会获最高 100 万元首期投资。"
)

PARTICIPANTS = (
    "1. 参赛对象为正在创业或已创办企业的大学生个人/团队，不限地域、户籍和国籍；"
    "「大学生」指全日制普通高校在校生或毕业三年内的本硕博；"
    "2. 已注册企业项目要求团队中持股≥10% 的大学生不少于 1 人；未注册企业团队不超过 10 人；"
    "3. 鼓励跨学科、跨校、跨地域、跨国组队，鼓励师生组队（教师不得担任负责人、不参与路演答辩）；"
    "4. 每队可配不超过 5 名校内外导师并确定第一指导教师。"
)

CONTENT_SCOPE = (
    "1. 首届设传统产业、优势产业、新兴产业、未来产业 4 个赛道；"
    "2. 第二届设先进制造与高端装备、人工智能与信息技术、新能源与新材料、空天海洋与低空经济、"
    "现代农业与大健康、新消费与 AI+文化科技 6 个主赛道，另设「揭榜挂帅」专项赛道与 AI 3D 大模型应用专项赛；"
    "3. 项目可为商业计划书、产品原型或创意说明，须真实合法、知识产权清晰；"
    "4. 已获国家级创新创业类赛事（中国国际大学生创新大赛、挑战杯等）奖项的项目不得重复参赛。"
)

EXAM_SCHEDULE = (
    "1. 首届：2025-03-12 启动报名，经资格审查、网络初评、网络复评，5 月 23—24 日现场决赛（160 个项目），"
    "5 月 25 日风采展示暨颁奖；\n"
    "2. 第二届：2025 年 9 月启动报名，2026-05-15—16 现场决赛（228 个项目），6 月 3 日公示，7 月 1 日正式公布；\n"
    "3. 评审以路演答辩为主，重点看项目真实性、技术/模式创新性、落地与成长性、团队匹配度。"
)

STAGE_SCHEDULE = [
    {"stage": "报名（第1届）", "start_date": "2025-03-12", "end_date": "2025-05-10",
     "note": "网络报名 → 资格审查 → 网络初评 → 网络复评"},
    {"stage": "现场决赛（第1届）", "start_date": "2025-05-23", "end_date": "2025-05-24",
     "note": "160 个项目现场决赛，4 个赛道各评金奖 4、银奖 8、铜奖 12、优胜奖 16"},
    {"stage": "风采展示暨颁奖（第1届）", "start_date": "2025-05-25", "end_date": "2025-05-25",
     "note": "长沙经开区三一魔豆仓创新中心；金奖 16、银奖 32、铜奖 48、优胜奖 63"},
    {"stage": "报名（第2届）", "start_date": "2025-09-01", "end_date": "2025-12-31",
     "note": "第二届于 2025 年 9 月启动报名"},
    {"stage": "现场决赛（第2届）", "start_date": "2026-05-15", "end_date": "2026-05-16",
     "note": "228 个项目现场决赛；金奖 20、银奖 39、铜奖 58、优胜奖 88"},
    {"stage": "获奖名单公示（第2届）", "start_date": "2026-06-03", "end_date": "2026-06-05",
     "note": "湖南省教育厅代章公示"},
    {"stage": "正式公布（第2届）", "start_date": "2026-07-01", "end_date": "2026-07-08",
     "note": "附件 1—7 分赛道获奖名单 + 附件 8 优秀组织奖"},
]

FIELDS = {
    "name": NAME,
    "short_name": SHORT_NAME,
    "category": "创业",
    "level": "provincial",
    "level_label": "省级",
    "has_project_awards": 1,
    "is_active": 1,
    "official_url": OFFICIAL_URL,
    "discover_source": NOTICE_2026,
    "intro": INTRO,
    "summary": INTRO,
    "participants": PARTICIPANTS,
    "content_scope": CONTENT_SCOPE,
    "exam_schedule": EXAM_SCHEDULE,
    "stage_schedule": json.dumps(STAGE_SCHEDULE, ensure_ascii=False),
    "reg_start_month": 9,
    "reg_end_month": 12,
    "reg_cross_year": 1,
    "contest_start_month": 5,
    "contest_end_month": 6,
    "tech_stack": "[]",
}


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="特批赛事：湖南省金种子杯 入库")
    parser.add_argument("--apply", action="store_true", help="真写库")
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args(argv)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    cols = {r["name"] for r in con.execute("PRAGMA table_info(competitions)")}
    payload = {k: v for k, v in FIELDS.items() if k in cols}

    existing = con.execute(
        "select * from competitions where name = ?", (NAME,)
    ).fetchone()
    if existing:
        print("已存在：id=%s %s（level=%s, is_active=%s, has_project_awards=%s）"
              % (existing["id"], existing["name"], existing["level"],
                 existing["is_active"], existing["has_project_awards"]))
        if args.apply:
            sets = ", ".join("%s = ?" % k for k in payload)
            con.execute("update competitions set %s where id = ?" % sets,
                        list(payload.values()) + [existing["id"]])
            con.commit()
            print("已更新关键字段（%d 个）" % len(payload))
        cid = existing["id"]
    else:
        keys = list(payload)
        con.execute(
            "insert into competitions (%s) values (%s)"
            % (", ".join(keys), ", ".join("?" for _ in keys)),
            [payload[k] for k in keys],
        )
        con.commit()
        cid = con.execute("select id from competitions where name = ?", (NAME,)).fetchone()[0]
        print("已插入：id=%s" % cid if args.apply else "dry-run：将插入，id 预计=%s" % cid)

    if args.apply:
        row = con.execute(
            "select id, name, category, level, level_label, has_project_awards, is_active "
            "from competitions where id = ?", (cid,)
        ).fetchone()
        print("落库结果：", dict(row))
        print("该赛事现有 awards：",
              con.execute("select count(*) from awards where competition_id = ?", (cid,)).fetchone()[0])
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())