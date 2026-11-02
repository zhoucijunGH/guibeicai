"""临时库验证：手机号为主绑定键（users.phone）+ 小程序微信绑定链路。

仅动临时库（SQLITE_PATH=tempfile），不碰真实演示数据。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

# ⚠️ 必须【先设 SQLITE_PATH 再 import _db】——_db.DB_PATH 在模块导入时求值，
#    顺序颠倒会把测试数据写进真实库 data/guibeicai.db（曾造成真实库污染）。
tmp = tempfile.mktemp(suffix=".db")
os.environ["SQLITE_PATH"] = tmp

import guibeicai.web._db as db  # noqa: E402

db.init_db()

# 建一家门店（id=1），供下方 add_user(store_ids=[1]) 使用（空库无门店会触发外键失败）
_c = db.get_conn()
try:
    _c.execute("INSERT INTO stores(name, city, status) VALUES (?,?,?)", ("测试门店", "南宁", "active"))
    _c.commit()
finally:
    _c.close()

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"✅ {name}")
    else:
        failed += 1
        print(f"❌ {name}")


# --- 0. 迁移：phone 列 + 部分唯一索引存在；init_db 幂等 ---
conn = db.get_conn()
cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
idx = [r[1] for r in conn.execute("PRAGMA index_list(users)").fetchall()]
conn.close()
check("users 表含 phone 列", "phone" in cols)
check("idx_users_phone 唯一索引存在", "idx_users_phone" in idx)
db.init_db()  # 二次调用不应报错（幂等）
check("init_db 二次调用幂等", True)

# --- 1. add_user 带 phone ---
check("add_user(phone) 成功", db.add_user("bossA", "pw", "merchant", [1], phone="13800000001"))
u = db.get_user_by_phone("13800000001")
check("get_user_by_phone 命中 bossA", u is not None and u["username"] == "bossA")

# --- 2. 手机号唯一性 ---
check("重复手机号 add_user 返回 False", db.add_user("bossB", "pw", "merchant", [1], phone="13800000001") is False)

# --- 3. 无手机号用户 ---
check("add_user(无 phone) 成功", db.add_user("bossC", "pw", "merchant", [1], phone="13800000003"))
check("空手机号查询返回 None", db.get_user_by_phone("") is None)

# --- 4. bind_openid_by_phone：成功 + 与小程序读取路径联动 ---
r = db.bind_openid_by_phone("13800000001", "wx_openid_1")
check("bind_openid_by_phone 成功", r["ok"] is True and r["reason"] is None)
linked = db.get_user_by_openid("wx_openid_1")
check("绑定后 get_user_by_openid 命中 bossA（=小程序 /api/wx/login 读取路径）",
      linked is not None and linked["username"] == "bossA")

# --- 5. 手机号未开通 ---
r = db.bind_openid_by_phone("13900000000", "wx_openid_2")
check("未开通手机号 → reason=no_user", r["ok"] is False and r["reason"] == "no_user")

# --- 6. openid 已被他人占用 ---
r = db.bind_openid_by_phone("13800000003", "wx_openid_1")
check("openid 已绑他人 → reason=openid_taken", r["ok"] is False and r["reason"] == "openid_taken")

# --- 7. 账号已过期 ---
db.add_user("bossD", "pw", "merchant", [1], phone="13800000004", valid_until="2000-01-01")
r = db.bind_openid_by_phone("13800000004", "wx_openid_4")
check("账号过期 → reason=inactive", r["ok"] is False and r["reason"] == "inactive")

# --- 8. 参数缺失 ---
r = db.bind_openid_by_phone("", "x")
check("缺手机号 → reason=bad_input", r["ok"] is False and r["reason"] == "bad_input")

# --- 9. get_all_users 透出 phone ---
allu = {x["username"]: x for x in db.get_all_users()}
check("get_all_users 透出 bossA.phone", allu["bossA"].get("phone") == "13800000001")
check("get_all_users 透出 bossC.phone", allu["bossC"].get("phone") == "13800000003")

print(f"\n通过 {passed} 项，失败 {failed} 项")
try:
    os.remove(tmp)
except OSError:
    pass
sys.exit(1 if failed else 0)
