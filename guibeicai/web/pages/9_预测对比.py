"""预测对比页面：单日单店对比预测 vs 实际销量"""

import streamlit as st
import pandas as pd
from datetime import date, timedelta

from guibeicai.web._nav import render_nav
from guibeicai.web._db import (
    init_db, get_all_stores, get_prediction_by_store_date, get_sales_history_df,
    get_store_by_id, get_user_store_ids, get_dishes_by_store,
    get_actual_sales_by_store_date, get_actual_sales_reconciled_by_store_date,
    get_latest_actual_qty,
)
from guibeicai.web.components import page_header, banner

init_db()

from guibeicai.web._auth import require_auth, require_page_access, is_admin

user = require_auth()
require_page_access("compare")

st.set_page_config(page_title="预测对比", page_icon="📊", layout="wide")
render_nav()

page_header("预测与实际销量对比", "核对 AI 预测与真实销量，量化偏差、持续校准模型", icon="📊")
banner("info", "对比逻辑：预测日期 = 昨日（T-1）；实际销量优先取「每日销量录入 / POS 对接」数据，无则 fallback 到批量导入历史。表格「数据来源」列标注每条实际销量的出处（POS自动 / 手工 / 历史导入）。")

# ==================== 多租户：门店选择 ====================
if is_admin(user):
    stores = get_all_stores(active_only=True)
    if not stores:
        st.warning("暂无门店数据，请先去「数据上传」页上传数据。")
        st.stop()
    store_options = {s["name"]: s["id"] for s in stores}
    selected_store_name = st.selectbox("选择门店", list(store_options.keys()))
    store_id = store_options[selected_store_name]
else:
    allowed_ids = set(get_user_store_ids(user.get("id", 0)))
    if not allowed_ids:
        st.error("❌ 您没有绑定任何门店，请联系管理员。")
        st.stop()
    stores = get_all_stores(active_only=True)
    allowed_stores = [s for s in stores if s["id"] in allowed_ids]
    if not allowed_stores:
        st.warning("暂无可用门店数据。")
        st.stop()
    store_options = {s["name"]: s["id"] for s in allowed_stores}
    selected_store_name = st.selectbox("选择门店", list(store_options.keys()), key="cmp_store")
    store_id = store_options[selected_store_name]
# ==================== 门店选择结束 ====================

# ==================== 日期选择：默认昨日，单日 ====================
yesterday = date.today() - timedelta(days=1)
min_date = date.today() - timedelta(days=7)

selected_date = st.date_input(
    "对比日期",
    value=yesterday,
    min_value=min_date,
    max_value=yesterday,
    format="YYYY-MM-DD",
    key="cmp_date",
    help="默认昨日（T-1），允许查看过去 7 天",
)
st.caption(f"📅 当前对比：{selected_store_name} · {selected_date.strftime('%Y-%m-%d')}")

date_str = selected_date.strftime("%Y-%m-%d")

# ==================== 加载预测 ====================
pred = get_prediction_by_store_date(store_id, date_str)

# ==================== 加载实际销量 ====================
# 优先：每日销量录入（actual_sales）
actual_dict = get_actual_sales_by_store_date(store_id, date_str)

# 把 actual_dict 的 key 统一转为菜品名称字符串（兼容 dish_id/int 与 dish_name/str）
dishes_map = {d["id"]: d["name"] for d in get_dishes_by_store(store_id, active_only=False)}
normalized_actual = {}
for k, v in actual_dict.items():
    if isinstance(k, int) or (isinstance(k, str) and k.isdigit()):
        name = dishes_map.get(int(k), f"菜品#{k}")
    else:
        name = str(k)
    normalized_actual[str(name)] = float(v)
actual_dict = normalized_actual

# 数据来源标记（POS自动 / 手工）：从 actual_sales 的 reconciled 列读取，按菜品名对齐
_rec_raw = get_actual_sales_reconciled_by_store_date(store_id, date_str)
reconciled_by_name = {}
for _k, _v in _rec_raw.items():
    if isinstance(_k, int) or (isinstance(_k, str) and str(_k).isdigit()):
        _name = dishes_map.get(int(_k), f"菜品#{_k}")
    else:
        _name = str(_k)
    reconciled_by_name[str(_name)] = int(_v)

