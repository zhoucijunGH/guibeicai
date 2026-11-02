import streamlit as st
from datetime import date, datetime, timedelta
import pandas as pd
import logging
import re
import threading
import queue as _queue
import time
from chinese_calendar import is_holiday

from guibeicai.web._nav import render_nav
from guibeicai.web._db import (
    init_db, save_prediction, load_all_history, check_prediction_exists,
    get_all_stores, get_dishes_by_store, get_store_by_id, get_user_store_ids,
    get_prediction_by_store_date, get_bias_corrections,
)
from guibeicai.web._cache import init_cache, get_cached_sales
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
from guibeicai.web._audit import log_action, ACTION_PREDICT_MANUAL
from guibeicai.web._auth import is_admin, require_auth, require_page_access

init_db()
init_cache()

user = require_auth()
require_page_access("predict_config")

from guibeicai.web._logging_setup import init_logging
init_logging()
logger = logging.getLogger("predict_page")

st.set_page_config(page_title="预测配置", page_icon="⚙️", layout="wide")
render_nav()
st.title("⚙️ 智能销量与采购预测")
st.caption("综合历史销量、天气、节假日、工作日等因素，AI辅助生成采购建议")

# ========== 品类映射函数 ==========
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

# ==================== 多租户：门店选择 ====================
db_stores = get_all_stores(active_only=True)
if not db_stores:
    st.error("❌ 没有可用的门店。"); st.stop()

if is_admin(user):
    store_options = {s["name"]: s["id"] for s in db_stores}
    store_name_sel = st.selectbox("选择门店", list(store_options.keys()))
    store_id = store_options[store_name_sel]
else:
    allowed_ids = set(get_user_store_ids(user.get("id", 0)))
    if not allowed_ids:
        st.error("❌ 您没有绑定任何门店，请联系管理员。"); st.stop()
    allowed_stores = [s for s in db_stores if s["id"] in allowed_ids]
    store_options = {s["name"]: s["id"] for s in allowed_stores}
    store_name_sel = st.selectbox("选择门店", list(store_options.keys()))
    store_id = store_options[store_name_sel]

store_city = next(s["city"] for s in db_stores if s["id"] == store_id)
store_lat, store_lon = city_to_coords(store_city)
st.caption(f"📍 {store_name_sel} → {store_city} (lat={store_lat:.2f}, lon={store_lon:.2f})")
# ==================== 门店选择结束 ====================

# ==================== 最近自动预测状态 ====================
tomorrow = date.today() + timedelta(days=1)
latest_pred = get_prediction_by_store_date(store_id, str(tomorrow))

if latest_pred:
    pred_revenue = latest_pred.get("revenue", 0)
    pred_cost = latest_pred.get("cost", 0)
    pred_margin = latest_pred.get("gross_margin", 0)
    pred_result = latest_pred.get("result", {})
    pred_source = pred_result.get("source", "manual") if isinstance(pred_result, dict) else "manual"

    if pred_source == "auto":
        st.info(
            f"🤖 最近一次自动预测：{tomorrow.strftime('%m-%d')} "
            f"｜ 营业额 ¥{pred_revenue:,.0f} ｜ 毛利率 {pred_margin:.1f}%"
        )
        st.caption("⚠️ 自动预测可能数据不完整（如昨日销量未录入），建议确认后手动重跑。")
    else:
        st.success(
            f"✅ 最近一次预测：{tomorrow.strftime('%m-%d')} "
            f"｜ 营业额 ¥{pred_revenue:,.0f} ｜ 毛利率 {pred_margin:.1f}%"
        )
else:
    st.warning(f"⚠️ 明日（{tomorrow.strftime('%m-%d')}）暂无预测记录，请手动执行预测。")
# ==================== 自动预测状态结束 ====================

sales_df = get_cached_sales(store_id)
if sales_df is None or len(sales_df) == 0:
    st.warning("⚠️ 该门店暂无历史销量数据，请先上传数据。")
    st.page_link("pages/1_数据上传.py", label="📤 去上传数据", use_container_width=True)
    st.stop()

if "sale_date" in sales_df.columns:
    sales_df["sale_date"] = pd.to_datetime(sales_df["sale_date"], errors="coerce")
    sales_df = sales_df.dropna(subset=["sale_date"])
