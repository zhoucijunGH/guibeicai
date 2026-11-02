import streamlit as st
from datetime import date, datetime
import pandas as pd
import plotly.express as px
import logging

from guibeicai.web._nav import render_nav
from guibeicai.config.settings import settings
from guibeicai.web._db import init_db, load_all_history, get_all_stores, get_sales_history_df, get_user_store_ids
from guibeicai.web._auth import is_admin
from guibeicai.web._cache import init_cache
from guibeicai.web._factors import ensure_history_factors
from guibeicai.web._scheduler import start_scheduler
from guibeicai.web._logging_setup import init_logging
from guibeicai.web.components import (
    hero_banner, page_header, metric_card, section, empty_state,
    banner, quick_link_card,
)

init_db()
init_cache()
init_logging()

# ========== 启动定时预测调度器（防重复启动） ==========
if not st.session_state.get("_scheduler_started", False):
    start_scheduler()
    st.session_state["_scheduler_started"] = True

# ========== 启动时后台补齐历史天气（只跑一次） ==========
@st.cache_resource
def backfill_history_factors_on_startup():
    """程序启动时，对所有门店的历史销量日期补齐天气因素，只执行一次"""
    stores = get_all_stores(active_only=True)
    for s in stores:
        try:
            sales_df = get_sales_history_df(s["id"])
            if sales_df is None or len(sales_df) == 0:
                continue
            sales_df["sale_date"] = pd.to_datetime(sales_df["sale_date"], errors="coerce")
            sales_df = sales_df.dropna(subset=["sale_date"])
            if len(sales_df) == 0:
                continue
            hist_dates = sales_df["sale_date"].unique()
            ensure_history_factors(s["id"], s["city"], hist_dates)
        except Exception:
            pass
    return True

backfill_history_factors_on_startup()

# ========== 启动时密钥校验（仅告警，不阻断启动；#15 密钥托管） ==========
@st.cache_resource
def _check_secrets_on_startup():
    missing = settings.validate_secrets()
    if missing:
        logging.getLogger(__name__).warning(
            "[secrets] 以下环境变量未配置，相关功能将降级/不可用: %s", ", ".join(missing)
        )
    else:
        logging.getLogger(__name__).info("[secrets] 关键密钥均已配置")
    return missing

_check_secrets_on_startup()

# ========== 登录检查 ==========
from guibeicai.web._auth import require_auth, require_page_access

user = require_auth()
require_page_access("dashboard")

st.set_page_config(page_title="桂备菜 - 工作台", page_icon="🏠", layout="wide")
render_nav()

# ==================== Hero 欢迎横幅 ====================
_now = datetime.now()
_hour = _now.hour
if 5 <= _hour < 11:
    _greet, _greet_icon = "早上好", "🌅"
elif 11 <= _hour < 14:
    _greet, _greet_icon = "中午好", "🍜"
elif 14 <= _hour < 18:
    _greet, _greet_icon = "下午好", "☕"
else:
    _greet, _greet_icon = "晚上好", "🌙"
_weekdays = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
_date_label = f"{_now.year} 年 {_now.month} 月 {_now.day} 日 · {_weekdays[_now.weekday()]}"
_username = user.get("username", "伙伴")

hero_banner(
    f"{_greet_icon} {_greet}，{_username}",
    f"{_date_label} ｜ AI 已为您盘好今日备菜数据",
    chips=["🔥 智能销量预测", "📉 降损耗", "💰 降本增效", "🎯 精准备货"],
    icon="🥘",
)

# ==================== 加载数据 ====================
history = load_all_history()
db_stores = get_all_stores(active_only=False)
id_to_name = {s["id"]: s["name"] for s in db_stores}

