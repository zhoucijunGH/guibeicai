import streamlit as st
import pandas as pd

from guibeicai.web._nav import render_nav
from guibeicai.web._db import (
    get_all_stores, get_dishes_by_store, update_dish_status, init_db,
    get_store_by_id, get_user_store_ids, upsert_dish_ex, update_dish, delete_dish,
    get_recipes_map_by_store, add_recipe_item, update_recipe_item, delete_recipe_item,
    replace_recipe_for_dish, mark_bom_status, recompute_dish_cost, recompute_all_costs,
    get_dishes_need_regen, get_ingredients_by_store,
)
from guibeicai.web._audit import log_action, ACTION_DISH_STATUS
from guibeicai.web.components import arm_danger, render_danger_confirm

init_db()

from guibeicai.web._auth import require_auth, require_page_access, is_admin

user = require_auth()
require_page_access("dish_manage")


# ==================== 大模型重新生成配方 ====================
def _regenerate(dishes: list, force: bool = True):
    """调用大模型批量重新生成配方。dishes: [{id, name, ...}]"""
    from guibeicai.web._llm import generate_recipes_for_dishes
    names = sorted({d["name"] for d in dishes})
    prog = st.progress(0.0, text=f"🤖 正在为 {len(names)} 道菜重新生成配方…")

    def _cb(done, total):
        try:
            prog.progress(min(done / max(total, 1), 1.0), text=f"🤖 已生成 {done}/{total}…")
        except Exception:
            pass

    res = generate_recipes_for_dishes(names, batch_size=8, max_workers=5, progress_cb=_cb, force=force)
    prog.empty()

    ok = fail = 0
    for d in dishes:
        items = res.get("ok", {}).get(d["name"])
        if items:
            replace_recipe_for_dish(d["id"], items, source="llm")
            ok += 1
        else:
            mark_bom_status(d["id"], "pending")
            fail += 1
    if fail == 0:
        st.success(f"✅ 已重新生成 {ok} 道菜的配方")
    else:
        st.warning(f"⚠️ 成功 {ok} 道，失败 {fail} 道（已标记待生成，可稍后重试）")
    return ok, fail


# ==================== 批量重新生成（存量全量重刷入口） ====================
def _render_batch_regen(dishes: list, store_id: int):
    pending = [d for d in dishes if d.get("bom_status") == "pending"]
    if not pending:
        return
    with st.expander(f"🤖 批量生成配方（{len(pending)} 道菜待生成）", expanded=False):
        st.warning(
            f"该门店有 **{len(pending)}** 道菜尚无有效配方，将影响食材采购（食材需求与采购单）。\n\n"
            f"点击下方按钮将调用大模型一次性生成全部配方（食材 / 每份用量 / 市场估价）。"
        )
        n_limit = st.number_input("本次生成数量（0 表示全部）", min_value=0,
                                  value=0, step=10, key=f"batch_limit_{store_id}")
        if st.button("🚀 开始批量生成", type="primary", key=f"batch_regen_{store_id}"):
            targets = pending if not n_limit else pending[: int(n_limit)]
            with st.spinner(f"正在生成 {len(targets)} 道菜的配方，请稍候…"):
                ok, fail = _regenerate(targets, force=False)
            st.rerun()


