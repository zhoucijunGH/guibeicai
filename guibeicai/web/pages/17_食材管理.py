"""食材管理：维护食材主数据，核心是管理采购单价（按门店独立）。

数据来源：菜品导入后由大模型自动生成配方时同步归位，也可在此手工新增。
改价后会联动重算所有引用该食材的菜品成本。
"""

import streamlit as st
import pandas as pd

from guibeicai.web._nav import render_nav
from guibeicai.web._db import (
    init_db, get_all_stores, get_user_store_ids, get_conn,
    get_ingredients_by_store, get_ingredient_by_id, add_ingredient, update_ingredient,
    delete_ingredient, update_ingredient_status, get_ingredient_usage_map,
    recompute_costs_for_ingredient, recompute_all_costs,
)
from guibeicai.web.components import page_header, section, empty_state, banner, arm_danger, render_danger_confirm

init_db()

from guibeicai.web._auth import require_auth, require_page_access, is_admin

user = require_auth()
require_page_access("ingredient_manage")

st.set_page_config(page_title="食材管理", page_icon="🥬", layout="wide")
render_nav()

page_header("食材管理", "维护食材采购单价，改价自动联动重算菜品成本", icon="🥬")

# ==================== 门店选择（多租户隔离） ====================
if is_admin(user):
    stores = get_all_stores(active_only=True)
    store_options = {s["name"]: s["id"] for s in stores}
else:
    allowed_ids = set(get_user_store_ids(user.get("id", 0)))
    stores = [s for s in get_all_stores(active_only=True) if s["id"] in allowed_ids]
    store_options = {s["name"]: s["id"] for s in stores}

if not store_options:
    empty_state("🏪", "暂无可用门店", "请先创建门店并上传数据。",
                action_label="🏪 去门店管理", action_target="pages/4_门店管理.py")
    st.stop()

sel_name = st.selectbox("选择门店", list(store_options.keys()), key="ing_store")
store_id = store_options[sel_name]

# ==================== 统计概览 ====================
ings_all = get_ingredients_by_store(store_id, active_only=False)
ings = [i for i in ings_all if i["status"] == "active"]

if not ings_all:
    empty_state("🥬", "该门店暂无食材", "上传销售数据后，系统会自动调用大模型生成菜品配方并归位食材。",
                action_label="📤 去上传数据", action_target="pages/1_数据上传.py")
    st.stop()

c1, c2, c3 = st.columns(3)
with c1:
    st.metric("食材种类", f"{len(ings)}")
with c2:
    avg_price = sum(i["unit_price"] or 0 for i in ings) / max(len(ings), 1)
    st.metric("平均单价", f"¥{avg_price:,.2f}")
with c3:
    src_llm = sum(1 for i in ings if i["source"] == "llm")
    st.metric("大模型估价", f"{src_llm} 种")

st.caption("💡 单价口径：元 / 单位（如 元/kg、元/个）。人工改过的价（来源=manual）不会被大模型覆盖。")

# ==================== 新增食材 ====================
with st.expander("➕ 新增食材", expanded=False):
    with st.form("add_ingredient_form", clear_on_submit=True):
        n1, n2, n3, n4 = st.columns([3, 2, 2, 2])
        with n1:
            new_name = st.text_input("食材名称", placeholder="如：鸡胸肉")
        with n2:
            new_cat = st.selectbox("品类", ["蔬菜", "肉类", "禽蛋", "水产", "豆制品",
                                            "菌菇", "米面", "调料", "粮油", "干货", "其他"])
        with n3:
            new_unit = st.selectbox("单位", ["kg", "g", "L", "ml", "个", "只", "片", "份"])
        with n4:
            new_price = st.number_input("采购单价(元)", min_value=0.0, step=0.5, value=0.0)
        submitted = st.form_submit_button("💾 保存", type="primary")
        if submitted:
            if not new_name.strip():
                st.error("❌ 请填写食材名称。")
            else:
                try:
                    add_ingredient(store_id, new_name.strip(), category=new_cat,
                                   base_unit=new_unit, unit_price=new_price, source="manual")
                    st.success(f"✅ 已新增食材「{new_name.strip()}」")
                    st.rerun()
                except ValueError as e:
                    st.error(f"❌ {e}")

st.divider()

