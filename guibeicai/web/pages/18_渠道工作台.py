"""渠道工作台：渠道商 / 配送商视角的跨店需求汇总

定位：使用对象是门店（门店做预测、生成采购单），付费方是渠道配送商。
本页面向付费方提供它真正购买的价值——「旗下门店明天一共要多少料」的跨店汇总视图，
以及各门店的使用活跃度（用于锁客与客户流失预警）。

只读设计：
- 本页不含任何预测执行 / 数据上传 / 配置变更入口；
- 数据范围统一走 get_accessible_stores（管理员看全部，渠道账号只看 user_stores 绑定的门店），
  杜绝越权查看其他渠道的门店数据。
"""

import streamlit as st
import pandas as pd
import plotly.express as px

from guibeicai.web._nav import render_nav
from guibeicai.web._db import (
    init_db,
    get_accessible_stores,
    get_prediction_stats_by_stores,
    get_store_supply_scope_norms,
    normalize_name,
)
from guibeicai.web.components import page_header, section, empty_state, metric_card, banner
from guibeicai.web._ingredient_forecast import (
    get_prediction_dates_by_store,
    build_purchase_order,
    aggregate_cross_store,
)

init_db()

from guibeicai.web._auth import require_auth, require_page_access

user = require_auth()
require_page_access("channel_overview")

st.set_page_config(page_title="渠道工作台", page_icon="🚚", layout="wide")
render_nav()


def _compute_scope_coverage(store_ids: list, predict_date: str, store_names: dict):
    """按各门店供货范围（store_supply_scope）拆分跨店需求为「已覆盖 / 未覆盖」。

    - 已覆盖：食材归一化名落在某门店供货范围内 → 渠道实际备货/配送。
    - 未覆盖：门店需求但渠道未勾选供货 → 需线下或 ERP 另行处理。
    返回 (covered, uncovered)，各为 {(norm, unit): {...聚合行}} 字典。
    """
    covered, uncovered = {}, {}
    for sid in store_ids:
        norms = get_store_supply_scope_norms(sid)
        po = build_purchase_order(sid, predict_date=predict_date)
        for it in po.get("items") or []:
            nn = normalize_name(it.get("ingredient_name") or "")
            key = (nn, it.get("unit") or "")
            tgt = covered if nn in norms else uncovered
            d = tgt.setdefault(key, {
                "ingredient_name": it.get("ingredient_name"), "unit": it.get("unit"),
                "quantity": 0.0, "cost": 0.0, "store_count": 0, "store_names": [],
            })
            d["quantity"] += float(it.get("quantity") or 0)
            d["cost"] += float(it.get("est_cost") or 0)
            nm = store_names.get(sid, f"门店#{sid}")
            if nm not in d["store_names"]:
                d["store_names"].append(nm)
                d["store_count"] = len(d["store_names"])
    return covered, uncovered

page_header("渠道工作台", "一次看清旗下所有门店的食材需求与使用情况，用于提前备货、降低损耗、提前发现沉默客户", icon="🚚")

# ==================== 门店范围（多租户隔离） ====================
stores = get_accessible_stores(user, active_only=True)
if not stores:
    empty_state("🏪", "暂无可查看的门店", "请联系管理员在「用户管理」中为您的账号绑定门店。")
    st.stop()

store_names = {s["id"]: s["name"] for s in stores}
store_ids = [s["id"] for s in stores]

# 门店活跃度统计（近 7 天预测次数 / 最近运行时间）
stats = get_prediction_stats_by_stores(store_ids)
active_7d = sum(1 for sid in store_ids if stats.get(sid, {}).get("count_7d", 0) > 0)

# ==================== 日期选择（各店预测日期并集） ====================
all_dates = sorted({d for sid in store_ids for d in get_prediction_dates_by_store(sid)}, reverse=True)
if not all_dates:
    empty_state("🔮", "旗下门店还没有预测结果", "门店生成预测后，这里会自动汇总跨店食材需求。")
    st.stop()

sel_date = st.selectbox("预测日期", all_dates, key="ch_date",
                        help="汇总口径：取各门店该日期的预测结果；某店若无该日期预测则不计入。")

agg = aggregate_cross_store(store_ids, predict_date=sel_date, store_names=store_names)
rows = agg["rows"]
per_store = agg["per_store"]

# ==================== 概览卡片 ====================
c1, c2, c3, c4 = st.columns(4)
with c1:
    metric_card("旗下门店", f"{len(stores)}", "您可查看的门店总数", variant="info")
with c2:
    metric_card("有预测门店", f"{agg['store_with_pred']}/{len(stores)}", f"{sel_date} 有预测结果的门店", variant="primary")
with c3:
    metric_card("近 7 日活跃", f"{active_7d}", "近 7 天生成过预测的门店数", variant="success")
with c4:
    metric_card("汇总采购金额", f"¥{agg['total_cost']:,.0f}", f"{sel_date} 各店合计（按配方单价估算）", variant="default")