# ==================== 单个菜品行（含食材明细展开） ====================
def _render_dish_row(dish: dict, store_id: int, items: list):
    cost = sum(float(i["amount"] or 0) for i in items)
    price = float(dish["standard_price"] or 0)
    margin = f"{(price - cost) / price * 100:.0f}%" if price > 0 else "-"

    c1, c2, c3, c4, c5 = st.columns([3.2, 1.6, 1.8, 1.4, 2.6])
    with c1:
        st.write(("~~" + dish["name"] + "~~") if dish["status"] != "active" else f"**{dish['name']}**")
        if dish.get("category"):
            st.caption(dish["category"])
    with c2:
        st.metric("售价", f"¥{price:,.2f}")
    with c3:
        st.metric("食材成本", f"¥{cost:,.2f}")
    with c4:
        st.metric("毛利率", margin)
    with c5:
        b1, b2, b3 = st.columns(3)
        with b1:
            st.button("停用" if dish["status"] == "active" else "启用",
                      key=f"dish_toggle_{dish['id']}", use_container_width=True,
                      on_click=_toggle_dish, args=(dish, store_id))
        with b2:
            st.button("✏️", key=f"dish_edit_open_{dish['id']}", use_container_width=True,
                      help="编辑菜品", on_click=_set_edit, args=(dish["id"],))
        with b3:
            st.button("🗑️", key=f"dish_del_open_{dish['id']}", use_container_width=True,
                      help="删除菜品", on_click=_set_delete, args=(dish["id"],))

    # 二次确认删除
    if st.session_state.get("_pending_delete") == dish["id"]:
        st.error(f"⚠️ 确认删除菜品「{dish['name']}」？其 {len(items)} 条食材配方将一并删除，且不可恢复。")
        y, n = st.columns([1, 4])
        with y:
            if st.button("✅ 确认删除", key=f"dish_del_yes_{dish['id']}", type="primary"):
                delete_dish(dish["id"])
                st.session_state.pop("_pending_delete", None)
                st.success(f"已删除「{dish['name']}」")
                st.rerun()
        with n:
            if st.button("取消", key=f"dish_del_no_{dish['id']}"):
                st.session_state.pop("_pending_delete", None)
                st.rerun()

    # 编辑表单
    if st.session_state.get("_editing_dish") == dish["id"]:
        with st.form(f"edit_dish_{dish['id']}"):
            e1, e2, e3 = st.columns([3, 2, 2])
            with e1:
                nn = st.text_input("菜品名称", value=dish["name"])
            with e2:
                nc = st.text_input("品类", value=dish.get("category") or "")
            with e3:
                np_ = st.number_input("售价(元)", min_value=0.0, step=1.0, value=price)
            cs, cx = st.columns([1, 4])
            with cs:
                if st.form_submit_button("💾 保存", type="primary"):
                    update_dish(dish["id"], name=nn.strip() or dish["name"], category=nc.strip(),
                                standard_price=float(np_))
                    st.session_state.pop("_editing_dish", None)
                    st.success("✅ 已保存")
                    st.rerun()
            with cx:
                if st.form_submit_button("取消"):
                    st.session_state.pop("_editing_dish", None)
                    st.rerun()

    # 食材明细
    with st.expander(f"🥘 食材明细（{len(items)} 种）", expanded=False):
        render_recipe_panel(dish, items)
    st.divider()


def _set_edit(dish_id: int):
    st.session_state["_editing_dish"] = dish_id
    st.session_state.pop("_pending_delete", None)


def _set_delete(dish_id: int):
    st.session_state["_pending_delete"] = dish_id
    st.session_state.pop("_editing_dish", None)


def _toggle_dish(dish: dict, store_id: int):
    new_status = "discontinued" if dish["status"] == "active" else "active"
    update_dish_status(dish["id"], new_status)
    log_action(ACTION_DISH_STATUS, target_type="dish", target_id=dish["id"],
               detail={"name": dish["name"], "from": dish["status"], "to": new_status,
                       "store_id": store_id}, user=user)