# ==================== 筛选 ====================
f1, f2, f3 = st.columns([3, 2, 2])
with f1:
    kw = st.text_input("🔍 搜索食材名", key="ing_kw", placeholder="输入关键字过滤")
with f2:
    cat_filter = st.selectbox("品类筛选", ["全部"] + sorted({i["category"] or "其他" for i in ings_all}), key="ing_cat")
with f3:
    show_inactive = st.checkbox("显示已停用", value=False, key="ing_show_inactive")

view = [i for i in ings_all if show_inactive or i["status"] == "active"]
if kw.strip():
    view = [i for i in view if kw.strip() in (i["display_name"] or i["name"])]
if cat_filter != "全部":
    view = [i for i in view if (i["category"] or "其他") == cat_filter]

if not view:
    banner("info", "没有符合条件的食材。")
    st.stop()

section(f"食材列表 · {sel_name}（{len(view)} 种）", icon="🥬")

# ==================== 列表 + 行内编辑 ====================
header_cols = st.columns([3, 1.6, 1.2, 1.8, 1.2, 1.6, 2.4])
for col, txt in zip(header_cols, ["食材", "品类", "单位", "采购单价(元)", "来源", "引用菜品", "操作"]):
    col.caption(f"**{txt}**")

SOURCE_LABEL = {"llm": "🤖 大模型", "import": "📥 导入", "manual": "✍️ 人工"}

# [C4 性能] 一次性取全店食材引用明细，避免列表里逐个食材查询（N+1）。
usage_map = get_ingredient_usage_map(store_id)

for ing in view:
    usage = usage_map.get(ing["id"], [])
    c_name, c_cat, c_unit, c_price, c_src, c_use, c_ops = st.columns([3, 1.6, 1.2, 1.8, 1.2, 1.6, 2.4])

    with c_name:
        st.write(("~~" + (ing["display_name"] or ing["name"]) + "~~") if ing["status"] != "active"
                 else f"**{ing['display_name'] or ing['name']}**")
    with c_cat:
        st.write(ing["category"] or "-")
    with c_unit:
        st.write(ing["base_unit"] or "-")
    with c_price:
        new_p = st.number_input(
            "单价", min_value=0.0, step=0.5,
            value=float(ing["unit_price"] or 0),
            key=f"ing_price_{ing['id']}",
            label_visibility="collapsed",
        )
    with c_src:
        st.caption(SOURCE_LABEL.get(ing["source"], ing["source"]))
    with c_use:
        st.caption(f"{len(usage)} 道菜" if usage else "未引用")
    with c_ops:
        b1, b2, b3 = st.columns(3)
        with b1:
            if st.button("💾", key=f"ing_save_{ing['id']}", help="保存新单价并重算菜品成本"):
                if abs(new_p - float(ing["unit_price"] or 0)) <= 1e-9:
                    st.info("单价未变化")
                else:
                    update_ingredient(ing["id"], unit_price=new_p)
                    # 人工改过的价标记为 manual，避免被后续大模型生成覆盖
                    conn = None
                    try:
                        conn = get_conn()
                        conn.execute("UPDATE ingredients SET source='manual' WHERE id=?", (ing["id"],))
                        conn.commit()
                    except Exception:
                        pass
                    finally:
                        if conn:
                            conn.close()
                    affected = recompute_costs_for_ingredient(ing["id"])
                    st.success(f"✅ 已更新单价并联动重算 {affected} 道菜的成本")
                    st.rerun()
        with b2:
            toggle_label = "🚫" if ing["status"] == "active" else "✅"
            if st.button(toggle_label, key=f"ing_toggle_{ing['id']}",
                         help="停用 / 启用"):
                update_ingredient_status(ing["id"], "inactive" if ing["status"] == "active" else "active")
                st.rerun()
        with b3:
            if st.button("🗑️", key=f"ing_del_{ing['id']}", help="删除食材（会同时移除配方中的引用）"):
                _refs = len(usage or [])
                arm_danger(
                    "ingredient", ing["id"], ing.get("display_name") or ing["name"],
                    warning=(f"确认删除食材「{ing.get('display_name') or ing['name']}」？"
                             + (f"它被 {_refs} 道菜的配方引用，删除后将一并移除这些引用。" if _refs else "")
                             + "此操作不可恢复。"),
                    extra={"被引用菜品数": _refs} if _refs else {},
                )
                st.rerun()

    # 引用明细（可展开）
    if usage:
        with st.expander(f"🔗 查看「{ing['display_name'] or ing['name']}」被哪些菜品引用", expanded=False):
            df = pd.DataFrame(usage).rename(columns={
                "dish_id": "菜品ID", "dish_name": "菜品", "quantity": "每份用量", "unit": "单位",
            })
            st.dataframe(df[["菜品", "每份用量", "单位"]], use_container_width=True, hide_index=True)

