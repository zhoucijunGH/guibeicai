"""#160 验证：渠道工作台供货范围覆盖拆分逻辑（对临时库副本，不碰真实数据）"""
import os, sys, shutil, tempfile, json
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEMO = os.path.join(ROOT, "data", "guibeicai.db")
tmp = tempfile.mkdtemp(prefix="cov_")
tmp_db = os.path.join(tmp, "cov.db")
shutil.copyfile(DEMO, tmp_db)
os.environ["SQLITE_PATH"] = tmp_db
sys.path.insert(0, ROOT)

from guibeicai.web import _db
from guibeicai.web._ingredient_forecast import build_purchase_order
from guibeicai.web._db import get_store_supply_scope_norms, normalize_name, set_store_supply_scope

_db.init_db()

# 选一个有预测结果的门店
stores = _db.get_all_stores(active_only=True)
target = None
for s in stores:
    po = build_purchase_order(s["id"])
    if po.get("items"):
        target = s["id"]
        break

assert target, "演示库无带预测的门店，无法验证"
items = build_purchase_order(target)["items"]
norms_all = [normalize_name(it["ingredient_name"]) for it in items]
# 仅把一半食材设为已覆盖
half = set(norms_all[: max(1, len(norms_all) // 2)])
set_store_supply_scope(target, [(n, n) for n in half])

# 复刻页面 _compute_scope_coverage 逻辑
covered, uncovered = {}, {}
norms = get_store_supply_scope_norms(target)
for it in items:
    nn = normalize_name(it["ingredient_name"])
    key = (nn, it.get("unit"))
    tgt = covered if nn in norms else uncovered
    d = tgt.setdefault(key, {"q": 0.0, "c": 0.0})
    d["q"] += float(it.get("quantity") or 0)
    d["c"] += float(it.get("est_cost") or 0)

cov_n = len(covered); unc_n = len(uncovered)
cov_items = sum(1 for nn in norms_all if nn in half)
print(f"门店 {target}: 总食材 {len(norms_all)} 种，设为已覆盖 {len(half)} 种")
print(f"拆分结果: 已覆盖 {cov_n} 项 / 未覆盖 {unc_n} 项，合计 {cov_n + unc_n} 项")
assert cov_n + unc_n == len(items), "拆分项数应与采购单项数一致"
assert cov_n == cov_items, f"已覆盖项数应={cov_items}，实际={cov_n}"
assert unc_n == len(norms_all) - cov_items, "未覆盖项数应等于未覆盖食材数"
# 金额守恒：已覆盖金额 + 未覆盖金额 ≈ 总采购金额
total = sum(float(it.get("est_cost") or 0) for it in items)
assert abs((sum(d['c'] for d in covered.values()) + sum(d['c'] for d in uncovered.values())) - total) < 0.01
print("金额守恒校验通过 ✅")
print("全部通过 ✅")
shutil.rmtree(tmp, ignore_errors=True)
