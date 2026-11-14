# -*- coding: utf-8 -*-
"""seed_jzz_competition.py —— 特批赛事「湖南省“金种子杯”大学生创业大赛」入库（幂等）。

用法：
    venv\\Scripts\\python.exe seed_jzz_competition.py            # dry-run
    venv\\Scripts\\python.exe seed_jzz_competition.py --apply    # 真写库

数据口径（2026-11-14 核对官方口径，当前处于**第三届**报名期）：
  * 第三届：2026-09-29 启动报名，2026-12-31 截止；2027-01 资格审查；
    2027-02—03 网络初评；2027-04 中上旬 网络复评；2027-05 上旬 现场决赛 + 颁奖。
  * 报名官网：https://hnjzz.hunan-data.com
  * 奖池：金奖 18、银奖 36、铜奖 54、优胜奖若干；金/银/铜分别奖 10 万/3 万/1 万元。

该赛事不在教育部 84 项目录（省级），已在 competition_whitelist.SPECIAL_APPROVED 特批，
gate()/清理脚本不会再把它降级或删除。脚本幂等：按 name 命中则更新字段。
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
SHORT_NAME = "金种"
OFFICIAL_URL = "https://hnjzz.hunan-data.com"
NOTICE_SOURCE = "https://cyxy.xtu.edu.cn/info/1024/2567.htm"

INTRO = (
    "湖南省人民政府主办的省级大学生创业赛事，2025 年创办，每年一届，是湖南支持大学生创业"
    "「七个一」行动的核心抓手。面向全球大学生（2026 年第三届起放宽至毕业五年内、即 2022-01-01 "
    "后毕业的本硕博与技师学院学生），不限地域、户籍和国籍。第三届（2026-09-29 启动报名）设"
    "先进制造与高端装备、人工智能与信息技术、新能源与新材料、空天海洋与低空经济、现代农业与大健康、"
    "新消费与 AI+文化科技 6 个主赛道，金奖 18 项、银奖 36 项、铜奖 54 项，金奖 10 万元、"
    "银奖 3 万元、铜奖 1 万元，指导教师团队同步奖励。前两届累计产生 364 个获奖项目"
    "（2025 首届 159 个、2026 第二届 205 个），平台已全部收录并标注技术栈。"
)

PARTICIPANTS = (
    "1. 参赛对象为正在创业或已创办企业的大学生个人/团队，不限地域、户籍和国籍；"
    "第三届起「大学生」含全日制普通高校与技师学院在校生，以及毕业五年内（2022-01-01 后毕业）的"
    "专科生、本科生、硕士研究生、博士研究生和技师学院学生；\n"
    "2. 已注册企业项目要求团队中持股≥10% 的大学生不少于 1 人，可直接申报；"
    "省内未注册企业项目须经高校、园区或企业推荐，外省及海外项目可直接系统申报；\n"
    "3. 团队成员不超过 15 人，每名学生最多担任 1 个项目的负责人，可作为成员参与其他项目；"
    "每项目只能选 1 个赛道，不得多报；\n"
    "4. 鼓励跨校、跨地域、跨国别联合组队与师生共创（教师不得担任团队负责人、不参与路演答辩）；"
    "可配备不超过 5 名校内外导师并确定第一指导教师。"
)

CONTENT_SCOPE = (
    "1. 第三届设 6 个主赛道：先进制造与高端装备、人工智能与信息技术、新能源与新材料、"
    "空天海洋与低空经济、现代农业与大健康产业、新消费与 AI+文化科技；\n"
    "2. 奖项设置：金奖 18 项、银奖 36 项、铜奖 54 项、优胜奖若干；金/银/铜团队分别奖励 "
    "10 万元 / 3 万元 / 1 万元，指导教师团队分别奖励 3 万元 / 1 万元 / 0.5 万元；\n"
    "3. 项目可为商业计划书、产品原型或创意说明，须真实合法、知识产权或物权清晰；"
    "已获往届「金种子杯」金、银、铜奖的项目不得重复参赛；\n"
    "4. 大赛期间配套项目路演、投融资洽谈会、「创业嘉年华」、园区落地对接等活动，"
    "获奖项目可直接对接湖南省大学生创业投资基金。"
)

EXAM_SCHEDULE = (
    "第三届（当前届）赛程：\n"
    "1. 参赛报名：2026-09-29 — 2026-12-31，登录 https://hnjzz.hunan-data.com 填报；"
    "省内高校须先办校赛并推荐；\n"
    "2. 资格审查：2027 年 1 月底前完成，核验身份、团队构成、项目合规性；\n"
    "3. 网络初评：2027 年 2—3 月；网络复评：2027 年 4 月中上旬，专家评审结合 AI 智能体打分；\n"
    "4. 现场决赛：2027 年 5 月上旬（路演答辩），决赛后举行颁奖仪式与创投对接活动。\n"
    "往届（均已结束，仅供时间规律参考）：首届 2025 年 3 月启动、5 月颁奖；"
    "第二届 2025 年 9 月报名、2026 年 5 月决赛、7 月 8 日公布全部获奖名单。"
)

STAGE_SCHEDULE = [
    {"stage": "参赛报名（第三届）", "start_date": "2026-09-29", "end_date": "2026-12-31",
     "note": "官网 https://hnjzz.hunan-data.com；省内未注册企业项目须高校/园区/企业推荐"},
    {"stage": "资格审查（第三届）", "start_date": "2027-01-01", "end_date": "2027-01-31",
     "note": "核验参赛身份、团队构成、项目合规；往届金银铜奖项目不得重复参赛"},
    {"stage": "网络初评（第三届）", "start_date": "2027-02-01", "end_date": "2027-03-31",
     "note": "专家评审 + AI 智能体打分"},
    {"stage": "网络复评（第三届）", "start_date": "2027-04-01", "end_date": "2027-04-20",
     "note": "4 月中上旬完成，筛出入围现场决赛项目"},
    {"stage": "现场决赛（第三届）", "start_date": "2027-05-01", "end_date": "2027-05-10",
     "note": "5 月上旬路演答辩；决赛后颁奖仪式 + 创投对接、创业嘉年华"},
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
    "discover_source": NOTICE_SOURCE,
    "intro": INTRO,
    "summary": INTRO,
    "participants": PARTICIPANTS,
    "content_scope": CONTENT_SCOPE,
    "exam_schedule": EXAM_SCHEDULE,
    "stage_schedule": json.dumps(STAGE_SCHEDULE, ensure_ascii=False),
    "reg_start_month": 9,
    "reg_end_month": 12,
    "reg_cross_year": 0,
    "contest_start_month": 5,
    "contest_end_month": 5,
    "reg_start_date": "2026-09-29",
    "reg_end_date": "2026-12-31",
    "contest_start_date": "2027-05-01",
    "contest_end_date": "2027-05-10",
    "tech_stack": "[]",
}


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser(description="特批赛事：湖南省金种子杯 入库/更新")
    parser.add_argument("--apply", action="store_true", help="真写库")
    parser.add_argument("--db", default=str(DB_PATH))
    args = parser.parse_args(argv)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    cols = {r["name"] for r in con.execute("PRAGMA table_info(competitions)")}
    payload = {k: v for k, v in FIELDS.items() if k in cols}

    existing = con.execute("select * from competitions where name = ?", (NAME,)).fetchone()
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
            "select id, name, level_label, has_project_awards, is_active, reg_start_date, reg_end_date, "
            "contest_start_date, contest_end_date, length(stage_schedule) ss_len from competitions where id = ?",
            (cid,),
        ).fetchone()
        print("落库结果：", dict(row))
        stages = json.loads(con.execute(
            "select stage_schedule from competitions where id = ?", (cid,)).fetchone()[0])
        for s in stages:
            print("   阶段:", s["stage"], s["start_date"], "~", s["end_date"])
        print("该赛事现有 awards：",
              con.execute("select count(*) from awards where competition_id = ?", (cid,)).fetchone()[0])
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())