if history:
    df_all = pd.DataFrame(history)
    df_all["predict_date"] = pd.to_datetime(df_all["predict_date"])
    df_all["store_display"] = df_all["store_id"].map(id_to_name).fillna("未指定门店")

    if not is_admin(user):
        allowed_ids = set(get_user_store_ids(user.get("id", 0)))
        df_all = df_all[df_all["store_id"].isin(allowed_ids)].copy()

    df_all["run_time"] = pd.to_datetime(df_all["run_time"])
    df_dedup = df_all.sort_values("run_time", ascending=False).drop_duplicates(
        subset=["store_display", "predict_date"], keep="first"
    ).copy()

    def _extract_financial(df, field):
        if field in df.columns:
            return df[field]
        col = df["result_json"] if "result_json" in df.columns else df.get("result", pd.Series([0] * len(df)))
        def _pick(x):
            if isinstance(x, str):
                try:
                    import json as _json
                    x = _json.loads(x)
                except Exception:
                    return 0
            if isinstance(x, dict):
                fin = x.get("financial", x)
                if isinstance(fin, dict):
                    return fin.get(field, 0)
                return x.get(field, 0)
            return 0
        return col.apply(_pick)

    for col in ["revenue", "cost", "gross_margin"]:
        df_dedup[col] = pd.to_numeric(_extract_financial(df_dedup, col), errors="coerce").fillna(0)

    total_runs = len(df_dedup)
    total_revenue = df_dedup["revenue"].sum()
    total_cost = df_dedup["cost"].sum()
    avg_margin = (total_revenue - total_cost) / total_revenue * 100 if total_revenue > 0 else 0

    # ---- 环比：近 30 天 vs 前 30 天（按预测日期） ----
    today = pd.Timestamp(date.today())
    last30 = df_dedup[(df_dedup["predict_date"] >= today - pd.Timedelta(days=30))
                      & (df_dedup["predict_date"] <= today)]
    prev30 = df_dedup[(df_dedup["predict_date"] >= today - pd.Timedelta(days=60))
                      & (df_dedup["predict_date"] < today - pd.Timedelta(days=30))]
    rev30, rev_prev = last30["revenue"].sum(), prev30["revenue"].sum()
    if rev_prev > 0:
        _rev_delta = (rev30 - rev_prev) / rev_prev * 100
        rev_delta_txt = ("↑ " if _rev_delta >= 0 else "↓ ") + f"{abs(_rev_delta):.1f}% 环比上期"
    else:
        rev_delta_txt = ""

    store_cnt = df_dedup["store_display"].nunique()
    latest_run = df_dedup["run_time"].max()

    # ==================== 核心 KPI ====================
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        metric_card("累计预估营业额", f"¥{total_revenue:,.0f}", "历史预测合计",
                    variant="primary", icon="💰", delta=rev_delta_txt)
    with k2:
        metric_card("累计食材成本", f"¥{total_cost:,.0f}", "营业额 - 成本 = 毛利",
                    variant="warning", icon="🥬")
    with k3:
        metric_card("平均毛利率", f"{avg_margin:.1f}%", "越高说明备菜越精准",
                    variant="success", icon="📈")
    with k4:
        metric_card("覆盖门店", f"{store_cnt} 家", f"最近预测：{latest_run.strftime('%m-%d %H:%M') if pd.notna(latest_run) else '—'}",
                    variant="info", icon="🏪")
else:
    empty_state("📡", "还没有预测数据", "先上传销售数据并执行一次预测，工作台将展示核心指标。",
                action_label="📤 去上传数据", action_target="pages/1_数据上传.py")

