"""
桂备菜 - 智能预警规则引擎
========================
基于「最近一次预测 vs 历史均值」「未来节假日」「实际 vs 预测偏差」生成预警：
  - 加备：预测 > 1.3× 历史均值
  - 减备/滞销：预测 < 0.7× 历史均值
  - 节假日备货：未来 3 天含法定节假日
  - 偏差复核：实际销量与预测偏差 > 30%
所有规则均为阈值触发，纯计算、无外部依赖。
"""
from datetime import date, timedelta

from guibeicai.web._db import (
    get_dishes_by_store, get_sales_avg_by_dish,
    get_upcoming_holidays, get_prediction_by_store_date, get_actual_sales_by_store_date,
)
from guibeicai.web._ingredient_forecast import get_dish_predictions, get_latest_prediction


def generate_alerts(store_id: int) -> list:
    alerts = []
    dishes = {d["id"]: d for d in get_dishes_by_store(store_id, active_only=True)}
    avg_map = get_sales_avg_by_dish(store_id)
    predict_date, dp = get_dish_predictions(store_id)

    # ---- 1) 加备 / 减备（基于菜品预测 vs 历史均值）----
    if dp:
        for dish_name, qty in dp.items():
            dish = next((d for d in dishes.values() if d["name"] == dish_name), None)
            if dish is None:
                continue
            did = dish["id"]
            avg = avg_map.get(did, 0)
            try:
                qty = float(qty)
            except Exception:
                continue
            if avg <= 0:
                continue
            ratio = qty / avg
            if ratio >= 1.3:
                alerts.append({
                    "severity": "高", "type": "加备",
                    "title": f"【{dish_name}】建议加备货",
                    "detail": f"预测 {qty:.0f} 份，约为历史均值 {avg:.0f} 的 {ratio:.0%}，需求明显走高。",
                })
            elif ratio <= 0.7:
                alerts.append({
                    "severity": "中", "type": "减备",
                    "title": f"【{dish_name}】可能滞销，建议减备",
                    "detail": f"预测 {qty:.0f} 份，仅为历史均值 {avg:.0f} 的 {ratio:.0%}，存在积压风险。",
                })

    # ---- 2) 节假日备货（未来 3 天）----
    today = date.today()
    f0 = today.strftime("%Y-%m-%d")
    f3 = (today + timedelta(days=3)).strftime("%Y-%m-%d")
    hols = get_upcoming_holidays(store_id, f0, f3)
    if hols:
        alerts.append({
            "severity": "高", "type": "节假日",
            "title": "节假日临近，提前备货",
            "detail": f"未来 3 天含法定节假日：{', '.join(hols)}，预计客流上升，请提前增加核心食材备货。",
        })

    # ---- 3) 实际 vs 预测偏差（最近一次预测日）----
    if predict_date:
        pred = get_prediction_by_store_date(store_id, predict_date)
        actual = get_actual_sales_by_store_date(store_id, predict_date)
        if pred and actual:
            result = pred.get("result", {})
            dp_actual = result.get("dish_predictions", {})
            name_to_id = {d["name"]: d["id"] for d in dishes.values()}
            for dish_name, pred_qty in dp_actual.items():
                did = name_to_id.get(dish_name)
                if did is None or did not in actual:
                    continue
                a = float(actual[did] or 0)
                p = float(pred_qty or 0)
                if p > 0 and abs(a - p) / p > 0.3:
                    sev = "高" if abs(a - p) / p > 0.5 else "中"
                    direction = "高于" if a > p else "低于"
                    alerts.append({
                        "severity": sev, "type": "偏差",
                        "title": f"【{dish_name}】实际{direction}预测 {abs(a - p) / p:.0%}",
                        "detail": f"{predict_date} 实际 {a:.0f} 份 vs 预测 {p:.0f} 份，请复核模型或数据。",
                    })

    # 排序：高 > 中
    sev_order = {"高": 0, "中": 1, "低": 2}
    alerts.sort(key=lambda x: sev_order.get(x["severity"], 3))
    return alerts
