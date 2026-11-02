"""
桂备菜 - 食材级预测与采购聚合
================================
将菜品销量预测结果（dish_predictions: 菜名→预测份数）按 recipes 聚合到食材维度，
输出「食材 → 预估用量 / 单位 / 预估成本」，供食材级预测页与采购单页共用。
"""
import logging

from guibeicai.web._db import load_all_history, get_active_recipes_by_store, get_prediction_by_store_date

logger = logging.getLogger(__name__)


def get_latest_prediction(store_id: int) -> dict | None:
    """取该门店最近一次预测记录（含已解析 result）。

    [F4] 直接在 DB 层按门店过滤，避免每次都全表加载后在 Python 过滤。
    """
    hist = load_all_history(store_ids=[store_id])
    if not hist:
        return None
    hist.sort(key=lambda h: (str(h.get("predict_date") or ""), str(h.get("run_time") or "")), reverse=True)
    return hist[0]


def get_dish_predictions(store_id: int) -> tuple:
    """
    返回 (predict_date:str, dish_predictions:dict)。
    无预测记录时返回 (None, {})。
    """
    rec = get_latest_prediction(store_id)
    if not rec:
        return None, {}
    return _extract_dish_predictions(rec)


def get_prediction_dates_by_store(store_id: int) -> list:
    """返回某门店所有可用的预测日期（去重、降序）。[F4] 直接按门店过滤。"""
    dates = sorted({str(h.get("predict_date") or "") for h in load_all_history(store_ids=[store_id]) if h.get("predict_date")}, reverse=True)
    return dates


def get_dish_predictions_by_date(store_id: int, predict_date: str) -> tuple:
    """
    按指定日期返回 (predict_date:str, dish_predictions:dict)。
    无预测记录时返回 (None, {})。
    """
    pred = get_prediction_by_store_date(store_id, predict_date)
    if not pred:
        return None, {}
    return _extract_dish_predictions({"predict_date": predict_date, "result": pred.get("result", {})})


def _extract_dish_predictions(rec: dict) -> tuple:
    """从预测记录中提取 dish_predictions 并归一化为 {菜名: 份数}。"""
    result = rec.get("result") or {}
    dp = result.get("dish_predictions") or {}
    # 兜底：部分记录可能把预测放在 result["result"]["dish_predictions"]
    if isinstance(dp, dict):
        dp = {str(k): float(v) for k, v in dp.items() if v is not None}
    else:
        dp = {}
    return str(rec.get("predict_date") or ""), dp


def aggregate_ingredients(store_id: int, dish_predictions: dict) -> list:
    """
    将菜品预测份数按配方聚合为食材需求。

    返回 list[dict]，按成本降序，每个元素含：
      ingredient_name, category, unit, quantity, cost, breakdown
    - 聚合键为 (食材名, 单位)：避免「kg」与「g」等不同单位被错误相加（单位换算暂未做）。
    - breakdown: [{dish_name, quantity, cost}]，按用量降序，用于说明
      「该食材被哪些菜品分摊，各用多少」（如 鸭肉 10kg = 啤酒鸭 5 + 烤鸭 3 + 柠檬鸭 2）。
    """
    recipes = get_active_recipes_by_store(store_id)  # {菜名: [recipe...]}
    agg = {}
    for dish_name, qty in dish_predictions.items():
        try:
            qty = float(qty)
        except Exception:
            continue
        for rec in recipes.get(dish_name, []):
            ing = rec.get("ingredient_name")
            if not ing:
                continue
            per = float(rec.get("quantity") or 0)
            price = float(rec.get("unit_price") or 0)
            unit = rec.get("unit") or ""
            add_qty = per * qty
            add_cost = add_qty * price
            key = (ing, unit)
            if key not in agg:
                agg[key] = {
                    "ingredient_name": ing,
                    "category": rec.get("category", ""),
                    "unit": unit,
                    "quantity": 0.0,
                    "cost": 0.0,
                    "breakdown": [],
                }
            agg[key]["quantity"] += add_qty
            agg[key]["cost"] += add_cost
            agg[key]["breakdown"].append({"dish_name": dish_name, "quantity": add_qty, "cost": add_cost})

    rows = list(agg.values())
    for r in rows:
        r["quantity"] = round(r["quantity"], 2)
        r["cost"] = round(r["cost"], 2)
        r["breakdown"].sort(key=lambda x: x["quantity"], reverse=True)
        for b in r["breakdown"]:
            b["quantity"] = round(b["quantity"], 2)
            b["cost"] = round(b["cost"], 2)
    rows.sort(key=lambda x: x["cost"], reverse=True)
    return rows


