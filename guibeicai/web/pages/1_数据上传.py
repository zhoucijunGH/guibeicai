import streamlit as st
import pandas as pd
from io import BytesIO
from datetime import datetime, date, timedelta

from guibeicai.web._nav import render_nav
from guibeicai.web._db import (
    init_db, upsert_store, upsert_dish, upsert_dish_ex,
    upsert_sales_history, check_prediction_exists, get_all_stores,
    get_dishes_by_store, get_store_by_id, get_user_store_ids,
    upsert_actual_sales, get_actual_sales_by_store_date, get_actual_sales_full_by_store_date,
    get_latest_actual_qty,
    get_prediction_by_store_date, get_dish_by_id, get_latest_sales_history_qty
)
from guibeicai.web._cache import init_cache, refresh_sales_cache

init_db()
init_cache()

from guibeicai.web._auth import require_auth, require_page_access, is_admin

user = require_auth()
require_page_access("data_upload")

st.set_page_config(page_title="数据上传", page_icon="📤", layout="wide")
render_nav()

st.title("📤 数据上传")

# ========== 列名归一：中文/别名 → 英文 ==========
def _normalize_to_english(df):
    df = df.copy()
    rename_map = {}
    for c in df.columns:
        k = str(c).strip()
        if k in ["日期", "销售日期", "下单日期", "营业日期", "日期时间", "date", "Date", "DATE", "时间", "销售时间"]:
            rename_map[c] = "sale_date"
        elif k in ["门店", "店铺", "store", "Store", "STORE", "门店名称"]:
            rename_map[c] = "store"
        elif k in ["菜品", "菜名", "dish", "Dish", "商品", "产品"]:
            rename_map[c] = "dish"
        elif k in ["销量", "销售数量", "数量", "qty", "Qty", "quantity"]:
            rename_map[c] = "qty"
        elif k in ["单价", "价格", "price", "Price", "unit_price"]:
            rename_map[c] = "unit_price"
        elif k in ["城市", "city", "City", "所在城市"]:
            rename_map[c] = "city"
        elif k in ["损耗量", "损耗", "waste", "Waste", "waste_qty"]:
            rename_map[c] = "waste"
    df = df.rename(columns=rename_map)
    return df

_DISPLAY_COLUMN_MAP = {
    "store": "门店", "city": "城市", "sale_date": "销售日期",
    "dish": "菜品", "qty": "销量", "unit_price": "单价",
}

def _display_chinese(df):
    df = df.copy()
    df.columns = [_DISPLAY_COLUMN_MAP.get(c, c) for c in df.columns]
    return df


# ==================== Tab 结构 ====================
tab1, tab2 = st.tabs(["📤 批量导入", "📝 每日销量录入"])

