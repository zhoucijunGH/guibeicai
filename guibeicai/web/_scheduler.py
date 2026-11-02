"""
桂备菜 Web 后台定时预测调度器
- 自动预测时间/目标日按门店配置：每个活跃门店可独立设置「预测目标日」（今天/明天/后天）
  和「每日预测时间」（默认 06:00）。调度器每 N 分钟巡检一次，到点且今天未预测过的门店
  自动补缺口；已预测门店经 check_prediction_exists 去重跳过。
- POS/收银数据源：每 N 分钟（默认 5）巡检分发器，按每个源的 schedule 独立时间拉取
  （今天未拉过才拉），拉取成功的门店立即按该店配置的 target_offset 预测并推送采购单。
- 结果存数据库，失败不阻断其他门店
"""

import atexit
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import re
from chinese_calendar import is_holiday

from apscheduler.schedulers.background import BackgroundScheduler

from guibeicai.config.settings import settings
from guibeicai.web._db import (
    init_db,
    get_all_stores,
    get_dishes_by_store,
    get_active_recipes_by_store,
    save_prediction, check_prediction_exists,
    get_sales_history_df, get_bias_corrections, get_default_history_days,
    get_store_by_id, get_auto_predict_stores, get_store_push_targets,
    get_alert_push_stores, mark_alert_pushed, alert_pushed_today,
    parse_pos_schedule, parse_auto_predict_time, update_store_predict_status,
    get_prediction_pushed, mark_prediction_pushed, get_all_pos_sources,
)
from guibeicai.data_import import pos_client

from guibeicai.web._cache import init_cache
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
from guibeicai.web._audit import log_action, ACTION_PREDICT_AUTO
from guibeicai.scheduler.notifiers.purchase_notifier import PurchaseNotifier
from guibeicai.web._alerts import generate_alerts
from guibeicai.scheduler.notifiers.store_alert_notifier import StoreAlertNotifier

logger = logging.getLogger("scheduler")


def _guess_category(name: str) -> str:
    """品类映射（与 2_预测配置.py 保持一致）"""
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
        if k in name: return "肉类/水产"
    for k in veg:
        if k in name: return "蔬菜"
    for k in tofu:
        if k in name: return "豆制品"
    for k in sauce:
        if k in name: return "酱料"
    for k in nut:
        if k in name: return "干货/坚果"
    for k in staple:
        if k in name: return "主食"
    for k in egg:
        if k in name: return "蛋品"
    for k in seasoning:
        if k in name: return "调味"
    return "其他"