def aggregate_cross_store(store_ids: list, predict_date: str = None, store_names: dict = None) -> dict:
    """跨门店食材需求汇总（渠道/配送商视角核心）。

    逐个门店调用 build_purchase_order，再按 (食材名, 单位) 合并，得到
    「旗下所有门店合计需要多少量、覆盖几家店」。

    参数：
    - store_ids: 门店 ID 列表（调用方必须先按用户权限过滤，本函数不做权限判断）
    - predict_date: 目标预测日期；None 表示各店取各自最近一次预测
    - store_names: {store_id: 门店名}，用于输出来源门店；缺省则只记 ID

    返回：
    {
      "predict_date": 汇总口径日期（None 表示各店取最近一次，日期不统一）,
      "rows": [{ingredient_name, category, unit, quantity, cost, store_count, store_names}],
      "total_cost": 合计金额,
      "store_total": 参与门店数,
      "store_with_pred": 有预测结果的门店数,
      "per_store": {store_id: {name, predict_date, total_cost, item_count}},
    }

    注意：聚合键含单位，同名不同单位（如「斤」与「kg」）分行显示不做换算，
    避免换算错误导致采购量偏差（与单店 aggregate_ingredients 口径一致）。
    """
    store_names = store_names or {}
    agg = {}
    per_store = {}
    store_with_pred = 0
    total_cost = 0.0

    for sid in [int(x) for x in (store_ids or []) if int(x)]:
        po = build_purchase_order(sid, predict_date=predict_date)
        items = po.get("items") or []
        if not items:
            continue
        store_with_pred += 1
        cost_sum = sum(float(it.get("est_cost") or 0) for it in items)
        total_cost += cost_sum
        per_store[sid] = {
            "name": store_names.get(sid, f"门店#{sid}"),
            "predict_date": po.get("predict_date"),
            "total_cost": round(cost_sum, 2),
            "item_count": len(items),
        }
        for it in items:
            key = (it.get("ingredient_name"), it.get("unit"))
            qty = float(it.get("quantity") or 0)
            cost = float(it.get("est_cost") or 0)
            if key not in agg:
                agg[key] = {
                    "ingredient_name": it.get("ingredient_name"),
                    "category": it.get("category", ""),
                    "unit": it.get("unit", ""),
                    "quantity": 0.0,
                    "cost": 0.0,
                    "store_count": 0,
                    "store_names": [],
                }
            agg[key]["quantity"] += qty
            agg[key]["cost"] += cost
            agg[key]["store_count"] += 1
            agg[key]["store_names"].append(store_names.get(sid, f"门店#{sid}"))

    rows = []
    for r in agg.values():
        r["quantity"] = round(r["quantity"], 2)
        r["cost"] = round(r["cost"], 2)
        rows.append(r)
    rows.sort(key=lambda x: x["cost"], reverse=True)

    return {
        "predict_date": predict_date,
        "rows": rows,
        "total_cost": round(total_cost, 2),
        "store_total": len([int(x) for x in (store_ids or []) if int(x)]),
        "store_with_pred": store_with_pred,
        "per_store": per_store,
    }


def build_purchase_order(store_id: int, predict_date: str = None) -> dict:
    """
    生成采购单结构。
    返回 {predict_date, items:[...], total_cost, total_qty, dish_count}。
    items: ingredient_name, category, quantity, unit, est_cost, breakdown
    predict_date 为 None 时使用最近一次预测。
    """
    if predict_date:
        predict_date, dp = get_dish_predictions_by_date(store_id, predict_date)
    else:
        predict_date, dp = get_dish_predictions(store_id)
    rows = aggregate_ingredients(store_id, dp)
    items = []
    total_cost = 0.0
    for r in rows:
        total_cost += r["cost"]
        items.append({
            "ingredient_name": r["ingredient_name"],
            "category": r["category"],
            "quantity": r["quantity"],
            "unit": r["unit"],
            "est_cost": r["cost"],
            "breakdown": r["breakdown"],
        })
    po = {
        "predict_date": predict_date,
        "items": items,
        "total_cost": round(total_cost, 2),
        "total_qty": round(sum(r["quantity"] for r in rows), 2),
        "dish_count": len(dp),
    }
    return po
