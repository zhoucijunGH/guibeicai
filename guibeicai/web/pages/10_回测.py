"""回测页面：评估预测准确度（MAE/MAPE/RMSE）并应用误差反馈校正。"""
import pandas as pd
import streamlit as st

from guibeicai.web._auth import require_auth, require_page_access
from guibeicai.web._db import init_db, get_accessible_stores, load_backtest_runs
from guibeicai.web._backtest import run_backtest, apply_and_save_bias_corrections
from guibeicai.web._audit import log_action, ACTION_BACKTEST_RUN, ACTION_BACKTEST_APPLY
from guibeicai.web._nav import render_nav
from guibeicai.web.components import page_header

init_db()

user = require_auth()
require_page_access("backtest")

st.set_page_config(page_title="预测回测", page_icon="📊", layout="wide")
render_nav()
page_header("预测回测与误差反馈", "评估 Prophet 预测准确度，应用偏差校正因子持续优化模型", icon="📊")

store_id = st.session_state.get("store_id") or 0
# 多租户加固：非管理员仅能选择其绑定的门店，避免越权查看其他租户数据
stores = get_accessible_stores(user, active_only=True)
store_options = {s["name"]: s["id"] for s in stores}
sel = st.selectbox("选择门店（回测范围）", ["全部门店"] + list(store_options.keys()), index=0)
target_store = None if sel == "全部门店" else store_options.get(sel)

c1, c2 = st.columns(2)
with c1:
    holdout = st.number_input("回测窗口(holdout 天)", min_value=1, max_value=30, value=7)
with c2:
    min_hist = st.number_input("最少历史天数", min_value=14, max_value=365, value=30)

if st.button("▶ 运行回测", type="primary"):
    with st.spinner("正在执行时间序列回测（Prophet 训练可能需数秒）…"):
        res = run_backtest(store_id=target_store, holdout_days=holdout, min_history_days=min_hist)
    # 操作审计：回测运行
    log_action(
        ACTION_BACKTEST_RUN,
        target_type="store",
        target_id=target_store if target_store is not None else "all",
        detail={
            "holdout_days": holdout,
            "min_history_days": min_hist,
            "n_dishes": res.get("overall", {}).get("n_dishes", 0),
            "mape": res.get("overall", {}).get("mape"),
            "store": sel,
        },
        user=user,
    )
    ov = res["overall"]
    st.subheader("整体指标")
    mc1, mc2, mc3, mc4, mc5 = st.columns(5)
    mc1.metric("平均绝对误差(MAE)", ov.get("mae"))
    mc2.metric("平均绝对百分比误差(%)", ov.get("mape"))
    mc3.metric("均方根误差(RMSE)", ov.get("rmse"))
    mc4.metric("平均偏差", f'{ov.get("bias")}')
    mc5.metric("覆盖菜品数", ov.get("n_dishes"))

    if res["per_dish"]:
        df = pd.DataFrame(res["per_dish"])[
            ["store_id", "dish_name", "n", "mae", "mape", "rmse", "bias", "pred_mean", "actual_mean"]
        ]
        df = df.rename(columns={
            "store_id": "门店ID",
            "dish_name": "菜品",
            "n": "样本数",
            "mae": "平均绝对误差",
            "mape": "平均绝对百分比误差(%)",
            "rmse": "均方根误差",
            "bias": "偏差",
            "pred_mean": "预测均值",
            "actual_mean": "实际均值",
        })
        st.subheader("逐菜品明细")
        st.dataframe(df, use_container_width=True, hide_index=True)

        if st.button("💾 应用偏差校正（写入下次预测）"):
            corr = apply_and_save_bias_corrections(target_store, res)
            # 操作审计：回测校正应用
            log_action(
                ACTION_BACKTEST_APPLY,
                target_type="store",
                target_id=target_store if target_store is not None else "all",
                detail={"n_corrections": len(corr), "store": sel, "factors": corr},
                user=user,
            )
            st.success(f"已写入 {len(corr)} 个菜品的偏差校正因子（限幅 0.5~1.5）。下次预测自动纠偏。")
            if corr:
                st.json(corr)
    else:
        st.info("暂无足够历史（≥%d+%d 天）的菜品可回测。" % (min_hist, holdout))

st.divider()
st.subheader("历史回测记录")
runs = load_backtest_runs(limit=20)
if runs:
    for r in runs:
        ov = r.get("summary", {}).get("overall", {})
        st.write(
            f"• #{r['id']} 门店={r['store_id']} 时间={r['run_time']} "
            f"holdout={r['holdout_days']} 菜品={r['summary'].get('n_dishes')} "
            f"MAPE={ov.get('mape')}% MAE={ov.get('mae')}"
        )
else:
    st.caption("尚无回测记录。")