def _predict_for_store(store, target_date: date) -> None:
    """
    对单个门店执行预测，结果存数据库。
    target_date: 预测目标日期（通常是明天）
    """
    try:
        store_id = store["id"]
        store_name = store["name"]
        store_city = store["city"]

        logger.info("[定时预测] 开始 store=%s city=%s target=%s", store_name, store_city, target_date)

        # 获取历史销量（[B2] 仅取近期窗口，降低内存与耗时）
        sales_df = get_sales_history_df(store_id, days=get_default_history_days())
        if sales_df is None or len(sales_df) == 0:
            logger.warning("[定时预测] 门店 %s 无历史数据，跳过", store_name)
            return

        sales_df["sale_date"] = pd.to_datetime(sales_df["sale_date"], errors="coerce")
        sales_df = sales_df.dropna(subset=["sale_date"])
        for col in ["qty", "unit_price"]:
            if col in sales_df.columns:
                sales_df[col] = pd.to_numeric(sales_df[col], errors="coerce")

        predict_dates = [target_date]
        weather_text = get_future_factors(store_id, store_city, predict_dates)
        holiday_dates = [target_date.strftime("%m-%d")] if is_holiday(target_date) else []

        # 特殊事件注入
        try:
            from guibeicai.web._special_events import get_special_event_context
            extra_dates, extra_manual = get_special_event_context(
                predict_date_str=target_date.strftime("%m-%d"), city=store_city, manual_event="",
            )
            if extra_dates:
                holiday_dates = list(set(holiday_dates + extra_dates))
        except Exception:
            pass

        # 获取菜品
        db_dishes = get_dishes_by_store(store_id, active_only=True)
        dishes = [d["name"] for d in db_dishes]

        # 误差反馈闭环：读取回测产出的偏差校正因子（无则全为 1.0）
        bias_map = get_bias_corrections(store_id)

        llm_res = analyze_by_dishes(
            dish_list=dishes, weather_text=weather_text,
            holiday_dates=holiday_dates, manual_event="", city=store_city,
        )
        by_dish = llm_res.get("by_dish", {})

        # 按菜品分组一次，预测循环 O(1) 取数（#13）
        dish_groups = {d: g.reset_index(drop=True) for d, g in sales_df.groupby("dish_name")}

        # 历史基准
        hist_baseline = {}
        for dish in dishes:
            dish_data = dish_groups.get(dish, pd.DataFrame())["qty"]
            if len(dish_data) >= 7:
                hist_baseline[dish] = dish_data.tail(7).mean()
            elif len(dish_data) > 0:
                hist_baseline[dish] = dish_data.mean()
            else:
                hist_baseline[dish] = 0

        # 温度
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

        # Prophet 预测
        periods = (pd.Timestamp(target_date) - sales_df["sale_date"].max()).days
        lat, lon = city_to_coords(store_city)

        # 特殊事件外生变量序列（历史+目标日，建模进 Prophet；自动调度无手动事件）
        try:
            from guibeicai.web._special_events import build_special_event_series
            se_df = build_special_event_series(
                city=store_city,
                ds_index=list(sales_df["sale_date"]) + [target_date],
                manual_event_dates=None,
            )
        except Exception:
            se_df = None

        pred_rows = []
        for dish in dishes:
            dish_data = dish_groups.get(dish, pd.DataFrame()).copy()
            if len(dish_data) < 14:
                prophet_raw = dish_data["qty"].mean() if len(dish_data) else 0
            else:
                weather_df = fetch_weather(lat, lon, dish_data["sale_date"].min(), pd.Timestamp(target_date))
                fc = prophet_predict_for_dish(dish_data, weather_df, max(periods, 1), special_event_df=se_df).set_index("ds")
                prophet_raw = fc.loc[pd.Timestamp(target_date), "yhat"] if pd.Timestamp(target_date) in fc.index else dish_data["qty"].mean()

            multiplier = by_dish.get(dish, {}).get("multiplier", 1.0)
            multiplier = max(LLM_MULTIPLIER_MIN, min(LLM_MULTIPLIER_MAX, multiplier))

            if _weather_temp is not None and _weather_temp >= RULE_WEATHER_HOT_DEG:
                if any(kw in dish for kw in RULE_WEATHER_HOT_KEYWORDS):
                    multiplier += RULE_WEATHER_HOT_PENALTY

            pred_qty = prophet_raw * multiplier * bias_map.get(dish, 1.0)
            baseline = hist_baseline.get(dish, 0)
            if baseline > 0:
                lower = baseline * (1 - PREDICTION_DEVIATION_THRESHOLD)
                upper = baseline * (1 + PREDICTION_DEVIATION_THRESHOLD)
                pred_qty = max(lower, min(upper, pred_qty))

            price = dish_data["unit_price"].iloc[-1] if len(dish_data) else 0
            pred_rows.append({"dish_name": dish, "predicted_qty": max(pred_qty, 0), "unit_price": price})

        pred_df = pd.DataFrame(pred_rows)
        pred_df["estimated_revenue"] = pred_df["predicted_qty"] * pred_df["unit_price"]

        # 风险
        risk_levels_order = {"低": 0, "偏高": 1, "高": 2}
        overall_risk = "低"
        for dish_analysis in by_dish.values():
            r = dish_analysis.get("risk_level", "低")
            if risk_levels_order.get(r, 0) > risk_levels_order.get(overall_risk, 0):
                overall_risk = r

        safety_rate = 0.15 if overall_risk == "高" else 0.05 if overall_risk == "偏高" else 0.0

        # BOM + 采购
        db_recipes = get_active_recipes_by_store(store_id)
        total_revenue = pred_df["estimated_revenue"].sum()
        purchase_rows = []
        total_food_cost = 0.0

        for _, row in pred_df.iterrows():
            dish = row["dish_name"]
            dish_analysis = by_dish.get(dish, {})
            dish_risk = dish_analysis.get("risk_level", overall_risk)
            dish_bom = db_recipes.get(dish, [])
            for bom_row in dish_bom:
                usage = bom_row["quantity"]
                unit_price = bom_row["unit_price"]
                base_qty = row["predicted_qty"] * usage
                final_qty = base_qty * (1 + safety_rate)
                total_cost = final_qty * unit_price
                total_food_cost += total_cost

                category = bom_row.get("category", "")
                if not category or str(category).strip() == "" or str(category).strip() == "其他":
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
                    "conclusion": dish_analysis.get("conclusion", ""),
                    "purchase_advice": dish_analysis.get("purchase_advice", ""),
                    "note": dish_analysis.get("note", ""),
                })

        gross_margin = (total_revenue - total_food_cost) / total_revenue * 100 if total_revenue > 0 else 0

        result_data = {
            "financial": {
                "predict_date": str(target_date),
                "store": store_name,
                "store_id": store_id,
                "revenue": round(total_revenue, 2),
                "cost": round(total_food_cost, 2),
                "gross_margin": round(gross_margin, 2),
            },
            "purchase_details": purchase_rows,
            "dish_predictions": {row["dish_name"]: max(row["predicted_qty"], 0) for _, row in pred_df.iterrows()},
            "risk_level": overall_risk,
            "risk_factors": [],
            "conclusion": "",
            "purchase_advice": "",
            "multiplier": 1.0,
            "source": "auto",
        }

        # 存库（按 store_id + predict_date 去重，避免重复记录）
        if not check_prediction_exists(store_id, str(target_date)):
            save_prediction({
                "run_time": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
                "store": store_name,
                "store_id": store_id,
                "predict_date": str(target_date),
                "special_event": f"weather:{weather_text} | holiday:{holiday_dates}",
                "revenue": round(total_revenue, 2),
                "cost": round(total_food_cost, 2),
                "gross_margin": round(gross_margin, 2),
                "result": result_data,
            })
            # 操作审计：自动（定时）预测完成
            log_action(
                ACTION_PREDICT_AUTO,
                target_type="store",
                target_id=store_id,
                detail={
                    "store_name": store_name,
                    "predict_date": str(target_date),
                    "revenue": round(total_revenue, 2),
                    "gross_margin": round(gross_margin, 2),
                },
                user={"id": 0, "username": "系统(定时任务)", "role": "system"},
            )
        else:
            logger.info("[定时预测] 预测已存在，跳过写入 store=%s date=%s", store_name, target_date)

        logger.info("[定时预测] 完成 store=%s revenue=%.0f margin=%.1f%%", store_name, total_revenue, gross_margin)

    except Exception:
        logger.exception("[定时预测] 异常 store=%s", store.get("name", "?"))