# fallback：批量导入历史（sales_history）
sales_df = get_sales_history_df(store_id=store_id)
has_history_fallback = False
if not actual_dict and not sales_df.empty:
    sales_df["sale_date"] = sales_df["sale_date"].astype(str)
    day_sales = sales_df[sales_df["sale_date"] == date_str].copy()
    if len(day_sales) > 0:
        actual_dict = dict(zip(day_sales["dish_name"].astype(str).str.strip(), day_sales["qty"]))
        has_history_fallback = True

# ==================== 状态判断 ====================
has_pred = pred is not None
has_actual = len(actual_dict) > 0

if not has_pred and not has_actual:
    st.info(f"📭 {selected_store_name} · {date_str} 既无预测记录也无实际销量数据。")
    st.stop()

# ==================== 顶部状态提示 ====================
if has_pred and not has_actual:
    st.warning("⚠️ 有预测记录，但当日实际销量尚未录入。请前往「数据上传 → 每日销量录入」补录。")
elif has_actual and not has_pred:
    st.warning("⚠️ 有实际销量，但当日无预测记录。请前往「预测配置」执行预测。")
elif has_pred and has_actual:
    if has_history_fallback:
        st.info("ℹ️ 实际销量来自批量导入历史（未找到每日销量录入记录），偏差仅供参考。")
    else:
        st.success("✅ 预测与实际销量均已就绪，可进行对比。")

# ==================== 汇总卡片 ====================
if has_pred or has_actual:
    st.divider()
    st.subheader("📈 汇总概览")

    # 计算实际营业额
    if has_actual and has_pred:
        pred_result = pred["result"] if isinstance(pred, dict) else {}
        pred_by_dish = pred_result.get("dish_predictions", {}) if isinstance(pred_result, dict) else {}
        actual_revenue = 0
        for dish_name, actual_qty in actual_dict.items():
            pred_price = 0
            for d in (pred_result.get("dish_predictions", {}).keys() if isinstance(pred_result, dict) else []):
                pass
            # 从预测结果取单价
            if isinstance(pred_result, dict) and "purchase_details" in pred_result:
                for pd_row in pred_result["purchase_details"]:
                    if pd_row.get("related_dish") == dish_name:
                        pred_price = pd_row.get("unit_cost", 0)
                        break
            actual_revenue += actual_qty * pred_price

        pred_revenue = pred.get("revenue", 0) if isinstance(pred, dict) else 0

        # 计算 MAPE
        abs_errors = []
        for dish_name, actual_qty in actual_dict.items():
            pred_qty = pred_by_dish.get(dish_name, 0)
            if pred_qty > 0:
                abs_errors.append(abs(actual_qty - pred_qty) / pred_qty)
        mape = (sum(abs_errors) / len(abs_errors) * 100) if abs_errors else None

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("预测营业额", f"¥{pred_revenue:,.2f}")
        c2.metric("实际营业额", f"¥{actual_revenue:,.2f}")
        c3.metric("平均绝对百分比误差(MAPE)", f"{mape:.1f}%" if mape is not None else "N/A")
        c4.metric(
            "偏差方向",
            "预测偏少 ↑" if actual_revenue > pred_revenue else "预测偏多 ↓",
            delta=f"{(actual_revenue - pred_revenue):,.0f}" if pred_revenue > 0 else None,
        )