# ==================== 菜品配方（食材明细）面板 ====================
def render_recipe_panel(dish: dict, items: list):
    """展示并维护某菜品的食材明细：种类、用量均可增删改查。items 由调用方批量预取（避免 N+1）。"""

    if not items:
        st.caption("⚠️ 该菜品暂无食材配方，可点下方「重新生成配方」由大模型自动生成，或手工新增。")
    else:
        total = sum(float(i["amount"] or 0) for i in items)
        st.caption(f"🧮 单份食材成本：**¥{total:,.2f}**（售价 ¥{float(dish['standard_price'] or 0):,.2f}）")

        h = st.columns([3, 1.6, 1.8, 1.4, 1.4, 1.6, 1.2])
        for col, txt in zip(h, ["食材", "品类", "每份用量", "单位", "单价(元)", "小计(元)", "操作"]):
            col.caption(f"**{txt}**")

        for it in items:
            a1, a2, a3, a4, a5, a6, a7 = st.columns([3, 1.6, 1.8, 1.4, 1.4, 1.6, 1.2])
            with a1:
                new_name = st.text_input("食材", value=it["ingredient_name"],
                                         key=f"rc_name_{it['id']}", label_visibility="collapsed")
            with a2:
                st.caption(it["category"] or "-")
            with a3:
                new_qty = st.number_input("用量", min_value=0.0, step=0.01,
                                          value=float(it["quantity"] or 0),
                                          key=f"rc_qty_{it['id']}", label_visibility="collapsed")
            with a4:
                st.caption(it["unit"] or "-")
            with a5:
                st.caption(f"{float(it['effective_price'] or 0):,.2f}")
            with a6:
                st.caption(f"{float(it['amount'] or 0):,.3f}")
            with a7:
                s1, s2 = st.columns(2)
                with s1:
                    if st.button("💾", key=f"rc_save_{it['id']}", help="保存改动"):
                        update_recipe_item(
                            it["id"],
                            ingredient_name=new_name if new_name != it["ingredient_name"] else None,
                            quantity=new_qty if abs(new_qty - float(it["quantity"] or 0)) > 1e-9 else None,
                        )
                        recompute_dish_cost(dish["id"])
                        st.success("✅ 已保存")
                        st.rerun()
                with s2:
                    if st.button("🗑️", key=f"rc_del_{it['id']}", help="删除该食材"):
                        arm_danger(
                            "recipe_item", it["id"], it["ingredient_name"],
                            warning=f"确认从「{dish['name']}」配方中移除食材「{it['ingredient_name']}」？菜品成本将随之重算。",
                            pending_key="_pending_danger_rc",
                        )
                        st.rerun()
        # 配方行删除二次确认（独立 key，避免与整菜删除冲突）
        _rc_pend = render_danger_confirm(pending_key="_pending_danger_rc", confirm_label="✅ 确认移除")
        if _rc_pend and _rc_pend["kind"] == "recipe_item":
            delete_recipe_item(_rc_pend["id"])
            recompute_dish_cost(dish["id"])
            st.success(f"已移除食材「{_rc_pend.get('label', '')}」")
            st.rerun()

    # 新增食材行
    with st.form(f"add_rc_{dish['id']}", clear_on_submit=True):
        st.caption("➕ 为该菜品新增食材")
        b1, b2, b3, b4, b5 = st.columns([3, 2, 1.6, 1.4, 1.2])
        with b1:
            ing_name = st.text_input("食材名称", placeholder="如：花生米", label_visibility="collapsed")
        with b2:
            ing_cat = st.selectbox("品类", ["蔬菜", "肉类", "禽蛋", "水产", "豆制品",
                                            "菌菇", "米面", "调料", "粮油", "干货", "其他"],
                                   label_visibility="collapsed")
        with b3:
            ing_qty = st.number_input("每份用量", min_value=0.0, step=0.01, value=0.1, label_visibility="collapsed")
        with b4:
            ing_unit = st.selectbox("单位", ["kg", "g", "L", "ml", "个", "只", "片", "份"], label_visibility="collapsed")
        with b5:
            sub = st.form_submit_button("添加")
        if sub:
            if not ing_name.strip():
                st.error("❌ 请填写食材名称。")
            elif ing_qty <= 0:
                st.error("❌ 用量必须大于 0。")
            else:
                add_recipe_item(dish["id"], ing_name.strip(), float(ing_qty), unit=ing_unit, category=ing_cat)
                mark_bom_status(dish["id"], "manual", source="manual")
                recompute_dish_cost(dish["id"])
                st.success(f"✅ 已为「{dish['name']}」添加食材「{ing_name.strip()}」")
                st.rerun()

    c_regen, c_tip = st.columns([2, 5])
    with c_regen:
        if st.button("🤖 重新生成配方", key=f"regen_{dish['id']}",
                     help="调用大模型重新生成该菜品的食材与用量（覆盖现有配方）"):
            _regenerate([dish])
    with c_tip:
        st.caption("重新生成会覆盖当前配方；在「食材管理」手工改过的单价不会被覆盖。")

