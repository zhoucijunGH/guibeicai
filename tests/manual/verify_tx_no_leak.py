"""验证 _tx() 事务上下文：异常路径不再泄漏连接、不再锁死后续写操作。

复现原缺陷：
  add_user 传入不存在的 store_id → user_stores 外键报错 → 旧代码 except: return False
  且不关连接 → 悬空写事务持锁 → 之后所有写操作报 database is locked。
修复后：失败自动回滚并关连接，后续写操作正常。

用法： python tests/manual/verify_tx_no_leak.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

_d = tempfile.mkdtemp(prefix="verify_tx_")
os.environ["SQLITE_PATH"] = os.path.join(_d, "tx.db")

from guibeicai.web import _db  # noqa: E402

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}")


print("== 场景1：add_user 传不存在的 store_id（触发外键失败）==")
_db.init_db()
# 建一个合法门店，拿到真实 store id
with _db._tx() as conn:
    c = conn.cursor()
    c.execute("INSERT INTO stores(name, city, status) VALUES (?, ?, ?)", ("合法门店", "X", "active"))
    good_sid = c.lastrowid
print(f"  合法 store_id = {good_sid}")

r_bad = _db.add_user("bad_store_user", "pw123456", "merchant", [99999], phone="13900000001")
check("add_user(不存在的门店) 返回 False（不再误报成功）", r_bad is False)
check("半成品账号未落库（原子回滚）", _db.get_user_by_phone("13900000001") is None)

print("== 场景2：失败之后，后续写操作必须仍可用（旧代码此处会 database is locked）==")
try:
    _db.update_user(1, username="admin")  # 对既有账号做一次写
    _db.bind_openid(1, "oTXPROBE1")
    _db.unbind_openid(1)
    ok_write = True
except Exception as e:  # noqa: BLE001
    ok_write = False
    print(f"     写操作异常：{type(e).__name__}: {e}")
check("失败后立即写操作（update/bind/unbind）正常，未被写锁卡死", ok_write)

print("== 场景3：add_user 合法门店 → 成功且门店绑定同事务生效 ==")
r_ok = _db.add_user("good_user", "pw123456", "merchant", [good_sid], phone="13900000002")
u = _db.get_user_by_phone("13900000002")
check("add_user(合法门店) 返回 True", r_ok is True)
check("用户已落库", u is not None)
check("门店绑定已生效", u is not None and good_sid in (_db.get_user_store_ids(u["id"]) or []))

print("== 场景4：重复用户名 → 返回 False，且不再锁死后续写 ==")
r_dup = _db.add_user("good_user", "pw123456", "merchant", [good_sid])
check("重复用户名 add_user 返回 False", r_dup is False)
try:
    _db.update_user(1, username="admin")
    ok_after_dup = True
except Exception as e:  # noqa: BLE001
    ok_after_dup = False
    print(f"     写操作异常：{type(e).__name__}: {e}")
check("重复用户名失败后，后续写操作仍正常", ok_after_dup)

print("== 场景5：set_user_store_ids 传非法门店 → 抛错但不锁死后续写 ==")
try:
    _db.set_user_store_ids(1, [99999])
except Exception:
    pass
try:
    _db.update_user(1, username="admin")
    ok_after_susi = True
except Exception as e:  # noqa: BLE001
    ok_after_susi = False
    print(f"     写操作异常：{type(e).__name__}: {e}")
check("set_user_store_ids 失败后，后续写操作仍正常", ok_after_susi)

print(f"\n通过 {passed} 项，失败 {failed} 项")
try:
    import shutil
    shutil.rmtree(_d, ignore_errors=True)
except Exception:
    pass
sys.exit(1 if failed else 0)
