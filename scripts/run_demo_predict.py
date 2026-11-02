"""
桂备菜 - 演示用无头预测脚本（不依赖 Streamlit / LLM）
==================================================
对演示门店（默认 3/4/5）逐菜品跑 Prophet，生成未来的预测份数，
并写入 predictions 表（result_json 含 dish_predictions），使食材预测/采购单/智能预警页面可直接演示。
天气回归量取 None（模型用周/年季节性 + 趋势训练），离线可跑。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlite3
import pandas as pd
from datetime import date, timedelta

from guibeicai.web._db import init_db, get_all_stores, get_dishes_by_store, get_sales_history_df, save_prediction, get_bias_corrections
from guibeicai.web._forecast import prophet_predict_for_dish
from guibeicai.web._cache import init_cache

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "guibeicai.db")

init_db()
init_cache()

TARGET = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
STORE_IDS = [3, 4, 5]  # 演示门店


def main():
    total = 0
    for store_id in STORE_IDS:
        store = next((s for s in get_all_stores(active_only=False) if s["id"] == store_id), None)
        if not store:
            print(f"门店 {store_id} 不存在，跳过"); continue
        print(f"\n=== 门店 {store['name']} (id={store_id}) 预测 {TARGET} ===")
        sales_df = get_sales_history_df(store_id)
        if sales_df is None or len(sales_df) == 0:
            print("  无销量数据，跳过"); continue
        sales_df["sale_date"] = pd.to_datetime(sales_df["sale_date"], errors="coerce")
        sales_df = sales_df.dropna(subset=["sale_date"])
        max_date = sales_df["sale_date"].max()
        periods = max((pd.Timestamp(TARGET) - max_date).days, 1)
        dishes = get_dishes_by_store(store_id, active_only=True)
        dish_groups = {d: g.reset_index(drop=True) for d, g in sales_df.groupby("dish_name")}
        bias_map = get_bias_corrections(store_id)
        dish_predictions = {}
        revenue = 0.0
        cost = 0.0
        for dish in dishes:
            g = dish_groups.get(dish["name"], pd.DataFrame()).copy()
            if len(g) < 14:
                yhat = float(g["qty"].mean()) if len(g) else 0.0
            else:
                try:
                    fc = prophet_predict_for_dish(g, weather_df=None, periods=periods, special_event_df=None)
                    if len(fc) and pd.Timestamp(TARGET) in pd.to_datetime(fc["ds"]).values:
                        yhat = float(fc.set_index("ds").loc[pd.Timestamp(TARGET), "yhat"])
                    else:
                        yhat = float(g["qty"].mean())
                except Exception:
                    yhat = float(g["qty"].mean())
            qty = max(yhat, 0) * bias_map.get(dish["name"], 1.0)
            price = float(g["unit_price"].iloc[-1]) if len(g) else 0.0
            dish_predictions[dish["name"]] = round(qty, 1)
            revenue += qty * price
        result = {
            "predict_date": TARGET,
            "store": store["name"],
            "dish_predictions": dish_predictions,
            "financial": {
                "predict_date": TARGET, "store": store["name"],
                "revenue": round(revenue, 2), "cost": round(cost, 2),
                "gross_margin": 0.0,
            },
            "risk_level": "低", "conclusion": "演示预测（无头脚本生成）", "purchase_advice": "",
        }
        save_prediction({
            "store_id": store_id, "predict_date": TARGET,
            "run_time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            "special_event": "", "revenue": round(revenue, 2), "cost": 0.0,
            "gross_margin": 0.0, "result": result,
        })
        total += 1
        print(f"  菜品 {len(dish_predictions)} 个，预估营业额 ¥{revenue:,.0f}，已落库")
    print(f"\n>>> 演示预测完成：{total} 个门店")


if __name__ == "__main__":
    main()
