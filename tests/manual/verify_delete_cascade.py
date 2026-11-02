"""C2 验证：删除门店 / 菜品的级联清理。

背景：除 user_stores / store_supply_scope 外，子表外键均为 NO ACTION，
PRAGMA foreign_keys=ON 下若不显式清理子表，DELETE stores/dishes 会报
"FOREIGN KEY constraint failed"，即无法删除。

验证项：
  1. get_store_delete_impact 计数正确；
  2. 含全部子数据的门店可被 delete_store 成功删除（旧版本会 FK 报错）；
  3. 子表行、recipes、dishes、ingredients 全部清空，users.store_id 复位为 0；
  4. delete_dish 能删除带 actual_sales/sales_history/recipes 的菜品；
  5. 残留孤儿数据为 0；其他门店数据不受影响。
"""
import os, sys, tempfile, sqlite3

TMP = os.path.join(tempfile.gettempdir(), "probe_delete_cascade.db")
if os.path.exists(TMP):
    os.remove(TMP)
# 关键：先在 import _db 之前设置 SQLITE_PATH，否则会污染真实库
os.environ["SQLITE_PATH"] = TMP
os.environ["GUOBEICAI_DISABLE_SEED"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import guibeicai.web._db as db

db.init_db()

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


# ---------- 造数据 ----------
sid = db.add_store(name="级联测试店", province="广西", city="南宁") if hasattr(db, "add_store") else None
if sid is None:
    raw("INSERT INTO stores (name, province, city, status, auto_predict, auto_predict_target_offset, alert_push_enabled) "
        "VALUES ('级联测试店','广西','南宁','active',1,0,1)")
    sid = raw("SELECT id FROM stores WHERE name='级联测试店'")[0][0]
# 另一个"旁观"门店，验证互不影响
raw("INSERT INTO stores (name, province, city, status, auto_predict, auto_predict_target_offset, alert_push_enabled) "
    "VALUES ('旁观店','广西','南宁','active',1,0,1)")
other_sid = raw("SELECT id FROM stores WHERE name='旁观店'")[0][0]

# 本店：食材 + 菜品 + 配方
raw("INSERT INTO ingredients (store_id, name, status) VALUES (?,?,?)", (sid, "白菜", "active"))
iid = raw("SELECT id FROM ingredients WHERE store_id=? AND name='白菜'", (sid,))[0][0]
raw("INSERT INTO ingredients (store_id, name, status) VALUES (?,?,?)", (other_sid, "土豆", "active"))
raw("INSERT INTO dishes (store_id, name, status, bom_status) VALUES (?,?,?,?)", (sid, "醋溜白菜", "active", "ready"))
did = raw("SELECT id FROM dishes WHERE store_id=? AND name='醋溜白菜'", (sid,))[0][0]
raw("INSERT INTO dishes (store_id, name, status, bom_status) VALUES (?,?,?,?)", (other_sid, "炒土豆丝", "active", "ready"))
other_did = raw("SELECT id FROM dishes WHERE store_id=? AND name='炒土豆丝'", (other_sid,))[0][0]
raw("INSERT INTO recipes (dish_id, ingredient_id, ingredient_name, quantity) VALUES (?,?,?,?)",
    (did, iid, "白菜", 0.2))

# 各叶子表
raw("INSERT INTO actual_sales (store_id, dish_id, sale_date, actual_qty, reconciled) VALUES (?,?,?,?,1)",
    (sid, did, "2026-09-01", 10))
raw("INSERT INTO sales_history (store_id, dish_id, sale_date, qty) VALUES (?,?,?,?)",
    (sid, did, "2026-09-01", 12))
raw("INSERT INTO predictions (store_id, predict_date, run_time, pushed) VALUES (?,?,?,?)",
    (sid, "2026-09-12", "2026-09-11T06:30:00", 0))
raw("INSERT INTO date_factors (store_id, factor_date) VALUES (?,?)", (sid, "2026-09-12"))
raw("INSERT INTO backtest_runs (store_id) VALUES (?)", (sid,))
raw("INSERT INTO bias_corrections (store_id, dish_name, factor) VALUES (?,?,?)", (sid, "醋溜白菜", 1.1))
raw("INSERT INTO store_supply_scope (store_id, ingredient_norm) VALUES (?,?)", (sid, "白菜"))
raw("INSERT INTO pos_sources (store_id, name, source_type, base_url, auth_type, enabled) "
    "VALUES (?,?,?,?,?,?)", (sid, "POS-A", "http", "http://x", "none", 1))
pid = raw("SELECT id FROM pos_sources WHERE store_id=?", (sid,))[0][0]
raw("INSERT INTO pos_sync_log (source_id, store_id) VALUES (?,?)", (pid, sid))
raw("INSERT INTO store_alert_push_log (store_id, sent_date, sent_at) VALUES (?,?,?)",
    (sid, "2026-09-11", "2026-09-11T06:30:00"))
# 用户绑定 + 主门店列
raw("INSERT INTO users (username, password_hash, role, store_id) VALUES (?,?,?,?)",
    ("cascade_user", "x", "merchant", sid))
uid = raw("SELECT id FROM users WHERE username='cascade_user'")[0][0]
raw("INSERT INTO user_stores (user_id, store_id) VALUES (?,?)", (uid, sid))

# ---------- 1. impact ----------
print("\n[1] get_store_delete_impact 计数")
imp = db.get_store_delete_impact(sid)
check("dishes=1", imp.get("dishes") == 1, imp)
check("ingredients=1", imp.get("ingredients") == 1, imp)
check("recipes=1", imp.get("recipes") == 1, imp)
check("actual_sales=1", imp.get("actual_sales") == 1, imp)
check("sales_history=1", imp.get("sales_history") == 1, imp)
check("predictions=1", imp.get("predictions") == 1, imp)
check("pos_sources=1", imp.get("pos_sources") == 1, imp)
check("user_stores=1", imp.get("user_stores") == 1, imp)

# ---------- 2. delete_store ----------
print("\n[2] delete_store 应成功（旧版会 FK 报错）")
try:
    db.delete_store(sid)
    check("delete_store 无异常", True)
except Exception as e:
    check("delete_store 无异常", False, repr(e))

# ---------- 3. 子数据清空 ----------
print("\n[3] 子数据全部清空 / users 复位")
check("stores 本店已删", raw("SELECT COUNT(*) FROM stores WHERE id=?", (sid,))[0][0] == 0)
for t in ("dishes", "ingredients", "actual_sales", "sales_history", "predictions",
          "date_factors", "backtest_runs", "bias_corrections", "store_supply_scope",
          "pos_sources", "pos_sync_log", "store_alert_push_log", "user_stores"):
    n = raw(f"SELECT COUNT(*) FROM {t} WHERE store_id=?", (sid,))[0][0] if t != "pos_sync_log" \
        else raw("SELECT COUNT(*) FROM pos_sync_log WHERE source_id=?", (pid,))[0][0]
    check(f"{t} 清空", n == 0, f"残留 {n}")
check("recipes 清空", raw("SELECT COUNT(*) FROM recipes WHERE dish_id=?", (did,))[0][0] == 0)
check("users.store_id 复位为 0",
      raw("SELECT store_id FROM users WHERE id=?", (uid,))[0][0] == 0)

# ---------- 4. delete_dish ----------
print("\n[4] delete_dish 级联")
raw("INSERT INTO actual_sales (store_id, dish_id, sale_date, actual_qty, reconciled) VALUES (?,?,?,?,1)",
    (other_sid, other_did, "2026-09-02", 5))
raw("INSERT INTO sales_history (store_id, dish_id, sale_date, qty) VALUES (?,?,?,?)",
    (other_sid, other_did, "2026-09-02", 6))
try:
    db.delete_dish(other_did)
    check("delete_dish 无异常", True)
except Exception as e:
    check("delete_dish 无异常", False, repr(e))
check("dishes 已删", raw("SELECT COUNT(*) FROM dishes WHERE id=?", (other_did,))[0][0] == 0)
check("actual_sales 已删", raw("SELECT COUNT(*) FROM actual_sales WHERE dish_id=?", (other_did,))[0][0] == 0)
check("sales_history 已删", raw("SELECT COUNT(*) FROM sales_history WHERE dish_id=?", (other_did,))[0][0] == 0)

# ---------- 5. 旁观店不受影响 ----------
print("\n[5] 旁观店数据完好")
check("旁观店仍在", raw("SELECT COUNT(*) FROM stores WHERE id=?", (other_sid,))[0][0] == 1)
check("旁观店食材仍在", raw("SELECT COUNT(*) FROM ingredients WHERE store_id=?", (other_sid,))[0][0] == 1)

print(f"\n==== RESULT: {PASS} passed, {FAIL} failed ====")
sys.exit(1 if FAIL else 0)
