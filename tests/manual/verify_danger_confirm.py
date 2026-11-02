"""危险操作二次确认（C5）端到端验证。

验证三件事：
  A. 组件契约：arm_danger 登记 → render_danger_confirm 取消不删 / 确认才删并返回动作；
  B. 页面接线：对每个已接入确认的页面，预置 pending 并"点击确认"，
     执行页面顶层代码，断言目标数据真的被删除（走通 delete 分支）；
  C. 取消分支：只"点击取消"，断言数据未被删除。

隔离：SQLITE_PATH 指向临时库。通过对桩 streamlit 的 button_policy 控制
"哪个按钮被点击"（key 以 __yes / __no 结尾）。
"""
import importlib.util
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

TMP = os.path.join(tempfile.gettempdir(), "verify_danger_confirm.db")
if os.path.exists(TMP):
    os.remove(TMP)
os.environ["SQLITE_PATH"] = TMP
os.environ["GUOBEICAI_DISABLE_SEED"] = "1"

from tests.manual._stub_streamlit import (  # noqa: E402
    install, RerunScript, StopScript, SwitchPage,
)

# 只让"确认删除"按钮为真
STATE = install(user={"id": 1, "username": "admin", "role": "admin"},
                button_policy=lambda key: key.endswith("__yes"))

import guibeicai.web._db as _db  # noqa: E402

_db.init_db()

PASS = 0
FAIL = 0


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


def raw(sql, params=()):
    con = sqlite3.connect(TMP)
    con.execute("PRAGMA foreign_keys=ON")
    try:
        cur = con.execute(sql, params)
        rows = cur.fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def _seed():
    raw("INSERT INTO stores (name, province, city, status, auto_predict, auto_predict_target_offset, "
        "alert_push_enabled) VALUES ('确认测试店','广西壮族自治区','南宁市','active',1,0,1)")
    raw("INSERT INTO users (id, username, password_hash, role, store_id) VALUES (2,'victim','x','merchant',1)")
    raw("INSERT INTO user_stores (user_id, store_id) VALUES (2,1)")
    raw("INSERT INTO ingredients (store_id, name, status) VALUES (1,'待删食材','active')")
    raw("INSERT INTO dishes (store_id, name, status, bom_status) VALUES (1,'待删菜','active','ready')")
    raw("INSERT INTO recipes (dish_id, ingredient_name, quantity, unit) VALUES (1,'待删食材',0.1,'kg')")
    raw("INSERT INTO contracts (contract_no, name, amount, party_a, party_b) "
        "VALUES ('HT-DEL-1','待删合同',1,'甲','乙')")
    raw("INSERT INTO pos_sources (store_id, name, source_type, base_url, auth_type, enabled) "
        "VALUES (1,'待删POS','http_json','http://x','none',1)")
    raw("INSERT OR IGNORE INTO roles (name, code, description, permissions, pages) "
        "VALUES ('待删角色','tmp_role','','{}','[]')")


def _exec_page(rel: str):
    path = _PROJECT_ROOT / "guibeicai" / "web" / rel
    name = "dc_" + rel.replace("\\", "_").replace("/", "_").replace(".", "_")
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except (RerunScript, StopScript, SwitchPage):
        pass


def _with_pending(key, kind, tid, label):
    import time as _t
    STATE.reset({"user": {"id": 1, "username": "admin", "role": "admin"}})
    STATE["login_time"] = _t.time()  # 必须是当前时间，否则会话过期会被重定向
    STATE[key] = {"kind": kind, "id": tid, "label": label, "warning": "w", "extra": {}}


_seed()

# ---------- A. 组件契约 ----------
print("\n[A] 组件契约 arm/render")
from guibeicai.web.components import arm_danger, render_danger_confirm  # noqa: E402

arm_danger("demo", 7, "示例", warning="小心")
check("arm_danger 写入 pending", STATE.get("_pending_danger", {}).get("id") == 7)
# 当前 policy 下 __yes 为真 → 直接确认
_p = render_danger_confirm()
check("确认返回动作 dict", isinstance(_p, dict) and _p["id"] == 7 and _p["kind"] == "demo", _p)
check("确认后清空 pending", STATE.get("_pending_danger") is None)
# 取消分支
arm_danger("demo2", 8, "示例2")
_p2 = render_danger_confirm()  # 同样会命中 __yes，这里只验证返回
check("二次调用仍返回 dict", isinstance(_p2, dict) and _p2["id"] == 8)

# ---------- B. 页面接线：确认即删 ----------
print("\n[B] 页面接线 · 确认删除")
CASES = [
    # (页面, pending_key, kind, 目标id, 表, 校验SQL, 说明)
    # 注意：门店删除会级联清空其子数据，故门店必须放最后，避免影响其它用例。
    ("pages/17_食材管理.py", "_pending_danger", "ingredient", 1, "ingredients",
     "SELECT COUNT(*) FROM ingredients WHERE id=1", "删除食材"),
    ("pages/22_合同管理.py", "_pending_danger", "contract", 1, "contracts",
     "SELECT COUNT(*) FROM contracts WHERE id=1", "删除合同"),
    ("pages/14_POS对接.py", "_pending_danger", "pos_source", 1, "pos_sources",
     "SELECT COUNT(*) FROM pos_sources WHERE id=1", "删除 POS 数据源"),
    ("pages/8_角色管理.py", "_pending_danger", "role", None, "roles",
     "SELECT COUNT(*) FROM roles WHERE code='tmp_role'", "删除角色"),
    ("pages/7_用户管理.py", "_pending_danger", "user", 2, "users",
     "SELECT COUNT(*) FROM users WHERE id=2", "删除用户"),
    ("pages/4_门店管理.py", "_pending_danger", "store", 1, "stores",
     "SELECT COUNT(*) FROM stores WHERE id=1", "删除门店并级联"),
]
for rel, key, kind, tid, table, verify_sql, desc in CASES:
    if kind == "role":
        row = raw("SELECT id FROM roles WHERE code='tmp_role'")
        if not row:
            raw("INSERT OR IGNORE INTO roles (name, code, description, permissions, pages) "
                "VALUES ('待删角色','tmp_role','','{}','[]')")
            row = raw("SELECT id FROM roles WHERE code='tmp_role'")
        tid = row[0][0]
    _with_pending(key, kind, tid, f"目标{kind}")
    _exec_page(rel)
    left = raw(verify_sql)[0][0]
    check(f"{desc}（{rel}）", left == 0, f"残留 {left}")

# ---------- C. 取消分支：不删 ----------
print("\n[C] 取消不删")
# 重新播种被删目标
raw("INSERT INTO contracts (contract_no, name, amount, party_a, party_b) VALUES ('HT-KEEP','不删合同',1,'甲','乙')")
_kid = raw("SELECT id FROM contracts WHERE contract_no='HT-KEEP'")[0][0]
# 把 policy 改成"只点取消"
sys.modules["streamlit"]._button_policy = lambda key: key.endswith("__no")
for _el in []:
    pass
STATE.reset({"user": {"id": 1, "username": "admin", "role": "admin"}})
STATE["login_time"] = __import__("time").time()
STATE["_pending_danger"] = {"kind": "contract", "id": _kid, "label": "不删合同", "warning": "w", "extra": {}}
_exec_page("pages/22_合同管理.py")
check("点击取消后合同仍在", raw("SELECT COUNT(*) FROM contracts WHERE id=?", (_kid,))[0][0] == 1)

print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