if agg["store_with_pred"] == 0:
    empty_state("🧾", "该日期暂无可汇总的预测", "请换一个日期，或确认门店已配置菜品用料表。")
    st.stop()

banner("info", f"以下为旗下 {agg['store_with_pred']} 家门店在 **{sel_date}** 的食材需求汇总，"
               f"可直接导出后安排备货与配送。")

# 单位不统一提示：同名食材存在多种计量单位时分行显示，提醒推动门店统一单位
unit_variants = {}
for r in rows:
    unit_variants.setdefault(r["ingredient_name"], set()).add(r["unit"])
conflict_names = sorted([n for n, us in unit_variants.items() if len(us) > 1])
if conflict_names:
    st.warning(f"⚠️ 有 {len(conflict_names)} 种食材存在多种计量单位（如「{'、'.join(conflict_names[:3])}」），"
               f"已分行显示不做换算，避免采购量偏差。建议推动门店在「食材管理」中统一单位，汇总才会自动合并。")

st.divider()

# ==================== 跨店食材需求汇总（核心） ====================
section("跨店食材需求汇总", icon="🚚")
st.caption("同一种食材按「食材 + 单位」合并；单位不同（如斤 / kg）分行显示不做换算。")

kw = st.text_input("🔍 按食材名筛选", "", key="ch_kw", placeholder="输入食材关键字，如「鸡」")

df = pd.DataFrame([{
    "食材": r["ingredient_name"],
    "分类": r["category"],
    "合计数量": r["quantity"],
    "单位": r["unit"],
    "预估金额(元)": r["cost"],
    "覆盖门店数": r["store_count"],
    "来源门店": "、".join(r["store_names"][:3]) + (f" 等 {r['store_count']} 家" if r["store_count"] > 3 else ""),
    "单位不统一": "⚠️" if len(unit_variants.get(r["ingredient_name"], set())) > 1 else "",
} for r in rows])

if kw.strip():
    df = df[df["食材"].astype(str).str.contains(kw.strip(), case=False, na=False)]

st.dataframe(df, use_container_width=True, hide_index=True, height=420)

if len(df) > 0:
    top = df.head(15).copy()
    fig = px.bar(top, x="食材", y="预估金额(元)", title="Top 15 食材成本（跨店合计）",
                 color="预估金额(元)", color_continuous_scale="Reds")
    fig.update_layout(height=360, xaxis=dict(fixedrange=True), yaxis=dict(fixedrange=True),
                      margin=dict(l=10, r=10, t=40, b=10))
    st.plotly_chart(fig, use_container_width=True)

    csv_all = df.to_csv(index=False, encoding="utf-8-sig")
    st.download_button("📥 下载跨店汇总 CSV", csv_all,
                       file_name=f"渠道汇总_{sel_date}.csv", mime="text/csv")

st.divider()

# ==================== 门店活跃度 ====================
section("门店活跃度", icon="📊")
st.caption("近 7 天未生成预测的门店可能已沉默，建议及时跟进（客户流失预警）。")

act_rows = []
for s in stores:
    st_info = stats.get(s["id"], {})
    po = per_store.get(s["id"])
    cnt = int(st_info.get("count_7d", 0) or 0)
    act_rows.append({
        "门店": s["name"],
        "城市": f"{s.get('province') or ''}{s.get('city') or ''}".strip(),
        "状态": "🟢 活跃" if cnt > 0 else "⚪ 沉默",
        "近7天预测次数": cnt,
        "最新预测日期": st_info.get("last_date") or "—",
        "最近运行时间": st_info.get("last_run") or "—",
        "当日食材金额(元)": round(po["total_cost"], 2) if po else 0.0,
        "食材种类": po["item_count"] if po else 0,
    })
act_df = pd.DataFrame(act_rows).sort_values(["近7天预测次数", "门店"], ascending=[False, True])
st.dataframe(act_df, use_container_width=True, hide_index=True)
st.download_button("📥 下载门店活跃度 CSV", act_df.to_csv(index=False, encoding="utf-8-sig"),
                   file_name=f"门店活跃度_{sel_date}.csv", mime="text/csv")

silent = [r["门店"] for r in act_rows if r["状态"] != "🟢 活跃"]
if silent:
    st.warning(f"⚠️ 近 7 天无预测的门店 {len(silent)} 家：{'、'.join(silent[:8])}"
               + ("…" if len(silent) > 8 else "") + "，建议跟进确认使用情况。")

st.divider()

# ==================== 单店下钻 ====================
section("单店明细下钻", icon="🔍")
drill_options = {f"{s['name']}（¥{per_store[s['id']]['total_cost']:,.0f}）" if s["id"] in per_store
                 else f"{s['name']}（无预测）": s["id"] for s in stores}
drill_label = st.selectbox("选择门店查看明细", list(drill_options.keys()), key="ch_drill")
drill_id = drill_options[drill_label]

