"""食材采购页面：将菜品销量预测自动拆解到食材维度，汇总采购需求并支持导出采购单

合并原「食材级预测」(12_食材预测.py) 与「采购单生成」(13_采购单.py) 两个重复页面：
两者底层均调用 aggregate_ingredients / build_purchase_order，输出同一份食材聚合数据，
仅展示口径（预估用量 vs 采购数量）不同，故合并为一个页面。
"""

import streamlit as st
import pandas as pd
import plotly.express as px

from guibeicai.web._nav import render_nav
from guibeicai.web._db import init_db, get_all_stores, get_user_store_ids, get_store_push_targets, get_store_by_id
from guibeicai.web.components import page_header, section, empty_state, metric_card, banner
from guibeicai.web._ingredient_forecast import build_purchase_order, get_prediction_dates_by_store
from guibeicai.scheduler.notifiers.purchase_notifier import PurchaseNotifier

init_db()

from guibeicai.web._auth import require_auth, require_page_access, is_admin

user = require_auth()
require_page_access("ingredient")

st.set_page_config(page_title="食材采购", page_icon="🧾", layout="wide")
render_nav()

page_header("食材采购", "把菜品销量预测自动拆解到食材维度，直接看清要备多少料，并可一键导出采购单", icon="🧾")

# ==================== 门店选择 ====================
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

sel_name = st.selectbox("选择门店", list(store_options.keys()), key="ing_store")
store_id = store_options[sel_name]

# 日期选择：列出该门店所有可用预测日期
available_dates = get_prediction_dates_by_store(store_id)
if not available_dates:
    empty_state("🔮", "该门店还没有预测结果", "请先在「执行预测」生成一次预测，这里会自动拆解到食材。",
                action_label="⚙️ 去执行预测", action_target="pages/2_预测配置.py")
    st.stop()

selected_date = st.selectbox("选择预测日期", available_dates, key="ing_date")

# 基于选定日期聚合食材采购需求（build_purchase_order = aggregate_ingredients）
po = build_purchase_order(store_id, predict_date=selected_date)
items = po.get("items", [])
predict_date = po.get("predict_date") or selected_date

if not items:
    empty_state("🧾", "该日期暂无可聚合的食材需求", "请换一个日期，或确认菜品已配置用料表。",
                action_label="⚙️ 去执行预测", action_target="pages/2_预测配置.py")
    st.stop()

total_cost = po["total_cost"]
total_qty = po["total_qty"]
dish_count = po["dish_count"]

c1, c2, c3, c4 = st.columns(4)
with c1:
    metric_card("菜品数", f"{dish_count}", "参与预测的菜品", variant="info")
with c2:
    metric_card("食材种类", f"{len(items)} 种", "需采购的食材", variant="primary")
with c3:
    metric_card("预估总金额", f"¥{total_cost:,.0f}", "按配方单价估算", variant="success")
with c4:
    metric_card("预估总重", f"{total_qty:,.1f}", "所有食材用量合计", variant="default")

banner("info", f"以下为「{sel_name}」{predict_date} 预测的食材聚合需求，可直接导出采购单或导入 ERP。")

st.divider()
section("食材需求明细", icon="🧾")

