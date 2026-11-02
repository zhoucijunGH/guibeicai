import streamlit as st
import pandas as pd
from datetime import date
import json

from guibeicai.web._nav import render_nav
from guibeicai.web._db import init_db, load_all_history, get_all_stores, get_store_by_id,get_user_store_ids
from guibeicai.web.components import page_header, section, empty_state, metric_card

init_db()

from guibeicai.web._auth import require_auth, require_page_access, is_admin

# 登录检查
user = require_auth()
require_page_access("predict_overview")

st.set_page_config(page_title="预测总览", page_icon="📊", layout="wide")
render_nav()

page_header("预测总览", "历史预测记录的检索、筛选与明细查看", icon="📊")


def _to_date_str(v):
    if v is None:
        return ""
    if hasattr(v, "strftime"):
        return v.strftime("%Y-%m-%d")
    return str(v)


def _extract_result(rec):
    """优先从 result_json 取，兼容 result 字段，字符串自动解析"""
    r = rec.get("result_json") or rec.get("result")
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except Exception:
            r = {}
    if not isinstance(r, dict):
        r = {}
    return r


# [F4] 多租户 + 分页：非管理员仅加载其绑定门店的预测（DB 层过滤，避免全表加载）
if not is_admin(user):
    _allowed_ids = set(get_user_store_ids(user.get("id", 0)))
    history = load_all_history(store_ids=list(_allowed_ids) if _allowed_ids else None, limit=2000, days=365)
else:
    history = load_all_history(limit=2000, days=365)
if not history:
    empty_state("📭", "暂无预测记录", "请先完成一次预测，这里将展示历史预测明细与财务指标。",
                action_label="⚙️ 去配置预测", action_target="pages/2_预测配置.py")
    st.stop()

# 提取 result
for h in history:
    h["_result"] = _extract_result(h)

df = pd.DataFrame(history)
df["predict_date"] = pd.to_datetime(df["predict_date"]).dt.date
df["run_time"] = pd.to_datetime(df["run_time"])

db_stores = get_all_stores(active_only=False)
id_to_name = {s["id"]: s["name"] for s in db_stores}

# ===== 兼容 store 字段不存在的情况 =====
df["store_display"] = df["store_id"].map(id_to_name)
if "store" in df.columns:
    df["store_display"] = df["store_display"].fillna(df["store"])
df["store_display"] = df["store_display"].fillna("未指定门店")

# ==================== 多租户过滤 ====================
if not is_admin(user):
    allowed_ids = set(get_user_store_ids(user.get("id", 0)))
    if not allowed_ids:
        st.error("❌ 您没有绑定任何门店，请联系管理员。"); st.stop()
    df = df[df["store_id"].isin(allowed_ids)].copy()
    if len(df) == 0:
        st.info("暂无您的门店预测记录。"); st.stop()
# ==================== 过滤结束 ====================

st.divider()

# ==================== 筛选栏 ====================
section("筛选条件", icon="🔍")

c1, c2, c3 = st.columns(3)
with c1:
    if is_admin(user):
        stores = ["全部"] + sorted(df["store_display"].unique().tolist())
        filter_store = st.selectbox("门店", stores, key="overview_filter_store")
    else:
        # 商家：显示有权限的门店下拉框（多店可选）
        allowed_ids = set(get_user_store_ids(user.get("id", 0)))
        allowed_names = [id_to_name[sid] for sid in allowed_ids if sid in id_to_name]
        stores = ["全部"] + sorted(allowed_names)
        filter_store = st.selectbox("门店", stores, key="overview_filter_store")
with c2:
    min_date = df["predict_date"].min()
    max_date = df["predict_date"].max()
    start_date = st.date_input("开始日期", value=min_date, min_value=min_date, max_value=max_date, key="overview_filter_start_date")
with c3:
    end_date = st.date_input("结束日期", value=max_date, min_value=min_date, max_value=max_date, key="overview_filter_end_date")

filtered = df.copy()
if is_admin(user) and filter_store != "全部":
    filtered = filtered[filtered["store_display"] == filter_store]
if start_date and end_date:
    filtered = filtered[(filtered["predict_date"] >= start_date) & (filtered["predict_date"] <= end_date)]

filtered = filtered.sort_values("run_time", ascending=False).reset_index(drop=True)

st.divider()

# ==================== 结果表格 ====================
section(f"筛选结果（共 {len(filtered)} 条）", icon="📋")

if len(filtered) == 0:
    st.info("没有匹配的记录。")
    st.stop()

