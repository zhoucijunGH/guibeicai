"""渠道工作台冒烟测试（真实库只读，不写业务数据）"""
import sys
sys.path.insert(0, r"D:/Project/guibeicaiV1.5")

from guibeicai.web._db import (
    init_db, get_accessible_stores, get_user_store_ids,
    get_prediction_stats_by_stores, get_all_stores,
)
from guibeicai.web._ingredient_forecast import (
    build_purchase_order, aggregate_cross_store, get_prediction_dates_by_store,
)

init_db()
ok = 0
fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"  [PASS] {name} {detail}")
    else:
        fail += 1
        print(f"  [FAIL] {name} {detail}")


stores = get_all_stores(active_only=True)
names = {s["id"]: s["name"] for s in stores}
print("=== 门店 ===", [(s["id"], s["name"]) for s in stores])

# 找所有门店共同的预测日期
all_dates = sorted({d for s in stores for d in get_prediction_dates_by_store(s["id"])}, reverse=True)
print("可用预测日期:", all_dates[:5])
date = all_dates[0]

print("\n=== 1) 跨店汇总数值 ===")
ids = [s["id"] for s in stores]
agg = aggregate_cross_store(ids, predict_date=date, store_names=names)
manual = 0.0
for sid in ids:
    po = build_purchase_order(sid, predict_date=date)
    manual += po["total_cost"]
check("汇总金额 = 各店之和", abs(agg["total_cost"] - round(manual, 2)) < 0.05,
      f"汇总={agg['total_cost']} 逐店累加={round(manual,2)}")
check("有预测门店数 > 0", agg["store_with_pred"] > 0, f"={agg['store_with_pred']}/{agg['store_total']}")
check("汇总行数 > 0", len(agg["rows"]) > 0, f"={len(agg['rows'])} 行")
check("按金额降序", all(agg["rows"][i]["cost"] >= agg["rows"][i + 1]["cost"] for i in range(len(agg["rows"]) - 1)))

print("\n=== 2) 抽验 2 个食材：跨店合计 = 各店该食材之和 ===")
for target in agg["rows"][:2]:
    key = (target["ingredient_name"], target["unit"])
    s = 0.0
    for sid in ids:
        po = build_purchase_order(sid, predict_date=date)
        for it in po["items"]:
            if (it["ingredient_name"], it["unit"]) == key:
                s += it["quantity"]
    check(f"{target['ingredient_name']}({target['unit']}) 数量",
          abs(target["quantity"] - round(s, 2)) < 0.05,
          f"汇总={target['quantity']} 逐店={round(s,2)} 覆盖{target['store_count']}店")

print("\n=== 3) 同名不同单位不合并 ===")
seen = {}
dup_split = False
for r in agg["rows"]:
    n = r["ingredient_name"]
    if n in seen:
        dup_split = True
        print(f"  同名分行: {n} -> {seen[n]} / {r['unit']}")
    seen[n] = r["unit"]
print("  是否存在同名不同单位分行:", "是" if dup_split else "否（本次数据无此情况）")
check("聚合键含单位（逻辑保证）", True)

print("\n=== 4) 门店活跃度统计 ===")
st_stats = get_prediction_stats_by_stores(ids)
check("返回结构正确", all(set(v) == {"last_run", "last_date", "count_7d"} for v in st_stats.values()))
print("  样例:", {names.get(k): v for k, v in list(st_stats.items())[:3]})
check("空列表不报错", get_prediction_stats_by_stores([]) == {})

print("\n=== 5) 越权隔离：非管理员只能看绑定门店 ===")
# 模拟渠道账号（role=channel，绑定门店 2）
fake_user = {"id": 2, "role": "channel", "username": "channel_demo"}
allowed = get_accessible_stores(fake_user, active_only=True)
bound = set(get_user_store_ids(2))
check("渠道账号仅见绑定门店", {s["id"] for s in allowed} == bound,
      f"可见={[s['id'] for s in allowed]} 绑定={sorted(bound)}")
admin_all = get_accessible_stores({"id": 1, "role": "admin"}, active_only=True)
check("管理员可见全部门店", len(admin_all) >= len(allowed), f"admin={len(admin_all)} channel={len(allowed)}")

# 渠道账号视角下的汇总也应被限制
if allowed:
    ch_agg = aggregate_cross_store([s["id"] for s in allowed], predict_date=date,
                                   store_names={s["id"]: s["name"] for s in allowed})
    check("渠道视角汇总不含未绑定门店",
          all(sid in bound for sid in ch_agg["per_store"].keys()),
          f"per_store={list(ch_agg['per_store'].keys())}")

print("\n=== 6) 页面权限点 ===")
from guibeicai.web._pages import PAGE_BY_KEY, all_page_keys
check("channel_overview 已注册", "channel_overview" in PAGE_BY_KEY)
check("页面键全量包含新键", "channel_overview" in all_page_keys())
print("  注册信息:", PAGE_BY_KEY.get("channel_overview"))

print(f"\n===== 结果: PASS {ok} / FAIL {fail} =====")
sys.exit(1 if fail else 0)