# ==================== 菜品级对比表 ====================
if has_pred and has_actual:
    st.divider()
    st.subheader("🍳 菜品级对比")

    pred_result = pred["result"] if isinstance(pred, dict) else {}
    pred_by_dish = pred_result.get("dish_predictions", {}) if isinstance(pred_result, dict) else {}
    pred_by_dish = {str(k): float(v) for k, v in pred_by_dish.items() if v is not None}

    # 合并所有菜品（预测 + 实际），统一为字符串 key 避免类型混排
    all_dishes = set(str(k) for k in pred_by_dish.keys()) | set(str(k) for k in actual_dict.keys())

    cmp_rows = []
    for dish_name in sorted(all_dishes):
        predicted_qty = pred_by_dish.get(dish_name, 0)
        actual_qty = actual_dict.get(dish_name, 0)

        if predicted_qty > 0:
            bias_abs = actual_qty - predicted_qty
            bias_pct = bias_abs / predicted_qty * 100
        else:
            bias_abs = None
            bias_pct = None

        # 取单价
        unit_price = 0
        if isinstance(pred_result, dict) and "purchase_details" in pred_result:
            for pd_row in pred_result["purchase_details"]:
                if pd_row.get("related_dish") == dish_name:
                    unit_price = pd_row.get("unit_cost", 0)
                    break

        # 数据来源：actual_sales 有记录→POS自动/手工；否则若来自历史导入→历史导入
        _rec = reconciled_by_name.get(dish_name)
        if _rec is None:
            _source = "历史导入" if has_history_fallback else "—"
        else:
            _source = "POS自动" if _rec else "手工"

        cmp_rows.append({
            "菜品名称": dish_name,
            "预测销量": round(predicted_qty, 1),
            "实际销量": round(actual_qty, 1),
            "绝对偏差": round(bias_abs, 1) if bias_abs is not None else None,
            "偏差率(%)": round(bias_pct, 1) if bias_pct is not None else None,
            "数据来源": _source,
            "实际营业额": round(actual_qty * unit_price, 2),
        })

    cmp_df = pd.DataFrame(cmp_rows)

    # 排序：按偏差率绝对值降序
    cmp_df = cmp_df.sort_values("偏差率(%)", key=lambda x: x.abs(), ascending=False, na_position="last")

    def color_bias(val):
        if pd.isna(val):
            return ""
        if abs(val) > 30:
            return "color: #d32f2f; font-weight: bold"
        elif abs(val) > 15:
            return "color: #f57c00"
        else:
            return "color: #388e3c"

    styled = cmp_df.style.map(color_bias, subset=["偏差率(%)"])
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # 偏差分布统计
    entered_cmp = cmp_df[cmp_df["偏差率(%)"].notna()]
    if len(entered_cmp) > 0:
        st.divider()
        st.subheader("📊 偏差分布")

        sc1, sc2, sc3, sc4 = st.columns(4)
        sc1.metric("可对比菜品数", len(entered_cmp))
        sc2.metric("平均偏差率", f"{entered_cmp['偏差率(%)'].mean():.1f}%")
        sc3.metric("预测偏多(<-15%)", len(entered_cmp[entered_cmp["偏差率(%)"] < -15]))
        sc4.metric("预测偏少(>15%)", len(entered_cmp[entered_cmp["偏差率(%)"] > 15]))

        # 偏差方向占比
        over_pct = len(entered_cmp[entered_cmp["偏差率(%)"] > 15]) / len(entered_cmp) * 100
        under_pct = len(entered_cmp[entered_cmp["偏差率(%)"] < -15]) / len(entered_cmp) * 100

        if over_pct > 30:
            st.warning(f"⚠️ {over_pct:.0f}% 的菜品预测偏少（>15%），模型可能系统性低估，建议提高预测系数或检查 LLM 分析。")
        if under_pct > 30:
            st.warning(f"⚠️ {under_pct:.0f}% 的菜品预测偏多（>15%），模型可能系统性高估，建议降低预测系数。")

    st.caption("💡 偏差率 = (实际销量 - 预测销量) ÷ 预测销量 × 100%。正值为预测偏少，负值为预测偏多。")

elif has_pred and not has_actual:
    # 只有预测，展示预测明细
    st.divider()
    st.subheader("🔮 预测明细（无实际销量可对比）")
    pred_result = pred["result"] if isinstance(pred, dict) else {}
    pred_by_dish = pred_result.get("dish_predictions", {}) if isinstance(pred_result, dict) else {}

    pred_rows = []
    for dish_name, qty in sorted(pred_by_dish.items()):
        pred_rows.append({
            "菜品名称": dish_name,
            "预测销量": round(qty, 1),
        })
    st.dataframe(pd.DataFrame(pred_rows), use_container_width=True, hide_index=True)

elif not has_pred and has_actual:
    # 只有实际销量，展示实际明细
    st.divider()
    st.subheader("📋 实际销量明细（无预测可对比）")
    actual_rows = []
    for dish_name, qty in sorted(actual_dict.items()):
        actual_rows.append({
            "菜品名称": dish_name,
            "实际销量": round(qty, 1),
        })
    st.dataframe(pd.DataFrame(actual_rows), use_container_width=True, hide_index=True)