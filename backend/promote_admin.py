# -*- coding: utf-8 -*-
"""promote_admin.py —— 极简管理员标记脚本

用法（backend 目录）：
  .\\venv\\Scripts\\python.exe promote_admin.py xas            # 设为管理员
  .\\venv\\Scripts\\python.exe promote_admin.py xas alice      # 批量
  .\\venv\\Scripts\\python.exe promote_admin.py --revoke xas   # 取消管理员
  .\\venv\\Scripts\\python.exe promote_admin.py --list         # 查看当前管理员

做的事：
  1. 先跑一遍 db.init_db()，确保老库补齐 users.is_admin / feedbacks.is_read 列；
  2. 按用户名（自动转小写）把 users.is_admin 置 1 / 0。

只改标记，不动密码、不动任何业务数据。
"""

import sys

import db


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    db.init_db()

    if not args or args[0] in ("--list", "-l"):
        rows = [u for u in _all_users() if u["is_admin"]]
        if not rows:
            print("[管理员] 当前没有任何管理员，请指定用户名，例如：promote_admin.py xas")
            return 0
        print("[管理员] 共 %d 个：" % len(rows))
        for row in rows:
            print("  id=%-3s username=%-20s created_at=%s"
                  % (row["id"], row["username"], row["created_at"]))
        return 0

    revoke = False
    if args[0] in ("--revoke", "-r"):
        revoke = True
        args = args[1:]
    if not args:
        print("请提供用户名")
        return 2

    failed = []
    for username in args:
        ok = db.set_user_admin_by_username(username, not revoke)
        if ok:
            print("[%s] %s" % ("取消管理员" if revoke else "设为管理员", username))
        else:
            failed.append(username)
            print("[跳过] 用户不存在：%s" % username)

    if failed:
        return 1
    return 0


def _all_users() -> list:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT id, username, created_at, is_admin FROM users ORDER BY id"
        ).fetchall()
    return [dict(row) for row in rows]


if __name__ == "__main__":
    raise SystemExit(main())
