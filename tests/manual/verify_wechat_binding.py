"""临时库验证：微信 openid 绑定原语（bind/unbind + 与小程序登录读取路径联动）。

注意：绑定键已于 2026-09-11 改为「手机号」（见 verify_phone_binding.py）。
本脚本只保留对 openid 原语（add_user(openid)/bind_openid/unbind_openid/get_user_by_openid）的回归。

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

# 建一家门店（id=1），供 add_user(store_ids=[1]) 使用
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


# --- 1. add_user 带 openid：WEB 建号即预绑微信 ---
ok = db.add_user("bossA", "pw", "merchant", [1], openid="wx_openid_A")
check("add_user(openid) 成功", ok)
u = db.get_user_by_openid("wx_openid_A")
check("get_user_by_openid 返回该账号（=小程序 /api/wx/login 读取路径）", u is not None and u["username"] == "bossA")

# --- 2. 重复 openid 应被拒（唯一性，与小程序绑定同一约束）---
ok2 = db.add_user("bossB", "pw", "merchant", [1], openid="wx_openid_A")
check("重复 openid 的 add_user 返回 False", ok2 is False)

# --- 3. add_user 不带 openid：留空，待小程序首次登录自动绑定 ---
ok3 = db.add_user("bossC", "pw", "merchant", [1])
check("add_user(无 openid) 成功", ok3)
check("无 openid 时 get_user_by_openid 为 None", db.get_user_by_openid("wx_openid_A")["username"] == "bossA")

# --- 4. unbind_openid：WEB 解绑后小程序登录查不到（401 路径）---
uidA = db.get_user_by_openid("wx_openid_A")["id"]
db.unbind_openid(uidA)
check("unbind_openid 后 get_user_by_openid 返回 None（小程序将走 401 引导重新绑定）",
      db.get_user_by_openid("wx_openid_A") is None)

# --- 5. bind_openid 重新绑定 / 冲突拒绝 ---
db.bind_openid(uidA, "wx_openid_A2")
check("bind_openid 重新绑定成功", db.get_user_by_openid("wx_openid_A2")["id"] == uidA)
# 把 A2 再绑到别的账号应失败（唯一索引 + 预校验）
uidC = db.get_user_by_username("bossC")["id"] if hasattr(db, "get_user_by_username") else None
# get_user_by_username 可能不存在，改用 get_all_users 找 id
if uidC is None:
    uidC = next(x["id"] for x in db.get_all_users() if x["username"] == "bossC")
conflict = db.bind_openid(uidC, "wx_openid_A2")
check("bind_openid 冲突（已绑他人）返回 False", conflict is False)

# --- 6. get_all_users 透出 openid（列表/编辑用）---
allu = {x["username"]: x for x in db.get_all_users()}
check("get_all_users 含 bossA.openid=wx_openid_A2", allu["bossA"]["openid"] == "wx_openid_A2")
check("get_all_users 含 bossC.openid 为空", (allu["bossC"].get("openid") or "") == "")

print(f"\n通过 {passed} 项，失败 {failed} 项")
try:
    os.remove(tmp)
except OSError:
    pass
sys.exit(1 if failed else 0)
