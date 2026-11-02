"""回测体系：预测准确度评估 (MAE / MAPE / RMSE) + 误差反馈闭环 (偏差校正)。

设计：
- 回测 (#17)：对每家门店每个菜品做时间序列 hold-out 交叉验证——
  用较早的销量训练 Prophet（复用已有天气/特殊事件回归量），预测最近 holdout_days 天，
  与历史销量 (sales_history) 真值对比，聚合 MAE / MAPE / RMSE 与逐菜品偏差。
- 误差反馈闭环 (#14)：由回测逐菜品系统性偏差生成校正因子
  factor = mean(actual)/mean(pred)（限幅），持久化到 bias_corrections 表；
  预测流程读取并乘到预测销量上，使下一轮预测自动纠偏。
"""
import logging
import os

import pandas as pd

from guibeicai.web._db import (
    get_all_stores,
    get_sales_history_df,
    save_bias_correction,
    save_backtest_run,
)
from guibeicai.web._forecast import prophet_predict_for_dish, fetch_weather, city_to_coords
from guibeicai.web._special_events import build_special_event_series

logger = logging.getLogger(__name__)

MIN_HISTORY_DAYS = int(os.getenv("BACKTEST_MIN_HISTORY_DAYS", "30"))
HOLDOUT_DAYS = int(os.getenv("BACKTEST_HOLDOUT_DAYS", "7"))
BIAS_FLOOR = float(os.getenv("BACKTEST_BIAS_FLOOR", "0.5"))
BIAS_CEIL = float(os.getenv("BACKTEST_BIAS_CEIL", "1.5"))


def _metrics(pairs):
    """pairs: list of (pred, actual)。返回 mae/mape/rmse/bias/pred_mean/actual_mean/n。"""
    if not pairs:
        return None
    n = len(pairs)
    errs = [abs(p - a) for p, a in pairs]
    mae = sum(errs) / n
    mape_items = [abs(p - a) / a for p, a in pairs if a and a > 0]
    mape = (sum(mape_items) / len(mape_items) * 100) if mape_items else 0.0
    rmse = (sum((p - a) ** 2 for p, a in pairs) / n) ** 0.5
    pred_sum = sum(p for p, a in pairs)
    act_sum = sum(a for p, a in pairs)
    bias = (pred_sum / act_sum - 1.0) if act_sum > 0 else 0.0
    return {
        "mae": round(mae, 3),
        "mape": round(mape, 2),
        "rmse": round(rmse, 3),
        "bias": round(bias, 4),
        "pred_mean": round(pred_sum / n, 3),
        "actual_mean": round(act_sum / n, 3),
        "n": n,
    }


def run_backtest(store_id=None, holdout_days=HOLDOUT_DAYS, min_history_days=MIN_HISTORY_DAYS, use_weather=True):
    """对每家门店每个菜品做 hold-out 回测，返回整体与逐菜品指标。"""
    stores = [s for s in get_all_stores(active_only=False) if store_id in (None, s.get("id"))]
    per_dish = []
    pooled = []

    for st in stores:
        sid = st.get("id")
        city = st.get("city") or ""
        sales = get_sales_history_df(sid)
        if sales is None or len(sales) == 0:
            continue
        lat = lon = None
        if use_weather and city:
            try:
                lat, lon = city_to_coords(city)
            except Exception:
                lat = lon = None

        for dish_name, g in sales.groupby("dish_name"):
            g = g.sort_values("sale_date").reset_index(drop=True)
            if len(g) < min_history_days + holdout_days:
                continue
            cut = len(g) - holdout_days
            train = g.iloc[:cut]
            test = g.iloc[cut:]

            train_df = train.rename(columns={"sale_date": "sale_date", "qty": "qty"})[["sale_date", "qty"]].copy()

            wdf = None
            if use_weather and lat is not None:
                try:
                    wdf = fetch_weather(lat, lon, pd.Timestamp(train["sale_date"].min()),
                                        pd.Timestamp(test["sale_date"].max()))
                except Exception as e:
                    logger.warning("[回测] 天气获取失败 store=%s dish=%s: %s", sid, dish_name, e)
                    wdf = None
            se_df = None
            try:
                se_df = build_special_event_series(
                    city, list(train["sale_date"]) + list(test["sale_date"]))
            except Exception:
                se_df = None

            try:
                fc = prophet_predict_for_dish(train_df, wdf, holdout_days, special_event_df=se_df)
            except Exception as e:
                logger.warning("[回测] Prophet 失败 store=%s dish=%s: %s", sid, dish_name, e)
                continue
            if fc is None or len(fc) == 0:
                continue
            fc = fc.set_index("ds")
            test_map = {pd.Timestamp(r["sale_date"]): r["qty"] for _, r in test.iterrows()}
            pairs = [(float(row["yhat"]), float(test_map[ds])) for ds, row in fc.iterrows() if ds in test_map]
            if not pairs:
                continue
            m = _metrics(pairs)
            m.update({"store_id": sid, "dish_name": dish_name})
            per_dish.append(m)
            pooled.extend(pairs)

    overall = _metrics(pooled) or {"mae": None, "mape": None, "rmse": None, "bias": None, "n": 0}
    overall["n_dishes"] = len(per_dish)

    result = {
        "overall": overall,
        "per_dish": per_dish,
        "store_id": store_id,
        "holdout_days": holdout_days,
        "min_history_days": min_history_days,
    }
    try:
        save_backtest_run(store_id, holdout_days, min_history_days,
                          {"overall": overall, "n_dishes": len(per_dish)})
    except Exception as e:
        logger.warning("[回测] 运行记录写入失败: %s", e)
    return result


def compute_bias_corrections(backtest_result):
    """由回测逐菜品偏差生成校正因子 factor = mean(actual)/mean(pred)，限幅 [FLOOR, CEIL]。"""
    corr = {}
    for d in backtest_result.get("per_dish", []):
        pm = d.get("pred_mean") or 0
        am = d.get("actual_mean") or 0
        if pm > 0 and am > 0:
            f = max(BIAS_FLOOR, min(BIAS_CEIL, am / pm))
            corr[d["dish_name"]] = round(f, 3)
    return corr


def apply_and_save_bias_corrections(store_id, backtest_result):
    """回测 → 持久化偏差校正（误差反馈闭环的写入端）。返回 {dish_name: factor}。"""
    corr = compute_bias_corrections(backtest_result)
    for dish, f in corr.items():
        try:
            save_bias_correction(store_id, dish, f)
        except Exception as e:
            logger.warning("[回测] 偏差校正写入失败 store=%s dish=%s: %s", store_id, dish, e)
    return corr