st.set_page_config(page_title="菜品管理", page_icon="🍳", layout="wide")
render_nav()
st.title("🍳 菜品管理")

# ==================== 省市数据 ====================
PROVINCES = [
    "全部", "北京市", "天津市", "河北省", "山西省", "内蒙古自治区",
    "辽宁省", "吉林省", "黑龙江省",
    "上海市", "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省",
    "河南省", "湖北省", "湖南省", "广东省", "广西壮族自治区", "海南省",
    "重庆市", "四川省", "贵州省", "云南省", "西藏自治区",
    "陕西省", "甘肃省", "青海省", "宁夏回族自治区", "新疆维吾尔自治区",
]

CITIES = {
    "北京市": ["东城区","西城区","朝阳区","丰台区","石景山区","海淀区","门头沟区","房山区","通州区","顺义区","昌平区","大兴区","怀柔区","平谷区","密云区","延庆区"],
    "天津市": ["和平区","河东区","河西区","南开区","河北区","红桥区","东丽区","西青区","津南区","北辰区","武清区","宝坻区","滨海新区","宁河区","静海区","蓟州区"],
    "河北省": ["石家庄市","唐山市","秦皇岛市","邯郸市","邢台市","保定市","张家口市","承德市","沧州市","廊坊市","衡水市"],
    "山西省": ["太原市","大同市","阳泉市","长治市","晋城市","朔州市","晋中市","运城市","忻州市","临汾市","吕梁市"],
    "内蒙古自治区": ["呼和浩特市","包头市","乌海市","赤峰市","通辽市","鄂尔多斯市","呼伦贝尔市","巴彦淖尔市","乌兰察布市"],
    "辽宁省": ["沈阳市","大连市","鞍山市","抚顺市","本溪市","丹东市","锦州市","营口市","阜新市","辽阳市","盘锦市","铁岭市","朝阳市","葫芦岛市"],
    "吉林省": ["长春市","吉林市","四平市","辽源市","通化市","白山市","松原市","白城市"],
    "黑龙江省": ["哈尔滨市","齐齐哈尔市","鸡西市","鹤岗市","双鸭山市","大庆市","伊春市","佳木斯市","七台河市","牡丹江市","黑河市","绥化市"],
    "上海市": ["黄浦区","徐汇区","长宁区","静安区","普陀区","虹口区","杨浦区","闵行区","宝山区","嘉定区","浦东新区","金山区","松江区","青浦区","奉贤区","崇明区"],
    "江苏省": ["南京市","无锡市","徐州市","常州市","苏州市","南通市","连云港市","淮安市","盐城市","扬州市","镇江市","泰州市","宿迁市"],
    "浙江省": ["杭州市","宁波市","温州市","嘉兴市","湖州市","绍兴市","金华市","衢州市","舟山市","台州市","丽水市"],
    "安徽省": ["合肥市","芜湖市","蚌埠市","淮南市","马鞍山市","淮北市","铜陵市","安庆市","黄山市","滁州市","阜阳市","宿州市","六安市","亳州市","池州市","宣城市"],
    "福建省": ["福州市","厦门市","莆田市","三明市","泉州市","漳州市","南平市","龙岩市","宁德市"],
    "江西省": ["南昌市","景德镇市","萍乡市","九江市","新余市","鹰潭市","赣州市","吉安市","宜春市","抚州市","上饶市"],
    "山东省": ["济南市","青岛市","淄博市","枣庄市","东营市","烟台市","潍坊市","济宁市","泰安市","威海市","日照市","临沂市","德州市","聊城市","滨州市","菏泽市"],
    "河南省": ["郑州市","开封市","洛阳市","平顶山市","安阳市","鹤壁市","新乡市","焦作市","濮阳市","许昌市","漯河市","三门峡市","南阳市","商丘市","信阳市","周口市","驻马店市"],
    "湖北省": ["武汉市","黄石市","十堰市","宜昌市","襄阳市","鄂州市","荆门市","孝感市","荆州市","黄冈市","咸宁市","随州市"],
    "湖南省": ["长沙市","株洲市","湘潭市","衡阳市","邵阳市","岳阳市","常德市","张家界市","益阳市","郴州市","永州市","怀化市","娄底市"],
    "广东省": ["广州市","韶关市","深圳市","珠海市","汕头市","佛山市","江门市","湛江市","茂名市","肇庆市","惠州市","梅州市","汕尾市","河源市","阳江市","清远市","东莞市","中山市","潮州市","揭阳市","云浮市"],
    "广西壮族自治区": ["南宁市","柳州市","桂林市","梧州市","北海市","防城港市","钦州市","贵港市","玉林市","百色市","贺州市","河池市","来宾市","崇左市"],
    "海南省": ["海口市","三亚市","三沙市","儋州市"],
    "重庆市": ["万州区","涪陵区","渝中区","大渡口区","江北区","沙坪坝区","九龙坡区","南岸区","北碚区","渝北区","巴南区","黔江区","长寿区","江津区","合川区","永川区","南川区","璧山区","铜梁区","潼南区","荣昌区","开州区","梁平区","武隆区"],
    "四川省": ["成都市","自贡市","攀枝花市","泸州市","德阳市","绵阳市","广元市","遂宁市","内江市","乐山市","南充市","眉山市","宜宾市","广安市","达州市","雅安市","巴中市","资阳市"],
    "贵州省": ["贵阳市","六盘水市","遵义市","安顺市","毕节市","铜仁市"],
    "云南省": ["昆明市","曲靖市","玉溪市","保山市","昭通市","丽江市","普洱市","临沧市"],
    "西藏自治区": ["拉萨市","日喀则市","昌都市","林芝市","山南市","那曲市"],
    "陕西省": ["西安市","铜川市","宝鸡市","咸阳市","渭南市","延安市","汉中市","榆林市","安康市","商洛市"],
    "甘肃省": ["兰州市","嘉峪关市","金昌市","白银市","天水市","武威市","张掖市","平凉市","酒泉市","庆阳市","定西市","陇南市"],
    "青海省": ["西宁市","海东市"],
    "宁夏回族自治区": ["银川市","石嘴山市","吴忠市","固原市","中卫市"],
    "新疆维吾尔自治区": ["乌鲁木齐市","克拉玛依市","吐鲁番市","哈密市"],
}

