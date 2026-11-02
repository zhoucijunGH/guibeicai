"""验证 #161 POS 自动对接：自动建档 + 自动配方触发 + 对账回灌预测历史。

对演示库做临时拷贝，不影响真实数据。
"""
import os, sys, tempfile, shutil

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

SRC = os.path.join(ROOT, "data", "guibeicai.db")
tmp = tempfile.mkdtemp(prefix="posdock_")
TMP_DB = os.path.join(tmp, "t.db")
shutil.copyfile(SRC, TMP_DB)
os.environ["SQLITE_PATH"] = TMP_DB
print("TMP_DB =", TMP_DB)

from guibeicai.web import _db
from guibeicai.data_import import pos_client

_db.init_db()

# 1) reconciled 列迁移已生效
c = _db.get_conn().cursor()
cols = [r[1] for r in c.execute("PRAGMA table_info(actual_sales)").fetchall()]
assert "reconciled" in cols, f"reconciled 列缺失: {cols}"
print("[OK] actual_sales.reconciled 列已迁移")

# 取一个真实门店
stores = _db.get_accessible_stores({"role": "系统管理员"}) if False else None
store = _db.get_conn().execute("SELECT id, name FROM stores LIMIT 1").fetchone()
store_id = store[0]
print(f"[INFO] 测试门店 #{store_id} {store[1]}")

# 2) 模拟 POS 拉取：一条全新菜名 + 一条已有菜名，走 normalize_and_upsert
existing = _db.get_dishes_by_store(store_id, active_only=True)
existing_name = existing[0]["name"] if existing else "测试老菜"
new_dish_name = "POS_AUTO_NEW_DISH_测试_自动建档"
records = [
    {"dish_name": existing_name, "sale_date": "2026-09-09", "qty": 5, "amount": 50},
    {"dish_name": new_dish_name, "sale_date": "2026-09-09", "qty": 3, "amount": 30},
]
# 关闭 auto_regen_bom 先验证建档与回灌（避免 LLM 依赖），再单独验证 regen 触发分支
stats = pos_client.normalize_and_upsert(store_id, records, "2026-09-09", {"auto_create_dish": True, "auto_regen_bom": False})
print("[STATS no-bom]", stats)
assert stats["auto_created"] == 1, f"应自动建档 1 个新菜, got {stats['auto_created']}"
assert stats["upserted"] == 2, f"应写入 2 条, got {stats['upserted']}"
# 校验 reconciled=1 已落库
recs = _db.get_conn().execute(
    "SELECT COUNT(*) FROM actual_sales WHERE store_id=? AND sale_date='2026-09-09' AND reconciled=1", (store_id,)
).fetchone()[0]
assert recs == 2, f"reconciled 应为 2, got {recs}"
print("[OK] 自动建档 + actual_sales.reconciled=1 落库")

# 3) 回灌 sales_history
bf = _db.backfill_history_from_actual(store_id=store_id, sale_date="2026-09-09", only_reconciled=True)
print("[BACKFILL]", bf)
assert bf["upserted"] == 2, f"回灌应为 2 行, got {bf['upserted']}"
hist = _db.get_conn().execute(
    "SELECT COUNT(*) FROM sales_history WHERE store_id=? AND sale_date='2026-09-09'", (store_id,)
).fetchone()[0]
assert hist == 2, f"sales_history 应有 2 行, got {hist}"
print("[OK] actual_sales -> sales_history 回灌成功")

# 4) 触发 regen_bom 分支（含一个全新的菜，auto_regen_bom=True）
new_dish2 = "POS_AUTO_NEW_DISH_测试_再建档"
records2 = [
    {"dish_name": existing_name, "sale_date": "2026-09-09", "qty": 5, "amount": 50},
    {"dish_name": new_dish2, "sale_date": "2026-09-09", "qty": 2, "amount": 20},
]
stats2 = pos_client.normalize_and_upsert(store_id, records2, "2026-09-09", {"auto_create_dish": True, "auto_regen_bom": True})
print("[STATS with-bom]", stats2)
assert stats2["auto_created"] == 1, f"应再自动建档 1 个新菜, got {stats2['auto_created']}"
assert stats2["auto_bom_triggered"] is True, "应触发配方生成"
# 不论 LLM 是否配置，都不应抛异常
print("[OK] auto_regen_bom 分支执行无异常 (auto_bom_ok=%s)" % stats2.get("auto_bom_ok"))

# 5) 幂等：重复回灌不增行（UNIQUE(store,dish,date) 去重，不重复造行）
bf2 = _db.backfill_history_from_actual(store_id=store_id, sale_date="2026-09-09", only_reconciled=True)
hist_count = _db.get_conn().execute(
    "SELECT COUNT(*) FROM sales_history WHERE store_id=? AND sale_date='2026-09-09'", (store_id,)
).fetchone()[0]
assert hist_count == 3, f"sales_history 应有 3 个去重菜品行, got {hist_count}"
bf3 = _db.backfill_history_from_actual(store_id=store_id, sale_date="2026-09-09", only_reconciled=True)
assert bf3["upserted"] == 3, f"第三次回灌仍应为 3 行(无重复), got {bf3['upserted']}"
print("[OK] 回灌幂等（去重无重复行）")

print("\nALL_POS_AUTODOCK_OK")
shutil.rmtree(tmp, ignore_errors=True)