else:
    st.error("❌ 历史销量数据中没有 sale_date 列，数据异常。"); st.stop()

for col in ["qty", "unit_price"]:
    if col in sales_df.columns:
        sales_df[col] = pd.to_numeric(sales_df[col], errors="coerce")

if "_history" not in st.session_state:
    st.session_state["_history"] = load_all_history()

st.markdown("---")
st.markdown("### 📅 预测日期配置")
st.caption(
    f"💡 您正在预测 **{tomorrow.strftime('%Y-%m-%d')}（明日）** 的销量，"
    f"用于指导当日采购。今天的销量请在「每日销量录入」中填写。"
)

# ---- 日期选择 ----
today = date.today()
c1, c2 = st.columns(2)
with c1:
    start_date = st.date_input(
        "开始日期",
        value=tomorrow,
        min_value=today,
        help="默认明日，不可选今天（今天已完成采购）",
    )
if isinstance(start_date, (list, tuple)):
    start_date = start_date[0]
with c2:
    end_date = st.date_input(
        "结束日期",
        value=tomorrow + timedelta(days=2),
        min_value=today,
        help="预测结束日期",
    )
if isinstance(end_date, (list, tuple)):
    end_date = end_date[0]

if end_date < start_date:
    st.error("结束日期不能早于开始日期")
    predict_dates = []
else:
    predict_dates = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]

manual_event = st.text_area("人工事件备注（可选）", placeholder="35℃高温、附近演唱会、门店促销8折...")

# ==================== 后台预测线程函数 ====================
def _run_prediction(result_queue, store_id, store_name_sel, store_city, predict_dates, manual_event, user):
    """后台线程：调用共享预测核心，结果通过 result_queue 返回（与小程序 API 共用同一逻辑）。"""
    try:
        from guibeicai.web._predict_core import run_prediction
        logger.info("[预测配置] 后台线程开始预测 store=%s dates=%s", store_name_sel, predict_dates)
        res = run_prediction(
            store_id, predict_dates, manual_event=manual_event,
            store_name=store_name_sel, store_city=store_city,
            progress_cb=lambda d, t, m: logger.info("[预测配置] 进度 %s/%s %s", d, t, m),
        )
        fin = res["predictions"][-1]["financial"] if res["predictions"] else {"revenue": 0, "gross_margin": 0}
        log_action(
            ACTION_PREDICT_MANUAL,
            target_type="store",
            target_id=store_id,
            detail={
                "store_name": store_name_sel,
                "dates": [str(d) for d in predict_dates],
                "dish_count": res["dish_count"],
                "revenue": fin["revenue"],
                "gross_margin": fin["gross_margin"],
            },
            user=user,
        )
        logger.info("[预测配置] 预测完成 store=%s", store_name_sel)
        result_queue.put({
            "status": "done",
            "result": res["predictions"][-1],
            "overall_risk": res["overall_risk"],
            "safety_rate": res["safety_rate"],
            "dish_count": res["dish_count"],
        })
    except Exception as e:
        logger.exception("[预测配置] 后台预测异常: %s", e)
        result_queue.put({"status": "error", "error": str(e)})

# ==================== 初始化 session_state ====================
if "_predict_running" not in st.session_state:
    st.session_state._predict_running = False
if "_predict_queue" not in st.session_state:
    st.session_state._predict_queue = None
if "_predict_thread" not in st.session_state:
    st.session_state._predict_thread = None

# ==================== 预测按钮 + 轮询 ====================
def _consume_predict_queue() -> bool:
    """从后台线程队列取一条结果。取到则更新会话状态并渲染结果，返回 True。"""
    q = st.session_state._predict_queue
    if q is None or q.empty():
        return False
    msg = q.get()
    st.session_state._predict_running = False
    st.session_state._predict_thread = None
    if msg["status"] == "done":
        st.session_state["_predict_triggered"] = True
        st.session_state["_predict_result"] = msg["result"]
        st.success(f"✅ 预测完成！营业额 ¥{msg['result']['financial']['revenue']:,.0f}，毛利率 {msg['result']['financial']['gross_margin']:.1f}%")
        col1, col2, col3 = st.columns(3)
        col1.metric("风险等级", msg["overall_risk"])
        col2.metric("菜品数", f"{msg['dish_count']}")
        col3.metric("安全库存加成", f"{msg['safety_rate'] * 100:+.0f}%")
        st.balloons()
    else:
        st.error(f"❌ 预测失败：{msg.get('error', '未知错误')}")
    return True