def _now_iso() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_auto_prediction_for_stores(store_ids, target_date: date = None):
    """对给定门店集合执行自动预测（复用 _predict_for_store），写状态并推送采购单。

    - target_date 为 None 时，按每个门店自己的 auto_predict_target_offset 计算目标日；
    - 自动跳过 auto_predict=0 的门店（仅手工预测）；
    - 已存在预测记录的门店经 check_prediction_exists 判定为成功（不重复写）；
    - 预测后尝试推送采购单（幂等，按 pushed 去重）。
    """
    for sid in store_ids:
        store = get_store_by_id(sid)
        if not store:
            continue
        if store.get("auto_predict", 1) == 0:
            logger.info("[自动预测] 门店 %s 已关闭自动预测，跳过", store.get("name"))
            continue
        try:
            td = target_date or (date.today() + timedelta(days=int(store.get("auto_predict_target_offset", 1))))
            _predict_for_store(store, td)
            status = "ok" if check_prediction_exists(sid, str(td)) else "failed"
        except Exception:
            logger.exception("[自动预测] 门店预测异常 store=%s", store.get("name", "?"))
            status = "failed"
            td = target_date
        update_store_predict_status(sid, _now_iso(), status)
        try:
            if td:
                push_prediction_if_needed(sid, td)
        except Exception:
            logger.exception("[自动预测] 推送异常 store=%s", store.get("name", "?"))