if drill_id not in per_store:
    st.info("该门店在所选日期没有预测结果，请换一个日期。")
else:
    po = build_purchase_order(drill_id, predict_date=sel_date)
    items = po.get("items") or []
    d1, d2, d3 = st.columns(3)
    with d1:
        metric_card("菜品数", f"{po['dish_count']}", "参与预测的菜品", variant="info")
    with d2:
        metric_card("食材种类", f"{len(items)} 种", "该店需求食材", variant="primary")
    with d3:
        metric_card("预估金额", f"¥{po['total_cost']:,.0f}", "按配方单价估算", variant="success")

    d_df = pd.DataFrame([{
        "食材": it["ingredient_name"], "分类": it["category"],
        "数量": it["quantity"], "单位": it["unit"], "预估金额(元)": it["est_cost"],
    } for it in items])
    st.dataframe(d_df, use_container_width=True, hide_index=True)
    st.download_button("📥 下载该店明细 CSV", d_df.to_csv(index=False, encoding="utf-8-sig"),
                       file_name=f"明细_{store_names.get(drill_id, drill_id)}_{sel_date}.csv", mime="text/csv")

st.divider()

# ==================== 渠道供货范围与备货（按 store_supply_scope 拆分已覆盖/未覆盖） ====================
section("渠道供货范围与备货", icon="🚚")
st.caption("依据各门店配置的「食材供货范围」拆分需求：已覆盖 = 渠道实际供货、可纳入备货/配送；"
           "未覆盖 = 门店需要但您未勾选供货的品类，需线下或 ERP 另行处理。")
st.info("供货范围在「渠道门店与供货」页按门店配置（勾选实际供货的食材，可手工补录待匹配项）。")

scope_covered, scope_uncovered = _compute_scope_coverage(store_ids, sel_date, store_names)

cov_cost = sum(d["cost"] for d in scope_covered.values())
unc_cost = sum(d["cost"] for d in scope_uncovered.values())
total_scope_cost = cov_cost + unc_cost
cov_rate = (cov_cost / total_scope_cost * 100) if total_scope_cost else 0.0

m1, m2, m3, m4 = st.columns(4)
with m1:
    metric_card("需求总金额", f"¥{total_scope_cost:,.0f}", f"{sel_date} 旗下门店合计", variant="default")
with m2:
    metric_card("已覆盖备货", f"¥{cov_cost:,.0f}", "渠道供货范围内，可纳入备货/配送", variant="success")
with m3:
    metric_card("未覆盖缺口", f"¥{unc_cost:,.0f}", "门店需、渠道未供货，需线下/ERP", variant="warning")
with m4:
    metric_card("供货覆盖率", f"{cov_rate:.0f}%", "已覆盖金额 / 需求总金额", variant="primary")

# 未覆盖缺口表（重点：渠道需处理的例外）
st.subheader("⚠️ 未覆盖需求（缺口）")
if scope_uncovered:
    unc_df = pd.DataFrame([{
        "食材": d["ingredient_name"], "单位": d["unit"],
        "缺口数量": round(d["quantity"], 2), "预估金额(元)": round(d["cost"], 2),
        "涉及门店数": d["store_count"],
        "来源门店": "、".join(d["store_names"][:3]) + (f" 等 {d['store_count']} 家" if d["store_count"] > 3 else ""),
    } for d in sorted(scope_uncovered.values(), key=lambda x: x["cost"], reverse=True)])
    st.dataframe(unc_df, use_container_width=True, hide_index=True, height=320)
    st.download_button("📥 下载未覆盖缺口 CSV", unc_df.to_csv(index=False, encoding="utf-8-sig"),
                       file_name=f"未覆盖缺口_{sel_date}.csv", mime="text/csv")
else:
    st.success("✅ 当前所有门店需求均在您的供货范围内，无未覆盖缺口。")

# 已覆盖备货明细（渠道实际需准备/配送）
with st.expander("✅ 已覆盖备货明细（渠道可配送）", expanded=False):
    if scope_covered:
        cov_df = pd.DataFrame([{
            "食材": d["ingredient_name"], "单位": d["unit"],
            "数量": round(d["quantity"], 2), "预估金额(元)": round(d["cost"], 2),
            "覆盖门店数": d["store_count"],
            "来源门店": "、".join(d["store_names"][:3]) + (f" 等 {d['store_count']} 家" if d["store_count"] > 3 else ""),
        } for d in sorted(scope_covered.values(), key=lambda x: x["cost"], reverse=True)])
        st.dataframe(cov_df, use_container_width=True, hide_index=True, height=320)
        st.download_button("📥 下载渠道备货单 CSV", cov_df.to_csv(index=False, encoding="utf-8-sig"),
                           file_name=f"渠道备货单_{sel_date}.csv", mime="text/csv")
    else:
        st.info("尚无已覆盖的备货项；请在「渠道门店与供货」页为门店勾选供货范围。")
