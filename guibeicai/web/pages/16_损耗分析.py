"""损耗分析页面：基于实际销量与实际损耗，量化门店/菜品损耗率与成本"""

import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import date, timedelta

from guibeicai.web._nav import render_nav
from guibeicai.web._db import init_db, get_all_stores, get_user_store_ids, get_dishes_by_store, get_actual_sales_range
from guibeicai.web.components import page_header, section, empty_state, metric_card, banner

init_db()

from guibeicai.web._auth import require_auth, require_page_access, is_admin

user = require_auth()
require_page_access("waste")

st.set_page_config(page_title="损耗分析", page_icon="🗑️", layout="wide")
render_nav()

page_header("损耗分析", "用实际销量与实际损耗算出损耗率与损耗成本，找到最该优化的菜品", icon="🗑️")

if is_admin(user):
    stores = get_all_stores(active_only=True)
    store_options = {s["name"]: s["id"] for s in stores}
else:
    allowed_ids = set(get_user_store_ids(user.get("id", 0)))
    stores = [s for s in get_all_stores(active_only=True) if s["id"] in allowed_ids]
    store_options = {s["name"]: s["id"] for s in stores}

if not store_options:
    empty_state("🏪", "暂无可用门店", "请先创建门店并上传数据。", action_label="🏪 去门店管理", action_target="pages/4_门店管理.py")
    st.stop()

sel_name = st.selectbox("选择门店", list(store_options.keys()), key="waste_store")
store_id = store_options[sel_name]

# 日期范围：默认近 14 天（与模拟 actual_sales 范围一致）
end = date.today() - timedelta(days=1)
start = end - timedelta(days=13)
col_s, col_e = st.columns(2)
with col_s:
    start = st.date_input("开始日期", value=start, key="waste_start")
with col_e:
    end = st.date_input("结束日期", value=end, key="waste_end")

rows = get_actual_sales_range(store_id, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"))
if not rows:
    empty_state("🗑️", "该区间暂无实际销量/损耗数据", "请先在「数据上传 → 每日销量录入」中登记实际销量与损耗。",
                action_label="📝 去录入实际销量", action_target="pages/1_数据上传.py")
    st.stop()

dishes = {d["id"]: d for d in get_dishes_by_store(store_id, active_only=False)}
# 聚合到菜品
agg = {}
total_qty = total_waste = total_cost = 0.0
for r in rows:
    did = r["dish_id"]
    q = float(r["actual_qty"] or 0)
    w = float(r["actual_waste"] or 0)
    price = float(dishes.get(did, {}).get("standard_price") or 0)
    if did not in agg:
        agg[did] = {"dish_id": did, "name": dishes.get(did, {}).get("name", f"菜品#{did}"),
                    "qty": 0.0, "waste": 0.0, "cost": 0.0}
    agg[did]["qty"] += q
    agg[did]["waste"] += w
    agg[did]["cost"] += w * price
    total_qty += q
    total_waste += w
    total_cost += w * price

waste_rate = (total_waste / total_qty * 100) if total_qty > 0 else 0

c1, c2, c3 = st.columns(3)
with c1:
    metric_card("总损耗量", f"{total_waste:,.1f}", f"区间总销量 {total_qty:,.0f}", variant="warning")
with c2:
    metric_card("综合损耗率", f"{waste_rate:.1f}%", "损耗量 / 总销量", variant=("danger" if waste_rate > 5 else "success"))
with c3:
    metric_card("损耗成本", f"¥{total_cost:,.0f}", "按菜品标准单价估算", variant="primary")

banner("info", "损耗率 = 实际损耗量 ÷ 实际销量；损耗成本按菜品标准单价估算，用于衡量浪费金额。")

st.divider()
section("菜品损耗排行", icon="📊")

detail = []
for d in agg.values():
    if d["qty"] > 0:
        detail.append({
            "菜品": d["name"],
            "总销量": round(d["qty"], 1),
            "损耗量": round(d["waste"], 1),
            "损耗率(%)": round(d["waste"] / d["qty"] * 100, 1),
            "损耗成本(元)": round(d["cost"], 1),
        })
detail.sort(key=lambda x: x["损耗成本(元)"], reverse=True)
df = pd.DataFrame(detail)
if len(df) > 0:
    st.dataframe(df, use_container_width=True, hide_index=True)
    top = df.head(15).copy()
    fig = px.bar(top, x="菜品", y="损耗成本(元)", title="Top 15 损耗成本菜品", color="损耗成本(元)",
                 color_continuous_scale="Reds")
    fig.update_layout(height=360, xaxis=dict(fixedrange=True), yaxis=dict(fixedrange=True),
                     margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)
    csv = df.to_csv(index=False, encoding="utf-8-sig")
    st.download_button("📥 下载损耗分析 CSV", csv, file_name=f"损耗分析_{sel_name}.csv", mime="text/csv")
else:
    st.info("暂无有效损耗数据。")