df = pd.DataFrame(items)
if len(df) > 0:
    # 就地改名（保持 df 为展示用中文列名，供明细表/柱状图/CSV 复用）
    df = df.rename(columns={
        "ingredient_name": "食材", "category": "分类", "quantity": "采购数量",
        "unit": "单位", "est_cost": "预估金额(元)",
    })
    st.dataframe(df[["食材", "分类", "采购数量", "单位", "预估金额(元)"]],
                 use_container_width=True, hide_index=True)

    # Top 15 食材成本柱状图
    top = df.head(15).copy()
    fig = px.bar(top, x="食材", y="预估金额(元)", title="Top 15 食材成本", color="预估金额(元)",
                 color_continuous_scale="Reds")
    fig.update_layout(height=360, xaxis=dict(fixedrange=True), yaxis=dict(fixedrange=True),
                     margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)

    # 来源拆解（供下方 CSV 与表格复用）
    bd_rows = []
    for it in items:
        for b in it.get("breakdown", []):
            bd_rows.append({
                "食材": it["ingredient_name"], "分类": it["category"],
                "来源菜品": b["dish_name"], "分摊用量": b["quantity"],
                "单位": it["unit"], "分摊成本(元)": b["cost"],
            })
    bd_df = pd.DataFrame(bd_rows)
    if len(bd_df) > 0:
        bd_df = bd_df.sort_values(["食材", "分摊用量"], ascending=[True, False])
    bd_csv = bd_df[["食材", "分类", "来源菜品", "分摊用量", "单位", "分摊成本(元)"]].to_csv(index=False, encoding="utf-8-sig") if len(bd_df) > 0 else ""

    csv_demand = df[["食材", "分类", "采购数量", "单位", "预估金额(元)"]].to_csv(index=False, encoding="utf-8-sig")
    csv_po = df[["食材", "分类", "采购数量", "单位", "预估金额(元)"]].to_csv(index=False, encoding="utf-8-sig")

    cdl1, cdl2, cdl3 = st.columns(3)
    with cdl1:
        st.download_button("📥 下载食材需求 CSV", csv_demand,
                          file_name=f"食材需求_{sel_name}_{predict_date}.csv", mime="text/csv", use_container_width=True)
    with cdl2:
        st.download_button("🧾 下载采购单 CSV", csv_po,
                          file_name=f"采购单_{sel_name}_{predict_date}.csv", mime="text/csv", use_container_width=True)
    with cdl3:
        if bd_csv:
            st.download_button("📊 下载来源拆解 CSV", bd_csv,
                              file_name=f"食材需求_来源拆解_{sel_name}_{predict_date}.csv", mime="text/csv", use_container_width=True)
    st.caption("💡 采购单 CSV 可直接发给渠道商或其 ERP 进行备货；本系统聚焦需求预测与备货调度，具体供应商由渠道商线下/ERP 下单。")

    # ============ 推送采购单（移动端核心） ============
    st.divider()
    section("📤 推送采购单到老板", icon="📲")
    targets = get_store_push_targets(store_id)
    tcols = st.columns(3)
    with tcols[0]:
        st.metric("邮件接收人", len(targets["emails"]), help="来自门店「老板邮箱」配置")
    with tcols[1]:
        st.metric("微信接收人", len(targets["openids"]), help="绑定到该门店且已绑微信的用户")
    with tcols[2]:
        st.metric("企微机器人", "已配" if targets["wecom_webhook"] else "未配")
    if not (targets["emails"] or targets["openids"] or targets["wecom_webhook"]):
        st.warning("⚠️ 该门店尚未配置任何接收方式（老板邮箱 / 微信绑定 / 企微 webhook）。"
                   "请到「门店管理」填写老板邮箱与企微 webhook，并让老板在「桂备菜」小程序登录后绑定微信，"
                   "否则只能手动导出 CSV。")
    else:
        if st.button("🚀 立即推送采购单", type="primary", use_container_width=True,
                     key="push_po_btn"):
            with st.spinner("正在推送（邮件 / 企业微信 / 微信订阅消息）…"):
                cfg = {}
                try:
                    from guibeicai.config.settings import Settings
                    cfg = {"email": dict(Settings().email_config), "wx_subscribe": {}}
                except Exception:
                    cfg = {"email": {}, "wx_subscribe": {}}
                notifier = PurchaseNotifier(config=cfg)
                res = notifier.send_purchase_order(
                    order=po, store_name=sel_name, date=predict_date,
                    emails=targets["emails"], openids=targets["openids"],
                    wecom_webhook=targets["wecom_webhook"],
                )
            st.success(f"✅ 推送完成：邮件 {res['email']} ／ 企微 {res['wecom']} ／ 微信订阅 {res['wx_subscribe']}")
            st.caption("未配置的渠道显示 False 属正常（例如未填邮件密码、未配企微或微信 AppID 未申请）。")

    # ============ 食材来源拆解 ============
    st.divider()
    section("📊 食材来源拆解", icon="🔍")
    st.caption("同一种食材往往被多个菜品共用，下方按「食材 → 来源菜品」展开，说明每种食材的总量如何分摊。")
    if len(bd_df) > 0:
        st.dataframe(bd_df[["食材", "分类", "来源菜品", "分摊用量", "单位", "分摊成本(元)"]],
                     use_container_width=True, hide_index=True)
    else:
        st.info("暂无可拆解的配方数据。")
else:
    st.info("该门店预测暂无可聚合的配方数据，请先在「数据上传」中补充菜品用料表。")