if st.session_state._predict_running:
    st.info("🔄 预测正在进行中（LLM 分析中），您可以切换到其他页面，预测会在后台继续完成...")

    # [C8] 用 st.fragment(run_every=2) 做非阻塞自动轮询，替代阻塞式 time.sleep(2)+rerun，
    # 避免页面每 2 秒被"冻住"一次、也不整页重跑（仅刷新本片段）。
    if hasattr(st, "fragment"):
        @st.fragment(run_every=2)
        def _poll_predict():
            if _consume_predict_queue():
                st.rerun(scope="app")  # 完成后整页刷新，切回"结果/按钮"布局
            else:
                st.caption("⏳ 结果生成后本页会自动刷新（每 2 秒检查一次）…")
        _poll_predict()
    else:  # 兼容无 fragment 的旧版 Streamlit：退化为短轮询
        if _consume_predict_queue():
            st.rerun()
        else:
            time.sleep(1)
            st.rerun()

else:
    if st.button("🚀 开始预测", type="primary"):
        if not predict_dates:
            st.error("请至少选择一个预测日期。")
        else:
            st.session_state._predict_running = True
            st.session_state._predict_queue = _queue.Queue()
            thread = threading.Thread(
                target=_run_prediction,
                args=(st.session_state._predict_queue, store_id, store_name_sel, store_city, predict_dates, manual_event, user),
                daemon=True,
            )
            thread.start()
            st.session_state._predict_thread = thread
            st.rerun()

# ==================== 底部展示区块 ====================
if st.session_state.get("_predict_triggered") and "_predict_result" in st.session_state:
    result = st.session_state["_predict_result"]
    fin = result["financial"]
    st.divider()
    st.subheader("📊 最新预测结果")
    c1, c2, c3 = st.columns(3)
    c1.metric("预测日期", fin["predict_date"])
    c2.metric("预估营业额", f"¥{fin['revenue']:,.2f}")
    c3.metric("毛利率", f"{fin['gross_margin']:.1f}%")
    st.markdown(f"**门店：** {fin['store']} ｜ **食材总成本：** ¥{fin['cost']:,.2f}")

    with st.expander("🛒 采购明细", expanded=True):
        if result["purchase_details"]:
            detail_df = pd.DataFrame(result["purchase_details"])
            rename_map = {
                "category": "品类", "ingredient_name": "食材", "base_qty": "基础采购量",
                "suggested_qty": "建议采购量", "unit_cost": "单位成本(元)", "total_cost": "总成本(元)",
                "related_dish": "关联菜品", "risk_level": "风险等级", "conclusion": "预测结论",
                "purchase_advice": "采购建议",
            }
            detail_df = detail_df.rename(columns=rename_map)
            if "采购建议" in detail_df.columns and "note" in detail_df.columns:
                detail_df["采购建议"] = detail_df.apply(
                    lambda r: f"{r['采购建议']}（原因：{r['note']}）" if pd.notna(r.get("note")) and str(r["note"]).strip() else r["采购建议"],
                    axis=1,
                )
            display_cols = ["品类", "食材", "基础采购量", "建议采购量", "单位成本(元)", "总成本(元)", "关联菜品", "风险等级", "预测结论", "采购建议"]
            display_cols = [c for c in display_cols if c in detail_df.columns]
            st.dataframe(detail_df[display_cols], use_container_width=True, hide_index=True)
        else:
            st.info("无采购明细")

    if result["purchase_details"]:
        download_df = pd.DataFrame(result["purchase_details"]).rename(columns=rename_map)
        show_cols = [c for c in ["品类", "食材", "基础采购量", "建议采购量", "单位成本(元)", "总成本(元)", "关联菜品", "风险等级", "预测结论", "采购建议"] if c in download_df.columns]
        csv_bytes = download_df[show_cols].to_csv(index=False, encoding="utf-8-sig")
        # st.download_button("📥 下载采购明细 CSV", csv_bytes, f"采购明细_{fin['store']}_{fin['predict_date']}.csv", "text/csv")