def push_prediction_if_needed(store_id: int, target_date) -> bool:
    """若已生成预测且尚未推送、且配置了推送渠道，则推送采购单并标记 pushed。

    无渠道或已推送则直接返回 False（幂等，避免重复推送）。
    """
    if not check_prediction_exists(store_id, str(target_date)):
        return False
    if get_prediction_pushed(store_id, str(target_date)):
        return False
    targets = get_store_push_targets(store_id)
    if not (targets["emails"] or targets["openids"] or targets["wecom_webhook"]):
        mark_prediction_pushed(store_id, str(target_date))  # 无渠道也标记，避免反复尝试
        return False
    try:
        from guibeicai.web._ingredient_forecast import build_purchase_order
        from guibeicai.config.settings import Settings
        po = build_purchase_order(store_id, predict_date=str(target_date))
        if not po.get("items"):
            mark_prediction_pushed(store_id, str(target_date))
            return False
        email_cfg = dict(Settings().email_config)
        notifier = PurchaseNotifier(config={"email": email_cfg, "wx_subscribe": {}})
        store = get_store_by_id(store_id) or {}
        notifier.send_purchase_order(
            order=po, store_name=store.get("name", ""), date=str(target_date),
            emails=targets["emails"], openids=targets["openids"],
            wecom_webhook=targets["wecom_webhook"],
        )
        mark_prediction_pushed(store_id, str(target_date))
        logger.info("[自动预测] 采购单已推送 store=%s date=%s", store.get("name", ""), target_date)
        return True
    except Exception:
        logger.exception("[自动预测] 采购单推送失败 store=%s", store_id)
        return False


def run_daily_prediction():
    """每 N 分钟巡检一次：对每个 auto_predict=1 的活跃门店，按该店配置的
    auto_predict_time 到点且今天未跑过则预测其目标日（由 auto_predict_target_offset
    决定）。已预测门店经 check_prediction_exists 去重，仅补缺口；覆盖 POS 失败 /
    新增门店等场景。预测后自动推送采购单（渠道未配则跳过）。
    """
    try:
        init_db()
        init_cache()
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        stores = get_auto_predict_stores()
        if not stores:
            logger.warning("[定时预测] 无参与自动预测的门店，跳过")
            return

        due_ids = []
        for store in stores:
            h, mi = parse_auto_predict_time(store.get("auto_predict_time"))
            target_dt = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            last = store.get("last_auto_predict_at") or ""
            last_date = last[:10] if len(last) >= 10 else ""
            if now >= target_dt and last_date != today_str:
                due_ids.append(store["id"])

        if not due_ids:
            logger.info("[定时预测] 暂无到点门店，跳过")
            return

        logger.info("[定时预测] 开始每日缺口补预测 到点门店数=%d", len(due_ids))
        run_auto_prediction_for_stores(due_ids)
        logger.info("[定时预测] 完成缺口补预测")
    except Exception:
        logger.exception("[定时预测] 每日任务异常")


def push_store_alerts_for_stores(store_ids):
    """对给定门店生成预警并推送（邮件 + 企微）。每日每店仅推送一次（去重表）。"""
    if not store_ids:
        return
    notifier = StoreAlertNotifier()
    today = date.today().strftime("%Y-%m-%d")
    for sid in store_ids:
        try:
            if alert_pushed_today(sid, today):
                continue
            store = get_store_by_id(sid)
            if not store:
                mark_alert_pushed(sid, today)
                continue
            targets = get_store_push_targets(sid)
            if not (targets.get("emails") or targets.get("wecom_webhook")):
                mark_alert_pushed(sid, today)  # 无渠道也标记，避免反复尝试
                continue
            alerts = generate_alerts(sid)
            if not alerts:
                mark_alert_pushed(sid, today)  # 当日无预警，标记已处理
                continue
            notifier.send_store_alert(store.get("name", ""), alerts, targets)
            mark_alert_pushed(sid, today)
            logger.info("[预警推送] 已处理 store=%s alerts=%d", store.get("name", ""), len(alerts))
        except Exception:
            logger.exception("[预警推送] 处理异常 store=%s", sid)


def run_daily_alert_push():
    """每日定时推送门店业务预警（独立于自动预测，默认 06:30）。"""
    try:
        init_db()
        init_cache()
        stores = get_alert_push_stores()
        if not stores:
            logger.info("[预警推送] 无开启推送的门店，跳过")
            return
        logger.info("[预警推送] 开始 门店数=%d", len(stores))
        push_store_alerts_for_stores([s["id"] for s in stores])
        logger.info("[预警推送] 完成")
    except Exception:
        logger.exception("[预警推送] 每日任务异常")


def _is_pos_source_due(src: dict, now) -> bool:
    """判断某 POS 源在 now 时刻是否「到点且今天未拉过」（需触发拉取）。

    - schedule 为空/非法 -> 回落全局默认时间（POS_PULL_HOUR/POS_PULL_MINUTE，默认 23:30）；
    - 仅当「当前时刻 >= 今日计划时刻」且「last_sync_at 不是今天」才返回 True，天然幂等。
    """
    default_h = int(os.getenv("POS_PULL_HOUR", "23"))
    default_m = int(os.getenv("POS_PULL_MINUTE", "30"))
    sched = parse_pos_schedule(src.get("schedule") or "")
    if sched is None:
        sched = (default_h, default_m)
    h, mi = sched
    sched_today = now.replace(hour=h, minute=mi, second=0, microsecond=0)
    last = src.get("last_sync_at") or ""
    last_date = last[:10] if len(last) >= 10 else ""
    return now >= sched_today and last_date != now.strftime("%Y-%m-%d")


