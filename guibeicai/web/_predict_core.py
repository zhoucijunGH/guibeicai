"""
桂备菜 - 共享预测核心（Streamlit 无关）
=====================================
从 WEB 端 pages/2_预测配置.py 的 _run_prediction 抽出的纯逻辑版本，
不依赖 Streamlit（无 st.session_state / st.stop / st.error），
供 WEB 预测页与小程序 API（_api/main.py）共用，保证两端预测结果一致。

对外入口：run_prediction(store_id, predict_dates, manual_event="", ...)
  - predict_dates: list，元素可为 date 对象或 "YYYY-MM-DD" 字符串
  - 返回 dict：{predictions, overall_risk, safety_rate, dish_count, by_dish, total_revenue, gross_margin}
  - 副作用：逐日调用 save_prediction 落库（按 (store_id, predict_date) 去重）
"""
from datetime import date, datetime, timedelta
import logging
import re
import pandas as pd
from chinese_calendar import is_holiday

from guibeicai.web import _db as db
from guibeicai.web._factors import get_future_factors
from guibeicai.web._forecast import fetch_weather, prophet_predict_for_dish, city_to_coords
from guibeicai.web._llm import analyze_by_dishes
from guibeicai.web._config import (
    PREDICTION_DEVIATION_THRESHOLD,
    LLM_MULTIPLIER_MIN,
    LLM_MULTIPLIER_MAX,
    RULE_WEATHER_HOT_DEG,
    RULE_WEATHER_HOT_PENALTY,
    RULE_WEATHER_HOT_KEYWORDS,
)

logger = logging.getLogger("predict_core")


# ========== 品类映射（与 WEB 页 _guess_category 一致）==========
def _guess_category(name: str) -> str:
    name = (name or "").strip()
    meat = ["鸡腿肉", "鸡肉", "鸡胸", "鸡翅", "猪肉", "肉末", "牛肉", "羊肉", "鱼片", "草鱼", "黑鱼", "鲈鱼", "虾", "虾仁", "鱿鱼", "鸭血", "肥牛", "五花肉"]
    veg = ["葱花", "豆芽", "辣椒", "青菜", "白菜", "土豆", "茄子", "黄瓜", "番茄", "莲藕", "莴笋", "芹菜", "蒜苗", "姜", "蒜", "香菜", "金针菇", "香菇", "木耳"]
    tofu = ["豆腐", "嫩豆腐", "老豆腐", "豆皮", "千张", "腐竹", "豆干"]
    sauce = ["豆瓣酱", "郫县豆瓣酱", "酱油", "醋", "蚝油", "料酒", "生抽", "老抽", "甜面酱", "辣酱", "花椒油", "香油"]
    nut = ["花生米", "花生", "芝麻", "核桃", "腰果"]
    staple = ["米饭", "面条", "粉", "馒头", "饺子皮", "馄饨皮"]
    egg = ["鸡蛋", "鸭蛋", "鹌鹑蛋"]
    seasoning = ["盐", "糖", "鸡精", "味精", "胡椒粉", "花椒", "干辣椒", "八角", "桂皮", "香叶"]
    for k in meat:
        if k in name:
            return "肉类/水产"
    for k in veg:
        if k in name:
            return "蔬菜"
    for k in tofu:
        if k in name:
            return "豆制品"
    for k in sauce:
        if k in name:
            return "酱料"
    for k in nut:
        if k in name:
            return "干货/坚果"
    for k in staple:
        if k in name:
            return "主食"
    for k in egg:
        if k in name:
            return "蛋品"
    for k in seasoning:
        if k in name:
            return "调味"
    return "其他"


def _normalize_dates(predict_dates):
    out = []
    for d in predict_dates:
        if isinstance(d, (list, tuple)):
            d = d[0]
        if isinstance(d, str):
            d = datetime.strptime(d, "%Y-%m-%d").date() if len(d) == 10 else date.fromisoformat(d)
        out.append(d)
    return out