# ==================== session_state 初始化 ====================
for k, v in {
    "dish_province": "全部",
    "dish_city": "全部",
    "dish_store_id": None,
    "dish_queried": False,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ==================== 商家：多店权限，可切换门店 ====================
if not is_admin(user):
    allowed_ids = set(get_user_store_ids(user.get("id", 0)))
    if not allowed_ids:
        st.error("❌ 您没有绑定任何门店，请联系管理员。")
        st.stop()

    # 取有权限的门店列表
    all_stores = get_all_stores(active_only=False)
    allowed_stores = [s for s in all_stores if s["id"] in allowed_ids]

    # 门店选择下拉框
    store_options = {f"{s.get('province','')} {s.get('city','')} {s['name']}": s["id"] for s in allowed_stores}
    sel_label = st.selectbox("选择门店", list(store_options.keys()), key="merchant_store_select")
    store_id = store_options[sel_label]

    st.subheader("菜品列表")
    dishes = get_dishes_by_store(store_id, active_only=False)
    if not dishes:
        st.info("该门店暂无菜品，上传销售数据后会自动创建。")
    else:
        _render_batch_regen(dishes, store_id)
        # [C4 性能] 一次取全店配方，避免每道菜两次查询（N+1）
        _recipes_map = get_recipes_map_by_store(store_id)
        for d in dishes:
            _render_dish_row(d, store_id, _recipes_map.get(d["id"], []))
        st.caption("停用的菜品不会参与预测。")
    st.stop()

# ==================== 以下仅 admin ====================
stores = get_all_stores(active_only=False)
if not stores:
    st.info("暂无门店数据。")
    st.stop()

st.subheader("🔍 筛选门店")

# ---- 省份选择（form 外，on_change 联动） ----
def on_province_change():
    """省份变了，城市重置为全部，门店清空"""
    st.session_state.dish_city = "全部"
    st.session_state.dish_store_id = None
    st.session_state.dish_queried = False

p_idx = PROVINCES.index(st.session_state.dish_province) if st.session_state.dish_province in PROVINCES else 0
st.session_state.dish_province = st.selectbox(
    "省份",
    PROVINCES,
    index=p_idx,
    key="province_sb",
    on_change=on_province_change,
)

# ---- 城市选择（联动） ----
if st.session_state.dish_province == "全部":
    city_opts = ["全部"]
else:
    city_opts = CITIES.get(st.session_state.dish_province, [])

def on_city_change():
    """城市变了，门店清空"""
    st.session_state.dish_store_id = None
    st.session_state.dish_queried = False

c_idx = city_opts.index(st.session_state.dish_city) if st.session_state.dish_city in city_opts else 0
st.session_state.dish_city = st.selectbox(
    "城市",
    city_opts,
    index=c_idx,
    key="city_sb",
    on_change=on_city_change,
)

# ---- 门店选择（根据省市过滤，放在 form 里） ----
filtered = stores
if st.session_state.dish_province != "全部":
    filtered = [s for s in filtered if s.get("province") == st.session_state.dish_province]
if st.session_state.dish_city != "全部":
    filtered = [s for s in filtered if s.get("city") == st.session_state.dish_city]

store_opts = {s["name"]: s["id"] for s in filtered}

with st.form("query_dish_form"):
    store_name_list = list(store_opts.keys())
    if store_name_list:
        sel_store_name = st.selectbox("门店", store_name_list, key="store_sb")
    else:
        st.write("该城市下暂无门店")
        sel_store_name = None

    submitted = st.form_submit_button("查询菜品", type="primary")

    if submitted and sel_store_name:
        st.session_state.dish_store_id = store_opts[sel_store_name]
        st.session_state.dish_queried = True

# ---- 查询结果 ----
if st.session_state.dish_queried and st.session_state.dish_store_id:
    st.divider()
    sel_store = next((s for s in stores if s["id"] == st.session_state.dish_store_id), None)
    if sel_store:
        st.subheader(f"📋 {sel_store['name']} - 菜品列表")

    dishes = get_dishes_by_store(st.session_state.dish_store_id, active_only=False)
    if not dishes:
        st.info("该门店暂无菜品，上传销售数据后会自动创建。")
    else:
        _render_batch_regen(dishes, st.session_state.dish_store_id)
        # [C4 性能] 一次取全店配方，避免每道菜两次查询（N+1）
        _recipes_map2 = get_recipes_map_by_store(st.session_state.dish_store_id)
        for d in dishes:
            _render_dish_row(d, st.session_state.dish_store_id, _recipes_map2.get(d["id"], []))
        st.caption("停用的菜品不会参与预测。")