# ==================== 删除二次确认 ====================
_pend = render_danger_confirm()
if _pend and _pend["kind"] == "ingredient":
    try:
        delete_ingredient(_pend["id"])
        st.success(f"已删除食材「{_pend.get('label', '')}」")
    except Exception as _e:
        st.error(f"删除失败：{_e}")
    st.rerun()

st.divider()

# ==================== 批量操作 ====================
section("批量操作", icon="⚙️")
bc1, bc2 = st.columns(2)
with bc1:
    if st.button("🔄 重算本店全部菜品成本", use_container_width=True, key="recalc_all"):
        n = recompute_all_costs(store_id)
        st.success(f"✅ 已重算 {n} 道菜品的成本")
with bc2:
    df_export = pd.DataFrame([{
        "食材": i["display_name"] or i["name"], "品类": i["category"] or "", "单位": i["base_unit"] or "",
        "采购单价(元)": i["unit_price"] or 0, "来源": i["source"], "状态": i["status"],
    } for i in ings_all])
    st.download_button(
        "📥 导出食材清单 CSV",
        df_export.to_csv(index=False, encoding="utf-8-sig"),
        file_name=f"食材清单_{sel_name}.csv",
        mime="text/csv",
        use_container_width=True,
    )
    st.caption("💡 可导出「食材清单 CSV」备份，或用下方批量改价回灌。")

st.divider()

# ==================== 批量改价（CSV 回灌） ====================
section("批量改价（CSV 回灌）", icon="💰")
ec1, ec2 = st.columns(2)
with ec1:
    df_tpl = pd.DataFrame([{
        "食材": i["display_name"] or i["name"], "品类": i["category"] or "",
        "单位": i["base_unit"] or "", "采购单价(元)": i["unit_price"] or 0,
    } for i in ings_all])
    st.download_button("📤 导出改价模板 CSV", df_tpl.to_csv(index=False, encoding="utf-8-sig"),
                       file_name=f"食材改价模板_{sel_name}.csv", mime="text/csv", use_container_width=True,
                       key="ing_price_tpl")
with ec2:
    up = st.file_uploader("📥 导入改价 CSV", type=["csv"], key="ing_price_csv",
                          help="列含：食材、采购单价(元)（品类/单位可选，仅对照）。系统按食材名匹配本店食材。")
    if up:
        try:
            df_imp = pd.read_csv(up)
            name_col = "食材" if "食材" in df_imp.columns else df_imp.columns[0]
            price_col = "采购单价(元)" if "采购单价(元)" in df_imp.columns else df_imp.columns[-1]
            id_by_name = {}
            for i in ings_all:
                nm = (i["display_name"] or i["name"] or "").strip().lower()
                if nm:
                    id_by_name.setdefault(nm, i["id"])
            ok_n = skip_n = 0
            matched_ids = set()
            for _, row in df_imp.iterrows():
                nm = str(row[name_col]).strip().lower()
                pid = id_by_name.get(nm)
                if pid is None or pid in matched_ids:
                    skip_n += 1
                    continue
                try:
                    pv = float(row[price_col])
                except Exception:
                    skip_n += 1
                    continue
                update_ingredient(pid, unit_price=pv)
                conn = None
                try:
                    conn = get_conn()
                    conn.execute("UPDATE ingredients SET source='manual' WHERE id=?", (pid,))
                    conn.commit()
                except Exception:
                    pass
                finally:
                    if conn:
                        conn.close()
                matched_ids.add(pid)
                ok_n += 1
            if ok_n:
                recompute_all_costs(store_id)
                st.success(f"✅ 已更新 {ok_n} 种食材单价并重算全部菜品成本"
                           + (f"；{skip_n} 行未匹配/跳过" if skip_n else ""))
                st.rerun()
            else:
                st.warning(f"⚠️ 未匹配到任何食材（{skip_n} 行跳过），请检查「食材」列名与值")
        except Exception as e:
            st.error(f"❌ 导入失败：{e}")