def dispatch_pos_pulls_and_predict():
    """每 N 分钟运行一次（默认 5）：按每个 POS 源的 schedule 到点拉取（今天未拉过才拉），
    拉取成功的门店立即预测「明日」并推送采购单（主链路：POS 同步完成 -> 自动预测）。

    - schedule 为空或非法时回落到全局默认时间（POS_PULL_HOUR / POS_PULL_MINUTE，默认 23:30）；
    - webhook / 禁用源跳过；
    - 仅当「已过计划时刻」且「今天尚未同步」时才拉取，天然幂等。
    """
    try:
        init_db()
        init_cache()
        now = datetime.now()
        due_stores = set()
        for src in get_all_pos_sources():
            if not src.get("enabled") or src.get("source_type") == "webhook":
                continue
            if not _is_pos_source_due(src, now):
                continue
            try:
                res = pos_client.pull_source(src["id"])
                if res.get("ok"):
                    due_stores.add(src["store_id"])
            except Exception:
                logger.exception("[POS分发] 拉取异常 source_id=%s", src["id"])
        if due_stores and int(os.getenv("AUTO_PREDICT_AFTER_POS", "1")):
            for sid in due_stores:
                run_auto_prediction_for_stores([sid])
    except Exception:
        logger.exception("[POS分发] 异常")


# ==================== 调度器实例 + 单实例守卫 ====================
# 跨进程单实例锁：单机云部署下，防止 Streamlit 多 worker / 进程重载导致
# 多个调度器实例同时运行、重复执行每日预测。文件锁实现，轻量且无外部依赖。
# （未来若扩为多副本集群，应改为基于 DB 的分布式锁。）
# [B1 安全加固] 跨平台进程存活探测：Windows 下改用 ctypes 调 OpenProcess/GetExitCodeProcess
# （os.kill(pid,0) 在 Windows 的信号语义与 Unix 不同，可能误判），并增加心跳时间戳
# 防过期锁长期卡死。
_SCHEDULER_LOCK = Path(__file__).resolve().parent.parent.parent / "data" / "scheduler.lock"
# 锁文件过期阈值（秒）：超过此年龄且进程探测存活仍失败则强制清理；默认 24h
_SCHEDULER_LOCK_STALE = max(60, int(os.getenv("GUOBEICAI_SCHEDULER_LOCK_STALE", "86400")))


def _pid_alive(pid: int) -> bool:
    """跨平台判断进程是否存活（不发送信号）。"""
    if pid <= 0:
        return False
    if sys.platform.startswith("win"):
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_INFORMATION = 0x0400
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
            if not handle:
                return False
            exit_code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return exit_code.value == STILL_ACTIVE
        except Exception:
            # 回退：os.kill 探测（Windows 下 pid 不存在会抛 OSError）
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ValueError):
            return False


def _acquire_scheduler_lock() -> bool:
    """尝试获取调度器单实例锁。

    返回 False 的情形：存在锁且持有进程仍存活（且未过期）。
    锁文件内容：`<pid>|<iso 时间戳>`；进程已死或锁已过期则清理后重建。
    """
    try:
        if _SCHEDULER_LOCK.exists():
            raw = _SCHEDULER_LOCK.read_text(encoding="utf-8").strip()
            old_pid = None
            ts = 0.0
            try:
                pid_s, ts_s = raw.split("|", 1)
                old_pid = int(pid_s)
                ts = float(ts_s)
            except (ValueError, AttributeError):
                # 旧格式（仅 pid）或格式损坏 -> 当作过期处理
                try:
                    old_pid = int(raw)
                except ValueError:
                    old_pid = None
            is_stale = (time.time() - ts) > _SCHEDULER_LOCK_STALE if ts else True
            if old_pid and _pid_alive(old_pid) and not is_stale:
                logger.warning("[定时预测] 调度器锁已被进程 %s 持有，放弃启动", old_pid)
                return False
            logger.info("[定时预测] 发现过期/失效调度器锁(pid=%s)，清理重建", old_pid)
            _SCHEDULER_LOCK.unlink(missing_ok=True)
        _SCHEDULER_LOCK.write_text(f"{os.getpid()}|{time.time()}", encoding="utf-8")
        atexit.register(_release_scheduler_lock)
        return True
    except Exception as e:  # 锁获取异常不应阻断主程序启动
        logger.error("[定时预测] 获取调度器锁失败: %s", e)
        return False


