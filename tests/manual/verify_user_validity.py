"""验证 #162 账号有效期：列迁移 + add_user/update_user + 校验逻辑 + authenticate 透传。

临时库副本，不影响真实演示库。
"""
import os, sys, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, "data", "guibeicai.db")
tmp = tempfile.mkdtemp(prefix="userval_")
TMP_DB = os.path.join(tmp, "t.db")
shutil.copyfile(SRC, TMP_DB)
os.environ["SQLITE_PATH"] = TMP_DB

from guibeicai.web import _db

_db.init_db()

# 1) 列迁移
c = _db.get_conn().cursor()
cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
assert "valid_from" in cols and "valid_until" in cols, f"缺失有效期列: {cols}"
print("[OK] users.valid_from / valid_until 列已迁移")

# 2) user_validity_status 纯逻辑
today = __import__("datetime").date.today().isoformat()
future = __import__("datetime").date.fromisoformat(today)
from datetime import date, timedelta
vf = (date.today() + timedelta(days=5)).isoformat()      # 未来生效
vu = (date.today() - timedelta(days=5)).isoformat()      # 已过期
assert _db.user_validity_status({"valid_from": None, "valid_until": None}) == (True, "")
assert _db.user_validity_status({"valid_from": vf, "valid_until": None})[0] is False
assert _db.user_validity_status({"valid_from": None, "valid_until": vu})[0] is False
assert _db.user_validity_status({"valid_from": None, "valid_until": (date.today() + timedelta(days=5)).isoformat()})[0] is True
print("[OK] user_validity_status 逻辑正确（未生效/已过期/有效）")

# 3) add_user 写入有效期
ok = _db.add_user("valid_test_user", "secret123", "merchant", [1], valid_from=vf, valid_until=None)
assert ok, "add_user 失败"
u = _db.authenticate_user("valid_test_user", "secret123")
assert u is not None, "认证失败"
assert u.get("valid_from") == vf and u.get("valid_until") is None, f"有效期未透传: {u}"
print("[OK] add_user + authenticate_user 有效期透传")

# 4) 过期账号：登录态校验应判为失效
assert _db.user_validity_status(u)[0] is False, "未来生效账号应判为未生效"
print("[OK] 未来生效账号 user_validity_status=False（登录将被拦截）")

# 5) update_user 改有效期（清空为长期）
_db.update_user(u["id"], valid_from="", valid_until="")
u2 = _db.authenticate_user("valid_test_user", "secret123")
assert (u2.get("valid_from") or None) is None and (u2.get("valid_until") or None) is None, f"清空失败: {u2}"
assert _db.user_validity_status(u2)[0] is True, "清空后应长期有效"
print("[OK] update_user 清空有效期 -> 长期有效")

# 6) get_all_users / get_user_by_id 含有效期字段
allu = _db.get_all_users()
assert any(x["username"] == "valid_test_user" for x in allu)
byid = _db.get_user_by_id(u["id"])
assert "valid_from" in byid and "valid_until" in byid
print("[OK] get_all_users / get_user_by_id 含有效期字段")

print("\nALL_USER_VALIDITY_OK")
shutil.rmtree(tmp, ignore_errors=True)
