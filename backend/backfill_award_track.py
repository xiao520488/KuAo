# -*- coding: utf-8 -*-
"""一次性维护：把「已入库但丢了 session/track」的 awards 行补回来。

背景：`db.create_award` 的 `_AWARD_WRITABLE_FIELDS` 早期没有 session / track，
而 `award_ingest.py` 会算这两个字段（也进了 content_hash），于是 2026-11-23
入库的 9,000+ 行 session/track 全被静默丢掉。修 db.py 只能管以后，历史行
用「重解析 → 按 content_hash 对齐 → UPDATE」补齐，不删不重建（id 不变）。

用法：
  python backfill_award_track.py                 # 演练，只报数字
  python backfill_award_track.py --apply         # 写库（先自动备份）
  python backfill_award_track.py --apply --db /path/to/competitions.db
"""
from __future__ import annotations

import argparse
import os
import sys

import db
import award_ingest as A


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="回填 awards.session / awards.track")
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只演练）")
    ap.add_argument("--db", default=None, help="sqlite 路径，默认 cwd/competitions.db")
    ap.add_argument("--source-id", type=int, action="append", default=None)
    ap.add_argument("--timeout", type=int, default=A.DEFAULT_TIMEOUT)
    args = ap.parse_args(argv)

    db_path = os.path.abspath(args.db or os.path.join(os.getcwd(), "competitions.db"))
    sources = db.list_award_sources(db_path=db_path)
    if args.source_id:
        wanted = set(args.source_id)
        sources = [s for s in sources if s["id"] in wanted]

    total_updated = 0
    with db.get_conn(db_path) as con:
        for src in sources:
            if (src.get("adapter") or "") == "manual":
                continue
            try:
                body, content_type, final_url = A.fetch_source(src, timeout=args.timeout)
                spec = A._spec_of(src)
                raw = A.parse_by_adapter(body, src.get("adapter") or "", spec)
                records = A.prepare_records(raw, spec, src)
            except Exception as exc:  # noqa: BLE001
                print(f"  [跳过] id={src['id']} cid={src['competition_id']} "
                      f"{type(exc).__name__}: {exc}")
                continue
            hits = 0
            for rec in records:
                h = A.content_hash(src["competition_id"], rec)
                row = con.execute(
                    "SELECT id, session, track FROM awards WHERE content_hash=?", (h,)
                ).fetchone()
                if not row:
                    continue
                new_session = rec.get("session") or None
                new_track = rec.get("track") or None
                if (new_session or None) == (row["session"] or None) and \
                        (new_track or None) == (row["track"] or None):
                    continue
                hits += 1
                if args.apply:
                    con.execute(
                        "UPDATE awards SET session=?, track=?, "
                        "updated_at=datetime('now','localtime') WHERE id=?",
                        (new_session, new_track, row["id"]),
                    )
            total_updated += hits
            print(f"  id={src['id']:>2} cid={src['competition_id']:>2} "
                  f"解析 {len(records):>5} 条，需补 {hits:>5} 条")

    print(f"\n合计{'已补' if args.apply else '待补（演练）'}：{total_updated} 行")
    if not args.apply:
        print("加 --apply 才会真正写库")
    return 0


if __name__ == "__main__":
    sys.exit(main())