# [F3] 分页：避免大表一次性渲染全部行（最多 2000 条已在上游 limit，此处分页展示）
PAGE_SIZE_OPTIONS = [50, 100, 200, 500]
page_size = st.selectbox("每页条数", PAGE_SIZE_OPTIONS, index=0, key="overview_page_size")
total = len(filtered)
total_pages = max(1, (total + page_size - 1) // page_size)
cur_page = int(st.session_state.get("overview_page", 1))
if cur_page > total_pages:
    cur_page = total_pages
    st.session_state["overview_page"] = cur_page
prev_col, next_col, info_col = st.columns([1, 1, 3])
with prev_col:
    if st.button("⬅️ 上一页", disabled=(cur_page <= 1), key="overview_prev_page", use_container_width=True):
        st.session_state["overview_page"] = max(1, cur_page - 1)
        st.rerun()
with next_col:
    if st.button("下一页 ➡️", disabled=(cur_page >= total_pages), key="overview_next_page", use_container_width=True):
        st.session_state["overview_page"] = min(total_pages, cur_page + 1)
        st.rerun()
with info_col:
    st.markdown(f"第 **{cur_page} / {total_pages}** 页（每页 {page_size} 条）")

start = (cur_page - 1) * page_size
end = min(start + page_size, total)
page_df = filtered.iloc[start:end].copy()

display_cols = ["run_time", "store_display", "predict_date", "revenue", "cost", "gross_margin"]
display_df = page_df[display_cols].copy()
display_df["revenue"] = display_df["revenue"].apply(lambda x: f"¥{x:,.0f}" if pd.notna(x) else "—")
display_df["cost"] = display_df["cost"].apply(lambda x: f"¥{x:,.0f}" if pd.notna(x) else "—")
display_df["gross_margin"] = display_df["gross_margin"].apply(lambda x: f"{x:.1f}%" if pd.notna(x) else "—")
display_df.columns = ["运行时间", "门店", "预测日期", "预估营业额", "食材成本", "毛利率"]

event = st.dataframe(
    display_df, use_container_width=True, hide_index=False,
    on_select="rerun", selection_mode="single-row", key="overview_table",
)

selected_rows = event.selection.rows if hasattr(event, "selection") and event.selection.rows else []
if len(selected_rows) > 0:
    row_idx = selected_rows[0]
    record = page_df.iloc[row_idx]
    result = record["_result"]
    fin = result.get("financial", {})

    st.divider()
    section("预测详情", icon="📊")

    pred_date = _to_date_str(fin.get("predict_date", record["predict_date"]))
    c1, c2, c3 = st.columns(3)
    with c1:
        metric_card("预测日期", pred_date, variant="info")
    with c2:
        metric_card("预估营业额", f"¥{fin.get('revenue', record.get('revenue', 0)):,.2f}", variant="primary")
    with c3:
        metric_card("毛利率", f"{fin.get('gross_margin', record.get('gross_margin', 0)):.1f}%", variant="success")
    st.markdown(f"**门店：** {fin.get('store', record['store_display'])} ｜ **食材总成本：** ¥{fin.get('cost', record.get('cost', 0)):,.2f}")

    risk_level = result.get("risk_level", "低")
    conclusion = result.get("conclusion", "")
    purchase_advice = result.get("purchase_advice", "")
    risk_factors = result.get("risk_factors", [])

    if risk_level:
        risk_color = {"高": "🔴", "偏高": "🟡", "低": "🟢"}.get(risk_level, "⚪")
        st.markdown(f"**风险等级：** {risk_color} {risk_level}")
    if conclusion:
        st.markdown(f"**结论：** {conclusion}")
    if purchase_advice:
        st.markdown(f"**采购建议：** {purchase_advice}")
    if risk_factors:
        st.markdown("**风险因素：**")
        for rf in risk_factors:
            st.markdown(f"- {rf}")

    # ========== 采购明细 ==========
    purchase_details = result.get("purchase_details", [])
    if purchase_details:
        st.divider()
        st.markdown("#### 🛒 采购明细")

        detail_df = pd.DataFrame(purchase_details)

        rename_map = {
            "category": "品类",
            "ingredient_name": "食材",
            "base_qty": "基础采购量",
            "suggested_qty": "建议采购量",
            "unit_cost": "单位成本(元)",
            "total_cost": "总成本(元)",
            "related_dish": "关联菜品",
            "risk_level": "风险等级",
            "conclusion": "预测结论",
            "purchase_advice": "采购建议",
        }
        detail_df = detail_df.rename(columns={k: v for k, v in rename_map.items() if k in detail_df.columns})

        if "采购建议" in detail_df.columns and "note" in detail_df.columns:
            detail_df["采购建议"] = detail_df.apply(
                lambda r: f"{r['采购建议']}（原因：{r['note']}）" if pd.notna(r.get("note")) and str(r["note"]).strip() else r["采购建议"],
                axis=1,
            )

        display_cols = ["品类", "食材", "基础采购量", "建议采购量", "单位成本(元)", "总成本(元)", "关联菜品", "风险等级", "预测结论", "采购建议"]
        show_cols = [c for c in display_cols if c in detail_df.columns]
        st.dataframe(detail_df[show_cols], use_container_width=True, hide_index=True)

        st.divider()
        file_date = _to_date_str(fin.get("predict_date", record["predict_date"]))
        file_store = fin.get("store", record["store_display"])
        file_name = f"采购明细_{file_store}_{file_date}.csv".replace(" ", "_")
        csv_bytes = detail_df[show_cols].to_csv(index=False, encoding="utf-8-sig")
        st.download_button(
            label="📥 下载采购明细 CSV",
            data=csv_bytes,
            file_name=file_name,
            mime="text/csv",
            use_container_width=False,
        )
    else:
        st.info("该记录没有采购明细数据。")