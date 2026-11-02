"""智能预警页面：汇总加备/减备/节假日/偏差类预警，支持标记已处理"""

import streamlit as st
from datetime import date

from guibeicai.web._nav import render_nav
from guibeicai.web._db import init_db, get_all_stores, get_user_store_ids, get_store_push_targets
from guibeicai.web.components import page_header, section, empty_state, metric_card, banner, risk_badge
from guibeicai.web._alerts import generate_alerts
from guibeicai.scheduler.notifiers.store_alert_notifier import StoreAlertNotifier

init_db()

from guibeicai.web._auth import require_auth, require_page_access, is_admin

user = require_auth()
require_page_access("alert")

st.set_page_config(page_title="智能预警", page_icon="🔔", layout="wide")
render_nav()

page_header("智能预警", "主动发现加备/减备/节假日/偏差风险，把问题挡在备货前", icon="🔔")

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

sel_name = st.selectbox("选择门店", list(store_options.keys()), key="alert_store")
store_id = store_options[sel_name]

if st.button("🔔 刷新预警", type="primary", use_container_width=True, key="refresh_alert"):
    st.session_state["alerts"] = generate_alerts(store_id)
    st.session_state["alert_store_name"] = sel_name

alerts = st.session_state.get("alerts")
if not alerts:
    banner("info", "点击上方「刷新预警」按钮，系统将基于最近一次预测与历史数据生成预警清单。")
    st.stop()

sel_name = st.session_state.get("alert_store_name", sel_name)
high = sum(1 for a in alerts if a["severity"] == "高")
mid = sum(1 for a in alerts if a["severity"] == "中")

c1, c2, c3 = st.columns(3)
with c1:
    metric_card("预警总数", f"{len(alerts)}", f"门店 {sel_name}", variant="info")
with c2:
    metric_card("高风险", f"{high}", "需优先处理", variant="primary")
with c3:
    metric_card("中风险", f"{mid}", "建议关注", variant="warning")

if st.button("🚀 立即推送（邮件 / 企业微信）", use_container_width=True, key="push_alert"):
    targets = get_store_push_targets(store_id)
    if not (targets.get("emails") or targets.get("wecom_webhook")):
        st.warning("⚠️ 该门店未配置「老板邮箱」或「企业微信 Webhook」，无法推送。请到门店管理填写。")
    elif not alerts:
        st.warning("当前没有预警可推送。")
    else:
        try:
            StoreAlertNotifier().send_store_alert(sel_name, alerts, targets)
            st.success(f"✅ 已将 {len(alerts)} 条预警推送给该门店配置的接收渠道")
        except Exception as e:
            st.error(f"❌ 推送失败：{e}")

handled = st.session_state.get("_alerts_handled", set())

st.divider()
section("预警清单", icon="📋")

if not alerts:
    st.success("🎉 当前没有触发预警，备货状态健康。")
else:
    for i, a in enumerate(alerts):
        key = f"{sel_name}_{a['type']}_{i}"
        if key in handled:
            continue
        col_main, col_btn = st.columns([5, 1])
        with col_main:
            st.markdown(f"""
            <div style="border-left:4px solid {'var(--primary)' if a['severity']=='高' else 'var(--warning)'};
                        background:var(--surface);border-radius:10px;padding:12px 14px;margin:8px 0;box-shadow:var(--shadow);">
                <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
                    {risk_badge(a['severity'])}
                    <span style="font-weight:700;color:var(--text);">{a['title']}</span>
                </div>
                <div style="color:var(--text-soft);font-size:14px;">{a['detail']}</div>
            </div>
            """, unsafe_allow_html=True)
        with col_btn:
            if st.button("标记已处理", key=f"handle_{i}"):
                handled.add(key)
                st.session_state["_alerts_handled"] = handled
                st.rerun()