def _release_scheduler_lock() -> None:
    """进程退出时释放锁（仅当锁属于本进程）。"""
    try:
        if _SCHEDULER_LOCK.exists():
            raw = _SCHEDULER_LOCK.read_text(encoding="utf-8").strip()
            txt = raw.split("|", 1)[0]
            if txt == str(os.getpid()):
                _SCHEDULER_LOCK.unlink(missing_ok=True)
    except Exception:
        pass


scheduler = BackgroundScheduler()


def start_scheduler():
    """启动调度器（在 app.py 主入口调用）。

    守卫策略（双重）：
    1. 跨进程：文件锁，防止多 worker / 进程重载导致重复调度；
    2. 进程内：app.py 用 session_state 标记，防止脚本重跑重复启动。

    自动预测：改为按门店配置的 auto_predict_time 独立触发（默认 06:00），
    调度器每 AUTO_PREDICT_INTERVAL_MIN 分钟巡检一次（默认 5）。
    POS 同步：改为按各 POS 源 schedule 独立时间触发，调度器每
    POS_DISPATCH_INTERVAL_MIN 分钟巡检一次（默认 5）。
    """
    if not _acquire_scheduler_lock():
        logger.warning("[定时预测] 未获取调度器锁，跳过启动（已有实例在运行）")
        return

    # 统一日志（幂等；确保调度进程日志也落盘）
    try:
        from guibeicai.web._logging_setup import init_logging
        init_logging()
    except Exception:
        pass
    if not scheduler.running:
        # 自动预测：改为「每 N 分钟巡检」按各门店 auto_predict_time 独立时间触发
        # （默认每 5 分钟巡检，到点且今天未预测过才执行；目标日由门店自己的
        #  auto_predict_target_offset 决定）。
        auto_predict_interval = max(1, int(os.getenv("AUTO_PREDICT_INTERVAL_MIN", "5")))
        scheduler.add_job(
            run_daily_prediction,
            "interval",
            minutes=auto_predict_interval,
            id="daily_prediction",
            replace_existing=True,
        )
        # POS/收银数据源：改为「每 N 分钟分发器」按各源 schedule 独立时间拉取
        # （默认每 5 分钟巡检，到点且今天未拉过才拉；时间仍可用
        #  POS_PULL_HOUR / POS_PULL_MINUTE 作为空 schedule 的全局默认）。
        pos_dispatch_interval = max(1, int(os.getenv("POS_DISPATCH_INTERVAL_MIN", "5")))
        scheduler.add_job(
            dispatch_pos_pulls_and_predict,
            "interval",
            minutes=pos_dispatch_interval,
            id="pos_dispatch",
            replace_existing=True,
        )
        # 数据库月备（默认每月 1 日 03:00，DB_BACKUP_DAY/HOUR/MINUTE 可调）
        from guibeicai.data_import.db_backup import backup_database, prune_backups

        def _monthly_db_backup():
            try:
                backup_database()
                prune_backups()
            except Exception:
                logger.exception("[数据库备份] 月备失败")

        scheduler.add_job(
            _monthly_db_backup,
            "cron",
            day=int(os.getenv("DB_BACKUP_DAY", "1")),
            hour=int(os.getenv("DB_BACKUP_HOUR", "3")),
            minute=int(os.getenv("DB_BACKUP_MINUTE", "0")),
            id="monthly_db_backup",
            replace_existing=True,
        )
        # 门店业务预警自动推送（默认每日 06:30，ALERT_PUSH_HOUR/MINUTE 可调）
        scheduler.add_job(
            run_daily_alert_push,
            "cron",
            hour=int(os.getenv("ALERT_PUSH_HOUR", "6")),
            minute=int(os.getenv("ALERT_PUSH_MINUTE", "30")),
            id="daily_alert_push",
            replace_existing=True,
        )
        scheduler.start()
        logger.info(
            "[定时预测] 调度器已启动，每天 %02d:%02d 执行",
            settings.scheduler_hour, settings.scheduler_minute,
        )