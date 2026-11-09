"""管理后台接口验收：在临时副本库上跑，不碰生产 competitions.db。"""

import os
import shutil
import sqlite3
import sys

BACKEND = r"D:\agent-lab\KuAo-Platform\backend"
TMP = r"D:\agent-lab\KuAo-Platform\.verify\admin_test.db"

sys.path.insert(0, BACKEND)

# 1) 用 sqlite backup API 复制一份库，避免 WAL 不一致
src = sqlite3.connect(os.path.join(BACKEND, "competitions.db"))
if os.path.exists(TMP):
    os.remove(TMP)
dst = sqlite3.connect(TMP)
src.backup(dst)
dst.close()
src.close()

import db  # noqa: E402

db.DB_PATH = TMP

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import security  # noqa: E402

client = TestClient(main.app)

admin_token = security.create_access_token(4, "xas")          # is_admin = 1
user_token = security.create_access_token(2, "alice")         # 普通用户
A = {"Authorization": "Bearer " + admin_token}
U = {"Authorization": "Bearer " + user_token}

ok = True


def check(label, cond, extra=""):
    global ok
    print(("  PASS  " if cond else "  FAIL  ") + label + ("  " + str(extra) if extra else ""))
    if not cond:
        ok = False


print("[1] 鉴权")
r = client.get("/api/admin/candidates")
check("无 token -> 401", r.status_code == 401, r.status_code)
r = client.get("/api/admin/candidates", headers=U)
check("普通用户 -> 403", r.status_code == 403, (r.status_code, r.json()))
r = client.get("/api/admin/candidates", headers=A)
check("管理员 -> 200", r.status_code == 200, r.status_code)
total = r.json()["total"]
items = r.json()["items"]
check("返回 pending 列表", total > 0 and len(items) > 0, "total=%s" % total)
sample = items[0]
print("       sample:", {k: sample[k] for k in ("id", "name", "level_label", "source")})

print("[2] 反馈提交（前端修复后的路径 /api/feedback）")
r = client.post("/api/feedback", json={"content": "管理后台验收反馈"}, headers=U)
check("POST /api/feedback -> 200", r.status_code == 200, (r.status_code, r.json()))
fb_id = r.json().get("id") if r.status_code == 200 else None
with db.get_conn() as conn:
    n = conn.execute("SELECT COUNT(*) FROM feedbacks").fetchone()[0]
check("已写入 feedbacks 表", n >= 1, "rows=%s" % n)
r = client.post("/api/api/feedback", json={"content": "x"}, headers=U)
check("旧错误路径 /api/api/feedback -> 404", r.status_code == 404, r.status_code)

print("[3] 反馈列表 / 标记已读")
r = client.get("/api/admin/feedback", headers=A)
check("GET /api/admin/feedback -> 200", r.status_code == 200, r.status_code)
body = r.json()
check("含刚提交的反馈", any(i["id"] == fb_id for i in body["items"]), "unread=%s" % body["unread"])
check("字段完整", all(k in body["items"][0] for k in ("content", "created_at", "is_read", "username")))
r = client.put("/api/admin/feedback/%s/read" % fb_id, headers=A)
check("PUT read -> 200", r.status_code == 200, r.status_code)
with db.get_conn() as conn:
    flag = conn.execute("SELECT is_read FROM feedbacks WHERE id = ?", (fb_id,)).fetchone()[0]
check("DB 中 is_read=1", flag == 1, flag)
r = client.put("/api/admin/feedback/%s/read" % fb_id, headers=U)
check("普通用户标记已读 -> 403", r.status_code == 403, r.status_code)
r = client.put("/api/admin/feedback/999999/read", headers=A)
check("不存在的反馈 -> 404", r.status_code == 404, r.status_code)

print("[4] 候选通过 / 拒绝")
cid = items[0]["id"]
r = client.post("/api/admin/candidates/%s/approve" % cid, headers=A)
check("approve -> 200", r.status_code == 200, (r.status_code, r.json()))
new_comp_id = r.json().get("competition_id") if r.status_code == 200 else None
if new_comp_id:
    with db.get_conn() as conn:
        row = conn.execute("SELECT id, name FROM competitions WHERE id = ?", (new_comp_id,)).fetchone()
    check("competitions 已入库", row is not None, dict(row) if row else None)
    with db.get_conn() as conn:
        st = conn.execute("SELECT status FROM competition_candidates WHERE id = ?", (cid,)).fetchone()[0]
    check("候选标记 approved", st == "approved", st)
r = client.post("/api/admin/candidates/%s/approve" % cid, headers=A)
check("重复 approve -> 400", r.status_code == 400, r.status_code)

cid2 = items[1]["id"]
r = client.post("/api/admin/candidates/%s/reject" % cid2, headers=A)
check("reject -> 200", r.status_code == 200, r.status_code)
with db.get_conn() as conn:
    st2 = conn.execute("SELECT status FROM competition_candidates WHERE id = ?", (cid2,)).fetchone()[0]
check("候选标记 rejected", st2 == "rejected", st2)
r = client.get("/api/admin/candidates", headers=A)
check("pending 总数减少 2", r.json()["total"] == total - 2, "%s -> %s" % (total, r.json()["total"]))
r = client.post("/api/admin/candidates/999999/approve", headers=A)
check("不存在的候选 -> 404", r.status_code == 404, r.status_code)

print("\nRESULT:", "ALL PASS" if ok else "HAS FAILURES")
sys.exit(0 if ok else 1)