# ==================== 近期预测 + 门店分布 ====================
if history:
    left, right = st.columns([3, 2])

    with left:
        section("预估营业额趋势", icon="📈")
        df_history = df_dedup.sort_values("predict_date")
        if not is_admin(user):
            allowed_ids = set(get_user_store_ids(user.get("id", 0)))
            allowed_names = {id_to_name.get(sid, "") for sid in allowed_ids}
            allowed_names.discard("")
            df_history = df_history[df_history["store_display"].isin(allowed_names)].copy()

        stores_sel = ["全部"] + sorted(df_history["store_display"].unique().tolist())
        selected_store = st.selectbox("选择门店", stores_sel, key="trend_store")
        if selected_store != "全部":
            df_history = df_history[df_history["store_display"] == selected_store]

        if len(df_history) > 0:
            daily = df_history.groupby(df_history["predict_date"].dt.strftime("%Y/%m/%d"), as_index=False)["revenue"].sum()
            fig_rev = px.area(daily, x="predict_date", y="revenue",
                              labels={"predict_date": "预测日期", "revenue": "预估营业额 (¥)"})
            fig_rev.update_traces(line_color="#d6453d", line_width=3,
                                  fillcolor="rgba(214,69,61,0.14)",
                                  hovertemplate="%{x}<br>¥%{y:,.0f}<extra></extra>")
            fig_rev.update_layout(height=330, xaxis=dict(fixedrange=True, showgrid=False),
                                  yaxis=dict(fixedrange=True), showlegend=False,
                                  margin=dict(l=10, r=10, t=18, b=10))
            st.plotly_chart(fig_rev, use_container_width=True)
        else:
            st.info("该门店暂无趋势数据。")

    with right:
        section("门店营业额分布", icon="🏪")
        store_rev = (df_dedup.groupby("store_display", as_index=False)["revenue"]
                     .sum().sort_values("revenue", ascending=False).head(6))
        if len(store_rev) > 0:
            fig_pie = px.pie(store_rev, names="store_display", values="revenue", hole=0.62)
            fig_pie.update_traces(textinfo="percent", textfont_size=12,
                                  marker=dict(line=dict(color="#ffffff", width=2)),
                                  hovertemplate="%{label}<br>¥%{value:,.0f}<extra></extra>")
            fig_pie.update_layout(height=330, showlegend=True,
                                  legend=dict(orientation="h", y=-0.15, x=0),
                                  margin=dict(l=10, r=10, t=18, b=40))
            st.plotly_chart(fig_pie, use_container_width=True)
        else:
            st.info("暂无门店数据。")

# ==================== 近期预测记录 ====================
if history:
    section("近期预测记录", icon="🕒")
    df_dedup = df_dedup.sort_values("predict_date", ascending=False)
    recent = df_dedup.head(8).copy()
    if len(recent) > 0:
        show = recent[["store_display", "predict_date", "revenue", "cost", "gross_margin"]].copy()
        show["predict_date"] = show["predict_date"].dt.strftime("%Y-%m-%d")
        show.columns = ["门店", "预测日期", "预估营业额", "食材成本", "毛利率(%)"]
        st.dataframe(
            show,
            use_container_width=True,
            hide_index=True,
            column_config={
                "门店": st.column_config.TextColumn("门店", width="medium"),
                "预估营业额": st.column_config.NumberColumn("预估营业额", format="¥,.0f"),
                "食材成本": st.column_config.NumberColumn("食材成本", format="¥,.0f"),
                "毛利率(%)": st.column_config.ProgressColumn(
                    "毛利率", min_value=0, max_value=100, format="%.1f%%"),
            },
        )
    st.page_link("pages/3_预测总览.py", label="📋 查看完整预测总览", use_container_width=False)

# ==================== 快捷入口 ====================
section("快捷入口", icon="🚀")
quick_links = [
    ("📤", "数据上传", "导入门店历史销量 Excel，AI 学习的基础", "pages/1_数据上传.py", "#d6453d", "#fdeeea"),
    ("⚙️", "执行预测", "一键生成未来营业额与备菜量预测", "pages/2_预测配置.py", "#e8a33d", "#fdf3e3"),
    ("🧾", "食材采购", "由预测结果自动反推食材采购清单", "pages/12_食材采购.py", "#2f9e63", "#e9f6ef"),
    ("📊", "预测对比", "预测 vs 实际，持续校准模型精度", "pages/9_预测对比.py", "#2e6fa7", "#e9f1f8"),
    ("🗑️", "损耗分析", "定位损耗大头，减少浪费真金白银", "pages/16_损耗分析.py", "#8a5fa8", "#f3eef8"),
    ("🔔", "智能预警", "异常波动、库存风险实时提醒", "pages/15_智能预警.py", "#c2703d", "#fbefe7"),
]
rows = [quick_links[i:i + 3] for i in range(0, len(quick_links), 3)]
for row in rows:
    cols = st.columns(3)
    for col, item in zip(cols, row):
        icon, title, desc, target, color, soft = item
        with col:
            quick_link_card(icon, title, desc, target, color=color, soft=soft)