def run_prediction(
    store_id: int,
    predict_dates,
    manual_event: str = "",
    store_name: str = None,
    store_city: str = None,
    progress_cb=None,
    log_action=None,
    user=None,
):
    """执行完整预测逻辑（便携版），返回 dict 并落库。

    progress_cb(done, total, msg) 可选，用于上报进度。
    log_action(action, **kwargs) 可选，供 WEB 端注入审计；核心本身不依赖审计模块。
    """
    def _prog(d, t, m):
        if progress_cb:
            try:
                progress_cb(d, t, m)
            except Exception:
                pass

    _prog(0, 1, "解析门店与历史销量")
    store = db.get_store_by_id(store_id)
    if not store:
        raise ValueError(f"门店不存在 store_id={store_id}")
    store_name = store_name or store["name"]
    store_city = store_city or store["city"]

    # ---- 日期归一化（必须在 get_future_factors / is_holiday 之前，二者期望 date 对象）----
    predict_dates = _normalize_dates(predict_dates)
    if not predict_dates:
        raise ValueError("未指定任何预测日期")

    # ---- 历史销量（底层函数，无 Streamlit 依赖）----
    # [B2] 仅取近期窗口，降低内存与耗时
    sales_df = db.get_sales_history_df(store_id, days=db.get_default_history_days())
    if sales_df is None or len(sales_df) == 0:
        raise ValueError("该门店暂无历史销量数据，请先上传数据")
    if "sale_date" in sales_df.columns:
        sales_df["sale_date"] = pd.to_datetime(sales_df["sale_date"], errors="coerce")
        sales_df = sales_df.dropna(subset=["sale_date"])
    else:
        raise ValueError("历史销量数据缺少 sale_date 列")
    for col in ["qty", "unit_price"]:
        if col in sales_df.columns:
            sales_df[col] = pd.to_numeric(sales_df[col], errors="coerce")

    _prog(1, 6, "获取天气与特殊事件")
    try:
        weather_text = get_future_factors(store_id, store_city, predict_dates)
    except Exception as e:
        logger.warning("[predict_core] 天气上下文获取失败，降级为空（不影响销量预测）: %s", e)
        weather_text = ""
    holiday_dates = [d.strftime("%m-%d") for d in predict_dates if is_holiday(d)]

    try:
        from guibeicai.web._special_events import get_special_event_context
        first_date_str = predict_dates[0].strftime("%m-%d")
        extra_dates, extra_manual = get_special_event_context(
            predict_date_str=first_date_str, city=store_city, manual_event=manual_event,
        )
        if extra_dates:
            holiday_dates = list(set(holiday_dates + extra_dates))
        if extra_manual:
            manual_event = extra_manual
    except Exception as e:
        logger.warning("[predict_core] special_events 上下文注入失败: %s", e)

    try:
        from guibeicai.web._special_events import build_special_event_series
        se_df = build_special_event_series(
            city=store_city,
            ds_index=list(sales_df["sale_date"]) + list(predict_dates),
            manual_event_dates=predict_dates if manual_event else None,
        )
    except Exception as e:
        logger.warning("[predict_core] 构建 special_event 序列失败，降级为无事件: %s", e)
        se_df = None

    db_dishes = db.get_dishes_by_store(store_id, active_only=True)
    dishes = [d["name"] for d in db_dishes]

    bias_map = db.get_bias_corrections(store_id)

    _prog(2, 6, "LLM 分析菜品因子")
    llm_res = analyze_by_dishes(
        dish_list=dishes, weather_text=weather_text,
        holiday_dates=holiday_dates, manual_event=manual_event, city=store_city,
    )
    by_dish = llm_res.get("by_dish", {})

    llm_failed = all(
        d.get("multiplier", 1.0) == 1.0 and "默认" in d.get("note", "")
        for d in by_dish.values()
    ) if by_dish else True
    if llm_failed:
        logger.warning("[predict_core] LLM 全部兜底，将使用历史基准替代 multiplier")

    db_recipes = db.get_active_recipes_by_store(store_id)

    periods = (pd.Timestamp(max(predict_dates)) - sales_df["sale_date"].max()).days

    dish_groups = {d: g.reset_index(drop=True) for d, g in sales_df.groupby("dish_name")}

    hist_baseline = {}
    for dish in dishes:
        dish_data = dish_groups.get(dish, pd.DataFrame())
        if len(dish_data) >= 7:
            hist_baseline[dish] = dish_data["qty"].tail(7).mean()
        elif len(dish_data) > 0:
            hist_baseline[dish] = dish_data["qty"].mean()
        else:
            hist_baseline[dish] = 0

    _weather_temp = None
    if weather_text:
        _temp_match = re.search(r'均温([\d.]+)℃|气温([\d.]+)℃|([\d.]+)℃', weather_text)
        if _temp_match:
            for g in _temp_match.groups():
                if g is not None:
                    try:
                        _weather_temp = float(g)
                        break
                    except (ValueError, TypeError):
                        pass
    if _weather_temp is not None:
        logger.info("[predict_core] 提取到天气温度=%.1f℃", _weather_temp)

    _prog(3, 6, "逐菜品预测建模")
    dish_fc_map = {}
    dish_mean_qty = {}
    dish_multiplier = {}
    dish_baseline = {}
    dish_price = {}
    dish_llm = {}

    store_lat, store_lon = city_to_coords(store_city)

    for dish in dishes:
        dish_data = dish_groups.get(dish, pd.DataFrame()).copy()
        dish_llm[dish] = by_dish.get(dish, {})
        if len(dish_data) < 14:
            dish_mean_qty[dish] = dish_data["qty"].mean() if len(dish_data) else 0
        else:
            weather_df = None
            try:
                weather_df = fetch_weather(store_lat, store_lon, dish_data["sale_date"].min(), pd.Timestamp(max(predict_dates)))
            except Exception as e:
                logger.warning("[predict_core] 天气获取失败 dish=%s，降级为无天气: %s", dish, e)
            try:
                fc = prophet_predict_for_dish(dish_data, weather_df, periods, special_event_df=se_df).set_index("ds")
            except Exception as e:
                logger.warning("[predict_core] Prophet 训练失败 dish=%s，降级为均值: %s", dish, e)
                fc = pd.DataFrame(columns=["ds", "yhat"])
            dish_fc_map[dish] = {ts: float(row["yhat"]) for ts, row in fc.iterrows()}
            dish_mean_qty[dish] = dish_data["qty"].mean()

        dish_analysis = by_dish.get(dish, {})
        baseline = hist_baseline.get(dish, 0)
        if llm_failed and baseline > 0 and dish_mean_qty[dish] > 0:
            multiplier = baseline / dish_mean_qty[dish]
            logger.warning("[predict_core] LLM兜底 dish=%s prophet_raw=%.2f baseline=%.2f multiplier=%.3f", dish, dish_mean_qty[dish], baseline, multiplier)
        else:
            multiplier = dish_analysis.get("multiplier", 1.0)

        orig_multiplier = multiplier
        multiplier = max(LLM_MULTIPLIER_MIN, min(LLM_MULTIPLIER_MAX, multiplier))
        if abs(multiplier - orig_multiplier) > 0.001:
            logger.info("[predict_core] multiplier软边界 dish=%s %.3f→%.3f", dish, orig_multiplier, multiplier)

        if _weather_temp is not None and _weather_temp >= RULE_WEATHER_HOT_DEG:
            is_hot_dish = any(kw in dish for kw in RULE_WEATHER_HOT_KEYWORDS)
            if is_hot_dish:
                multiplier += RULE_WEATHER_HOT_PENALTY

        dish_multiplier[dish] = multiplier
        dish_baseline[dish] = baseline
        dish_price[dish] = dish_data["unit_price"].iloc[-1] if len(dish_data) else 0

    risk_levels_order = {"低": 0, "偏高": 1, "高": 2}
    overall_risk = "低"
    for dish_analysis in by_dish.values():
        r = dish_analysis.get("risk_level", "低")
        if risk_levels_order.get(r, 0) > risk_levels_order.get(overall_risk, 0):
            overall_risk = r

    safety_rate = 0.15 if overall_risk == "高" else 0.05 if overall_risk == "偏高" else 0.0

    _prog(4, 6, "按日计算销量与采购")
    all_predictions = []
    for pd_date in predict_dates:
        ts_date = pd.Timestamp(pd_date)
        dish_qty_map = {}
        for dish in dishes:
            fc_map = dish_fc_map.get(dish, {})
            if ts_date in fc_map:
                prophet_raw = fc_map[ts_date]
            else:
                prophet_raw = dish_mean_qty.get(dish, 0)
            pred_qty = prophet_raw * dish_multiplier.get(dish, 1.0) * bias_map.get(dish, 1.0)
            baseline = dish_baseline.get(dish, 0)
            if baseline > 0:
                lower = baseline * (1 - PREDICTION_DEVIATION_THRESHOLD)
                upper = baseline * (1 + PREDICTION_DEVIATION_THRESHOLD)
                original_qty = pred_qty
                pred_qty = max(lower, min(upper, pred_qty))
                if abs(original_qty - pred_qty) > 0.01:
                    logger.warning(
                        "[predict_core] 偏差补偿 dish=%s date=%s original=%.2f corrected=%.2f baseline=%.2f",
                        dish, pd_date, original_qty, pred_qty, baseline,
                    )
            dish_qty_map[dish] = max(pred_qty, 0)

        total_revenue = sum(dish_qty_map[d] * dish_price[d] for d in dishes)
        purchase_rows = []
        total_food_cost = 0.0
        for dish in dishes:
            dish_analysis = dish_llm.get(dish, {})
            dish_risk = dish_analysis.get("risk_level", overall_risk)
            dish_conclusion = dish_analysis.get("conclusion", "")
            dish_purchase_advice = dish_analysis.get("purchase_advice", "")
            predicted_qty = dish_qty_map[dish]

            dish_bom = db_recipes.get(dish, [])
            for bom_row in dish_bom:
                usage = bom_row["quantity"]
                unit_price = bom_row["unit_price"]
                base_qty = predicted_qty * usage
                final_qty = base_qty * (1 + safety_rate)
                total_cost = final_qty * unit_price
                total_food_cost += total_cost

                category = bom_row.get("category", "")
                if not category or str(category).strip() in ("", "其他"):
                    category = _guess_category(bom_row["ingredient_name"])

                purchase_rows.append({
                    "category": category,
                    "ingredient_name": bom_row["ingredient_name"],
                    "base_qty": f"{base_qty:.2f} {bom_row.get('unit', '份')}",
                    "suggested_qty": f"{final_qty:.2f} {bom_row.get('unit', '份')}",
                    "unit_cost": unit_price,
                    "total_cost": round(total_cost, 2),
                    "related_dish": dish,
                    "risk_level": dish_risk,
                    "conclusion": dish_conclusion,
                    "purchase_advice": dish_purchase_advice,
                    "note": dish_analysis.get("note", ""),
                })

        gross_margin = (total_revenue - total_food_cost) / total_revenue * 100 if total_revenue > 0 else 0
        result_data = {
            "financial": {"predict_date": str(pd_date), "store": store_name, "revenue": round(total_revenue, 2), "cost": round(total_food_cost, 2), "gross_margin": round(gross_margin, 2)},
            "purchase_details": purchase_rows,
            "dish_predictions": {d: dish_qty_map[d] for d in dishes},
            "risk_level": overall_risk,
            "risk_factors": [],
            "conclusion": "",
            "purchase_advice": "",
            "multiplier": 1.0,
            "source": "manual",
        }
        all_predictions.append(result_data)

        # 落库（按 (store_id, predict_date) 去重）
        if not db.check_prediction_exists(store_id, str(pd_date)):
            rec = {
                "store_id": store_id,
                "predict_date": str(pd_date),
                "run_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "special_event": f"weather:{weather_text} | holiday:{holiday_dates} | manual:{manual_event}",
                "revenue": round(total_revenue, 2),
                "cost": round(total_food_cost, 2),
                "gross_margin": round(gross_margin, 2),
                "result": result_data,
            }
            db.save_prediction(rec)
        else:
            logger.info("[predict_core] 预测已存在，跳过写入 store_id=%s date=%s", store_id, pd_date)

    _prog(5, 6, "写入完成，生成审计")
    last_fin = all_predictions[-1]["financial"] if all_predictions else {"revenue": 0, "gross_margin": 0}

    if log_action is not None and user is not None:
        try:
            log_action(
                "predict_manual",
                target_type="store",
                target_id=store_id,
                detail={
                    "store_name": store_name,
                    "dates": [str(d) for d in predict_dates],
                    "dish_count": len(dishes),
                    "revenue": last_fin["revenue"],
                    "gross_margin": last_fin["gross_margin"],
                },
                user=user,
            )
        except Exception as e:
            logger.warning("[predict_core] 审计写入失败: %s", e)

    _prog(6, 6, "完成")
    return {
        "predictions": all_predictions,
        "overall_risk": overall_risk,
        "safety_rate": safety_rate,
        "dish_count": len(dishes),
        "by_dish": by_dish,
        "total_revenue": last_fin["revenue"],
        "gross_margin": last_fin["gross_margin"],
    }