# ==================== Tab 1：批量导入（原有功能完整保留） ====================
with tab1:
    st.subheader("1. 上传数据")

    # 将 Streamlit 原生文件上传按钮的英文 "Upload" 替换为中文
    st.markdown(
        """
        <style>
        [data-testid="stFileUploader"] button {
            position: relative;
        }
        [data-testid="stFileUploader"] button span {
            display: none !important;
        }
        [data-testid="stFileUploader"] button::after {
            content: "选择文件";
            display: inline;
            font-size: inherit;
            font-weight: inherit;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )

    upload_done = st.session_state.get("_upload_done", False)

    if upload_done:
        st.success("✅ 数据已加载（本次会话缓存中）")
        if st.button("🔄 重新上传", key="reupload_btn"):
            for k in ["_sales_df", "_bom_df", "_upload_done", "_upload_compare_hints"]:
                st.session_state.pop(k, None)
            st.rerun()

    combined_file = st.file_uploader(
        "上传数据文件（Excel，只需「销售数据」一个 Sheet，食材配方由系统自动生成）",
        type=["xlsx", "xls"], key="combined_upload", disabled=upload_done,
    )

    if st.button("📤 确认上传", type="primary", disabled=upload_done, key="confirm_upload_btn"):
        if combined_file is None:
            st.error("❌ 请上传数据文件。"); st.stop()

        try:
            sales_df = pd.read_excel(combined_file, sheet_name="销售数据")
        except ValueError:
            st.error("❌ Excel 文件中必须包含名为「销售数据」的 Sheet，请检查。"); st.stop()
        except Exception as e:
            st.error(f"❌ 文件读取失败：{e}"); st.stop()

        sales_df = _normalize_to_english(sales_df)

        # ---- 校验 ----
        required_sales_cols = ["store", "city", "sale_date", "dish", "qty", "unit_price"]
        missing_sales = [c for c in required_sales_cols if c not in sales_df.columns]
        if missing_sales:
            st.error(f"❌ 销售数据 Sheet 缺少以下列：{missing_sales}。当前列名：{list(sales_df.columns)}"); st.stop()
        if sales_df["city"].isna().any():
            st.error("❌ 销售数据中「city」列存在空值，请补全。"); st.stop()
        try:
            sales_df["sale_date"] = pd.to_datetime(sales_df["sale_date"], errors="coerce")
        except Exception:
            st.error("❌ 销售数据「sale_date」列格式错误，请使用 YYYY-MM-DD。"); st.stop()
        sales_df = sales_df.dropna(subset=["sale_date"])
        if (sales_df["qty"] < 0).any():
            st.error("❌ 销售数据「qty」不能为负数。"); st.stop()
        if (sales_df["unit_price"] < 0).any():
            st.error("❌ 销售数据「unit_price」不能为负数。"); st.stop()

        # ========== 同步到 DB（多租户权限过滤） ==========
        all_stores = get_all_stores(active_only=False)

        if is_admin(user):
            sys_store_map = {s["name"].strip(): s["id"] for s in all_stores}
            store_id_map = {}
            unmatched_stores = []

            for excel_store in sales_df["store"].dropna().astype(str).str.strip().unique():
                if excel_store in sys_store_map:
                    store_id_map[excel_store] = sys_store_map[excel_store]
                else:
                    unmatched_stores.append(excel_store)

            if unmatched_stores:
                st.error(
                    f"❌ 以下门店在系统中不存在，请先在「门店管理」中创建：\n\n"
                    f"{chr(10).join('  - ' + s for s in unmatched_stores)}\n\n"
                    f"系统已有门店：{list(sys_store_map.keys())}"
                )
                st.stop()

            sys_city_map = {s["name"].strip(): s["city"] for s in all_stores}
            sales_df["city"] = sales_df["store"].map(lambda s: sys_city_map.get(str(s).strip(), ""))

        else:
            allowed_ids = set(get_user_store_ids(user.get("id", 0)))
            if not allowed_ids:
                st.error("❌ 您没有绑定任何门店，请联系管理员。"); st.stop()

            allowed_stores = [s for s in all_stores if s["id"] in allowed_ids]
            allowed_store_map = {s["name"].strip(): s["id"] for s in allowed_stores}
            allowed_store_name_set = set(allowed_store_map.keys())

            excel_stores = sales_df["store"].dropna().astype(str).str.strip().unique()
            unmatched_stores = [s for s in excel_stores if s not in allowed_store_name_set]

            if unmatched_stores:
                st.error(
                    f"❌ 以下门店不在您的权限范围内，无法上传：\n\n"
                    f"{chr(10).join('  - ' + s for s in unmatched_stores)}\n\n"
                    f"您有权限的门店：{list(allowed_store_map.keys())}"
                )
                st.stop()

            store_id_map = allowed_store_map.copy()
            sys_city_map = {s["name"].strip(): s["city"] for s in allowed_stores}
            sales_df["city"] = sales_df["store"].map(lambda s: sys_city_map.get(str(s).strip(), ""))
        # ========== 同步到 DB 结束 ==========

        # new_dishes: {dish_id: (store_id, dish_name)} —— 仅新入库的菜才需要调大模型生成配方
        new_dishes = {}
        dish_stat = {"total": 0, "new": 0, "exist": 0}

        for store_name in sales_df["store"].unique():
            store_id = store_id_map[store_name]
            store_dishes = sales_df[sales_df["store"] == store_name]["dish"].dropna().astype(str).str.strip().unique()
            for dish_name in store_dishes:
                dish_data = sales_df[(sales_df["store"] == store_name) & (sales_df["dish"] == dish_name)]
                price = dish_data["unit_price"].iloc[-1] if len(dish_data) else 0
                did, is_new = upsert_dish_ex(store_id, dish_name, standard_price=price)
                dish_stat["total"] += 1
                if is_new:
                    dish_stat["new"] += 1
                    new_dishes[did] = (store_id, dish_name)
                else:
                    dish_stat["exist"] += 1

        compare_hints = []
        affected_dates_by_store = {}

        for store_name, sdf in sales_df.groupby("store"):
            store_id = store_id_map[store_name]
            if store_id not in affected_dates_by_store:
                affected_dates_by_store[store_id] = set()

            for _, row in sdf.iterrows():
                dish_name = str(row["dish"]).strip()
                dishes_in_db = get_dishes_by_store(store_id, active_only=False)
                dish_id = next((d["id"] for d in dishes_in_db if d["name"] == dish_name), None)
                if dish_id is None:
                    continue
                sale_date_str = row["sale_date"].strftime("%Y-%m-%d")
                upsert_sales_history(store_id, dish_id, sale_date_str, float(row["qty"]), float(row["unit_price"]))
                # 同步写实际销量/损耗表（如有损耗量列则带入，无则默认 0）
                waste_val = float(row["waste"]) if "waste" in row and pd.notna(row["waste"]) else 0.0
                upsert_actual_sales(store_id, dish_id, sale_date_str, actual_qty=float(row["qty"]), actual_waste=waste_val)
                affected_dates_by_store[store_id].add(sale_date_str)

        for store_id in store_id_map.values():
            refresh_sales_cache(store_id)

        # ========== 新菜品：增量调用大模型自动生成配方（每份食材/用量/市场估价） ==========
        # 复用独立重生成引擎：小批请求 -> 立即落库 -> 下一批，天然可恢复。
        # 新门店导入大批菜品时，即使中途异常也不会丢失已落库的批次（断点续跑）。
        bom_stat = {"ok": 0, "failed": 0}
        if new_dishes:
            from guibeicai.web._bom_regen import regen_bom

            names = sorted({nm for _, nm in new_dishes.values()})
            prog = st.progress(0.0, text=f"🤖 正在为 {len(names)} 道新菜品生成配方（食材/用量/估价）…")

            def _on_progress(done, total):
                try:
                    prog.progress(min(done / max(total, 1), 1.0), text=f"🤖 已生成 {done}/{total} 道菜的配方…")
                except Exception:
                    pass

            # 按门店分别增量重生成（仅处理该门店 pending 菜品，含本次新导入）
            for sid in sorted({sid for sid, _ in new_dishes.values()}):
                res = regen_bom(store_id=sid, batch=6, max_workers=2, progress_cb=_on_progress)
                if res:
                    bom_stat["ok"] += res["ok"]
                    bom_stat["failed"] += res["failed"]
            prog.empty()

        for store_id, dates in affected_dates_by_store.items():
            for d in dates:
                if check_prediction_exists(store_id, d):
                    store_name = next(k for k, v in store_id_map.items() if v == store_id)
                    compare_hints.append(f"{store_name} · {d}")

        st.session_state["_sales_df"] = sales_df
        st.session_state["_bom_df"] = pd.DataFrame()
        st.session_state["_upload_done"] = True
        st.session_state["_upload_compare_hints"] = compare_hints
        st.session_state["_dish_stat"] = dish_stat
        st.session_state["_bom_stat"] = bom_stat

        st.success(
            f"✅ 上传成功！销售数据 {len(sales_df)} 行已持久化（重复数据已更新）。\n\n"
            f"🍳 菜品：共 {dish_stat['total']} 道，新入库 {dish_stat['new']} 道，已存在跳过 {dish_stat['exist']} 道。"
        )
        if new_dishes:
            if bom_stat["failed"] == 0:
                st.success(f"🤖 已为 {bom_stat['ok']} 道新菜品自动生成配方（食材/用量/市场估价）。")
            else:
                st.warning(
                    f"🤖 自动生成配方完成 {bom_stat['ok']} 道，失败 {bom_stat['failed']} 道（已标记为待生成，"
                    f"可到「菜品管理」页手工补录或重试）。"
                )
        else:
            st.caption("💡 本次上传未发现新菜品，无需重新生成配方。")

        if compare_hints:
            st.warning("⚠️ 检测到以下门店+日期已有预测记录，上传后数据已更新：")
            for h in compare_hints:
                st.write(f"  - {h}")
            st.info("👉 建议前往「预测对比」页面查看上传销量与预测的偏差。")
            st.page_link("pages/9_预测对比.py", label="📊 去查看预测对比", use_container_width=False)

        st.info("👉 可以去「执行预测」页面开始预测了。")

        st.divider()
        st.subheader("📋 数据预览")
        st.dataframe(_display_chinese(sales_df.head(200)), use_container_width=True)

    elif upload_done:
        sales_df = st.session_state.get("_sales_df")
        st.divider()
        st.subheader("📋 数据预览")
        if sales_df is not None:
            st.dataframe(_display_chinese(sales_df.head(200)), use_container_width=True)

    # ========== 下载模板 ==========
    st.divider()
    st.subheader("2. 下载模板")
    st.caption("💡 只需填写销售数据，食材配方由系统在导入后自动调用大模型生成，无需手工录入。")

    sales_template = pd.DataFrame({
        "门店": ["南宁演示店", "南宁演示店"],
        "城市": ["南宁市", "南宁市"],
        "销售日期": ["2026-08-01", "2026-08-02"],
        "产品": ["老友粉", "柠檬鸭"],
        "销量": [120, 60],
        "损耗量": [5, 2],
        "单价": [15, 48],
    })

    st.dataframe(sales_template, use_container_width=True, hide_index=True)

    combined_buf = BytesIO()
    with pd.ExcelWriter(combined_buf, engine="openpyxl") as writer:
        sales_template.to_excel(writer, sheet_name="销售数据", index=False)

    st.download_button(
        label="📥 下载数据模板（仅需销售数据）",
        data=combined_buf.getvalue(),
        file_name="桂备菜_销售数据模板.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

# ==================== Tab 2：每日销量录入 ====================
with tab2:
    st.subheader("📝 每日销量录入")
    st.caption("💡 录入实际销量，用于与预测结果对比验证模型准确度。默认值优先级：已录入 > 预测结果 > 历史最近一次销量。")

    # ---- 门店选择 ----
    all_stores = get_all_stores(active_only=True)

    if is_admin(user):
        store_options = {s["name"]: s["id"] for s in all_stores}
    else:
        allowed_ids = set(get_user_store_ids(user.get("id", 0)))
        store_options = {s["name"]: s["id"] for s in all_stores if s["id"] in allowed_ids}

    if not store_options:
        st.error("❌ 没有可用门店，请先创建门店并绑定权限。")
        st.stop()

    selected_store_name = st.selectbox("选择门店", options=list(store_options.keys()), key="daily_store")
    selected_store_id = store_options[selected_store_name]

    # ---- 日期选择：默认昨日，只允许过去 7 天 ----
    yesterday = date.today() - timedelta(days=1)
    min_date = date.today() - timedelta(days=7)

    selected_date = st.date_input(
        "录入日期",
        value=yesterday,
        min_value=min_date,
        max_value=yesterday,
        format="YYYY-MM-DD",
        key="daily_date",
        help="默认昨日，允许补录过去 7 天",
    )
    sale_date_str = selected_date.strftime("%Y-%m-%d")
    st.caption(f"📅 当前录入：{sale_date_str}（T-1 实际销量）")

    # ---- 加载该门店所有活跃菜品 ----
    dishes = get_dishes_by_store(selected_store_id, active_only=True)
    if not dishes:
        st.warning("⚠️ 该门店暂无活跃菜品，请先在批量导入中上传数据。")
        st.stop()

    # ---- 三级 fallback 加载销量初始值 ----
    def load_initial_qty(store_id, dish_id, target_date):
        """
        优先级：
        1. actual_sales 已录入
        2. predictions 预测结果（从 result_json 里取 dish_predictions）
        3. sales_history 最近一次销量
        4. 0
        """
        # 1. actual_sales
        existing_map = get_actual_sales_by_store_date(store_id, target_date)
        if dish_id in existing_map:
            return float(existing_map[dish_id])

        # 2. 预测结果
        pred_record = get_prediction_by_store_date(store_id, target_date)
        if pred_record and pred_record.get("result"):
            dish_preds = pred_record["result"].get("dish_predictions", {})
            dish_info = get_dish_by_id(dish_id)
            if dish_info and dish_info["name"] in dish_preds:
                return float(dish_preds[dish_info["name"]])

        # 3. sales_history 最近一次
        latest = get_latest_sales_history_qty(store_id, dish_id)
        if latest > 0:
            return float(latest)

        # 4. 都没有
        return 0.0

    # ---- 加载损耗初始值 ----
    def load_initial_waste(store_id, dish_id, target_date):
        """从 actual_sales 读取已录入损耗，无则返回 0。"""
        existing_full = get_actual_sales_full_by_store_date(store_id, target_date)
        info = existing_full.get(dish_id)
        if info:
            return float(info.get("waste") or 0)
        return 0.0

    # ---- 来源提示 ----
    existing_sales = get_actual_sales_by_store_date(selected_store_id, sale_date_str)
    pred_record = get_prediction_by_store_date(selected_store_id, sale_date_str)

    if existing_sales:
        st.info("📌 已录入过实际销量与损耗，当前展示已保存的数据。")
    elif pred_record:
        st.info("🔮 暂无实际销量，已用预测结果填充默认值，确认后点保存。")
    else:
        st.info("📋 暂无预测结果，已用历史最近销量填充默认值，确认后点保存。")

    st.divider()
    st.markdown(f"**{selected_store_name} · {sale_date_str}** 销量与损耗录入（共 {len(dishes)} 个菜品）")

    # ---- 批量操作 callback ----
    def zero_all():
        for d in dishes:
            st.session_state[f"qty_{d['id']}"] = 0.0

    def clear_all():
        for d in dishes:
            st.session_state.pop(f"qty_{d['id']}", None)

    # ---- 批量操作按钮 ----
    col_all_zero, col_clear_all = st.columns(2)
    with col_all_zero:
        st.button("🔘 全部填 0", key="fill_zero_btn", use_container_width=True, on_click=zero_all)
    with col_clear_all:
        st.button("🧹 清空全部", key="clear_all_btn", use_container_width=True, on_click=clear_all)

    st.divider()

    # ---- 逐菜品录入 ----
    for dish in dishes:
        dish_id = dish["id"]
        dish_name = dish["name"]
        category = dish.get("category", "")

        default_val = load_initial_qty(selected_store_id, dish_id, sale_date_str)
        default_waste = load_initial_waste(selected_store_id, dish_id, sale_date_str)

        # 在 widget 创建前初始化 session_state，不覆盖已有值
        st.session_state.setdefault(f"qty_{dish_id}", float(default_val))
        st.session_state.setdefault(f"waste_{dish_id}", float(default_waste))

        col_a, col_b, col_c = st.columns([3, 2, 2])
        with col_a:
            st.markdown(f"**{dish_name}**")
            if category:
                st.caption(category)
        with col_b:
            st.number_input(
                label="销量",
                min_value=0.0,
                value=float(st.session_state[f"qty_{dish_id}"]),
                step=1.0,
                key=f"qty_{dish_id}",
                label_visibility="collapsed",
                help="当日实际售出份数",
            )
            st.caption("销量")
        with col_c:
            st.number_input(
                label="损耗量",
                min_value=0.0,
                value=float(st.session_state[f"waste_{dish_id}"]),
                step=1.0,
                key=f"waste_{dish_id}",
                label_visibility="collapsed",
                help="当日未售出/报损份数，用于损耗分析",
            )
            st.caption("损耗量")

    st.divider()

    # ---- 提交 ----
    col_submit, col_jump = st.columns(2)
    with col_submit:
        if st.button("💾 保存销量与损耗", type="primary", use_container_width=True, key="save_daily_btn"):
            saved_count = 0
            for dish in dishes:
                dish_id = dish["id"]
                qty = st.session_state.get(f"qty_{dish_id}", 0.0)
                waste = st.session_state.get(f"waste_{dish_id}", 0.0)
                try:
                    upsert_actual_sales(
                        store_id=selected_store_id,
                        dish_id=dish_id,
                        sale_date=sale_date_str,
                        actual_qty=float(qty),
                        actual_waste=float(waste),
                    )
                    saved_count += 1
                except Exception as e:
                    st.error(f"❌ {dish['name']} 保存失败：{e}")

            if saved_count > 0:
                st.success(f"✅ 已保存 {selected_store_name} · {sale_date_str} 的 {saved_count} 个菜品实际销量与损耗（覆盖写入）。")
                st.info("👉 可前往「预测对比」查看预测 vs 实际的偏差，或前往「损耗分析」查看损耗排行。")
                st.page_link("pages/9_预测对比.py", label="📊 去查看预测对比", use_container_width=False)
                st.page_link("pages/16_损耗分析.py", label="🗑️ 去查看损耗分析", use_container_width=False)

    with col_jump:
        st.page_link("pages/9_预测对比.py", label="📊 前往预测对比", use_container_width=True)