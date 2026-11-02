import re

import streamlit as st
from guibeicai.web._nav import render_nav
from guibeicai.web._db import (
    get_all_stores, update_store_status, init_db,
    upsert_store, update_store, delete_store, get_user_store_ids,
    parse_auto_predict_time, get_store_delete_impact,
)
from guibeicai.web._audit import log_action, ACTION_STORE_CREATE, ACTION_STORE_UPDATE, ACTION_STORE_DELETE, ACTION_STORE_STATUS
from guibeicai.web.components import arm_danger, render_danger_confirm

init_db()

from guibeicai.web._auth import require_auth, require_page_access, is_admin

user = require_auth()
require_page_access("store_manage")

st.set_page_config(page_title="门店管理", page_icon="🏪", layout="wide")
render_nav()
st.title("🏪 门店管理")

# ==================== 省市数据 ====================
PROVINCES = [
    "全部", "北京市", "天津市", "河北省", "山西省", "内蒙古自治区",
    "辽宁省", "吉林省", "黑龙江省",
    "上海市", "江苏省", "浙江省", "安徽省", "福建省", "江西省", "山东省",
    "河南省", "湖北省", "湖南省", "广东省", "广西壮族自治区", "海南省",
    "重庆市", "四川省", "贵州省", "云南省", "西藏自治区",
    "陕西省", "甘肃省", "青海省", "宁夏回族自治区", "新疆维吾尔自治区",
    "台湾省", "香港特别行政区", "澳门特别行政区",
]

CITIES = {
    "北京市": ["全部", "东城区","西城区","朝阳区","丰台区","石景山区","海淀区","门头沟区","房山区","通州区","顺义区","昌平区","大兴区","怀柔区","平谷区","密云区","延庆区"],
    "天津市": ["全部", "和平区","河东区","河西区","南开区","河北区","红桥区","东丽区","西青区","津南区","北辰区","武清区","宝坻区","滨海新区","宁河区","静海区","蓟州区"],
    "河北省": ["全部", "石家庄市","唐山市","秦皇岛市","邯郸市","邢台市","保定市","张家口市","承德市","沧州市","廊坊市","衡水市"],
    "山西省": ["全部", "太原市","大同市","阳泉市","长治市","晋城市","朔州市","晋中市","运城市","忻州市","临汾市","吕梁市"],
    "内蒙古自治区": ["全部", "呼和浩特市","包头市","乌海市","赤峰市","通辽市","鄂尔多斯市","呼伦贝尔市","巴彦淖尔市","乌兰察布市"],
    "辽宁省": ["全部", "沈阳市","大连市","鞍山市","抚顺市","本溪市","丹东市","锦州市","营口市","阜新市","辽阳市","盘锦市","铁岭市","朝阳市","葫芦岛市"],
    "吉林省": ["全部", "长春市","吉林市","四平市","辽源市","通化市","白山市","松原市","白城市"],
    "黑龙江省": ["全部", "哈尔滨市","齐齐哈尔市","鸡西市","鹤岗市","双鸭山市","大庆市","伊春市","佳木斯市","七台河市","牡丹江市","黑河市","绥化市"],
    "上海市": ["全部", "黄浦区","徐汇区","长宁区","静安区","普陀区","虹口区","杨浦区","闵行区","宝山区","嘉定区","浦东新区","金山区","松江区","青浦区","奉贤区","崇明区"],
    "江苏省": ["全部", "南京市","无锡市","徐州市","常州市","苏州市","南通市","连云港市","淮安市","盐城市","扬州市","镇江市","泰州市","宿迁市"],
    "浙江省": ["全部", "杭州市","宁波市","温州市","嘉兴市","湖州市","绍兴市","金华市","衢州市","舟山市","台州市","丽水市"],
    "安徽省": ["全部", "合肥市","芜湖市","蚌埠市","淮南市","马鞍山市","淮北市","铜陵市","安庆市","黄山市","滁州市","阜阳市","宿州市","六安市","亳州市","池州市","宣城市"],
    "福建省": ["全部", "福州市","厦门市","莆田市","三明市","泉州市","漳州市","南平市","龙岩市","宁德市"],
    "江西省": ["全部", "南昌市","景德镇市","萍乡市","九江市","新余市","鹰潭市","赣州市","吉安市","宜春市","抚州市","上饶市"],
    "山东省": ["全部", "济南市","青岛市","淄博市","枣庄市","东营市","烟台市","潍坊市","济宁市","泰安市","威海市","日照市","临沂市","德州市","聊城市","滨州市","菏泽市"],
    "河南省": ["全部", "郑州市","开封市","洛阳市","平顶山市","安阳市","鹤壁市","新乡市","焦作市","濮阳市","许昌市","漯河市","三门峡市","南阳市","商丘市","信阳市","周口市","驻马店市"],
    "湖北省": ["全部", "武汉市","黄石市","十堰市","宜昌市","襄阳市","鄂州市","荆门市","孝感市","荆州市","黄冈市","咸宁市","随州市"],
    "湖南省": ["全部", "长沙市","株洲市","湘潭市","衡阳市","邵阳市","岳阳市","常德市","张家界市","益阳市","郴州市","永州市","怀化市","娄底市"],
    "广东省": ["全部", "广州市","韶关市","深圳市","珠海市","汕头市","佛山市","江门市","湛江市","茂名市","肇庆市","惠州市","梅州市","汕尾市","河源市","阳江市","清远市","东莞市","中山市","潮州市","揭阳市","云浮市"],
    "广西壮族自治区": ["全部", "南宁市","柳州市","桂林市","梧州市","北海市","防城港市","钦州市","贵港市","玉林市","百色市","贺州市","河池市","来宾市","崇左市"],
    "海南省": ["全部", "海口市","三亚市","三沙市","儋州市"],
    "重庆市": ["全部", "万州区","涪陵区","渝中区","大渡口区","江北区","沙坪坝区","九龙坡区","南岸区","北碚区","渝北区","巴南区","黔江区","长寿区","江津区","合川区","永川区","南川区","璧山区","铜梁区","潼南区","荣昌区","开州区","梁平区","武隆区"],
    "四川省": ["全部", "成都市","自贡市","攀枝花市","泸州市","德阳市","绵阳市","广元市","遂宁市","内江市","乐山市","南充市","眉山市","宜宾市","广安市","达州市","雅安市","巴中市","资阳市"],
    "贵州省": ["全部", "贵阳市","六盘水市","遵义市","安顺市","毕节市","铜仁市"],
    "云南省": ["全部", "昆明市","曲靖市","玉溪市","保山市","昭通市","丽江市","普洱市","临沧市"],
    "西藏自治区": ["全部", "拉萨市","日喀则市","昌都市","林芝市","山南市","那曲市"],
    "陕西省": ["全部", "西安市","铜川市","宝鸡市","咸阳市","渭南市","延安市","汉中市","榆林市","安康市","商洛市"],
    "甘肃省": ["全部", "兰州市","嘉峪关市","金昌市","白银市","天水市","武威市","张掖市","平凉市","酒泉市","庆阳市","定西市","陇南市"],
    "青海省": ["全部", "西宁市","海东市"],
    "宁夏回族自治区": ["全部", "银川市","石嘴山市","吴忠市","固原市","中卫市"],
    "新疆维吾尔自治区": ["全部", "乌鲁木齐市","克拉玛依市","吐鲁番市","哈密市"],
    "台湾省": ["全部", "台北市","新北市","桃园市","台中市","台南市","高雄市"],
    "香港特别行政区": ["全部", "香港岛","九龙","新界"],
    "澳门特别行政区": ["全部", "澳门半岛","氹仔","路环"],
}

# ==================== session_state 初始化 ====================
for k, v in {
    "store_province": "全部",
    "store_city": "全部",
    "edit_store_id": None,
    "filter_province": "全部",
    "filter_city": "全部",
    "filter_name": "",
    "store_queried": False,
}.items():
    if k not in st.session_state:
        st.session_state[k] = v

# ==================== 新增门店（仅 admin） ====================
if is_admin(user):
    with st.expander("➕ 新增门店", expanded=False):
        new_name = st.text_input("门店名称 *", key="new_store_name")

        col_p, col_c = st.columns(2)
        p_idx = PROVINCES.index(st.session_state.store_province)
        new_p = col_p.selectbox("省份 *", PROVINCES, index=p_idx, key="sb_province")

        if new_p != st.session_state.store_province:
            st.session_state.store_province = new_p
            st.session_state.store_city = "全部"
            st.rerun()

        city_opts = CITIES.get(st.session_state.store_province, ["全部"])
        c_idx = city_opts.index(st.session_state.store_city) if st.session_state.store_city in city_opts else 0
        new_c = col_c.selectbox("城市 *", city_opts, index=c_idx, key="sb_city")

        if new_c != st.session_state.store_city:
            st.session_state.store_city = new_c

        st.divider()
        st.caption("📲 采购单推送配置（移动端）")
        new_owner_email = st.text_input("老板邮箱（多个用逗号分隔）", key="new_owner_email")
        new_wecom = st.text_input("企业微信机器人 Webhook", key="new_wecom")

        st.divider()
        st.caption("🤖 自动预测配置")
        new_auto_predict = st.checkbox("参与自动预测（每日自动按配置预测并推送采购单）",
                                       value=True, key="new_auto_predict",
                                       help="关闭后该门店不参与自动预测，仅可手工在预测配置页执行")
        col_np1, col_np2 = st.columns(2)
        target_opts = {"今天": 0, "明天": 1, "后天": 2}
        new_target_label = col_np1.selectbox("预测目标日", options=list(target_opts.keys()),
                                             index=1, key="new_target_label")
        new_auto_predict_time = col_np2.text_input("每日预测时间", value="06:00", key="new_auto_predict_time",
                                                   help="格式 HH:MM，如 06:00；到点后若当天未预测过则自动执行")
        new_alert_push = st.checkbox("开启预警自动推送（命中预警时邮件/企微通知老板）",
                                     value=True, key="new_alert_push",
                                     help="关闭后该门店预警仅站内展示，不自动推送")

        if st.button("创建门店", type="primary", key="btn_create_store"):
            if not new_name or not new_name.strip():
                st.error("门店名称不能为空")
            elif st.session_state.store_province == "全部" or st.session_state.store_city == "全部":
                st.error("请选择省份和城市")
            elif not re.match(r'^\d{1,2}\s*:\s*\d{2}$', new_auto_predict_time.strip()):
                st.error("每日预测时间格式错误，请使用 HH:MM（如 06:00）")
            else:
                upsert_store(new_name.strip(), st.session_state.store_city, st.session_state.store_province,
                             owner_email=new_owner_email.strip(), wecom_webhook=new_wecom.strip(),
                             auto_predict=1 if new_auto_predict else 0,
                             auto_predict_target_offset=target_opts[new_target_label],
                             auto_predict_time=new_auto_predict_time.strip() or "06:00",
                             alert_push_enabled=1 if new_alert_push else 0)
                log_action(ACTION_STORE_CREATE, target_type="store", target_id=new_name.strip(),
                           detail={"name": new_name.strip(), "province": st.session_state.store_province, "city": st.session_state.store_city}, user=user)
                st.success(f"门店【{new_name.strip()}】已创建")
                st.session_state.store_province = "全部"
                st.session_state.store_city = "全部"
                st.rerun()
else:
    st.info("仅管理员可新增门店")

# ==================== 筛选条件 ====================
st.divider()
st.subheader("🔍 筛选查询")

with st.form("store_filter_form"):
    fcol1, fcol2, fcol3 = st.columns([2, 2, 3])

    fp_idx = PROVINCES.index(st.session_state.filter_province)
    filter_province = fcol1.selectbox("省份", PROVINCES, index=fp_idx, key="form_filter_province")

    city_opts = CITIES.get(filter_province, ["全部"])
    fc_idx = city_opts.index(st.session_state.filter_city) if st.session_state.filter_city in city_opts else 0
    filter_city = fcol2.selectbox("城市", city_opts, index=fc_idx, key="form_filter_city")

    filter_name = fcol3.text_input("店名（模糊查询）", value=st.session_state.filter_name, placeholder="输入关键词")

    submitted = st.form_submit_button("查询", type="primary")

    if submitted:
        st.session_state.filter_province = filter_province
        st.session_state.filter_city = filter_city
        st.session_state.filter_name = filter_name.strip()
        st.session_state.store_queried = True

# ==================== 门店列表（点查询后才展示） ====================
if st.session_state.store_queried:
    st.subheader("门店列表")

    # 按权限过滤：admin看全部，普通用户只看授权的
    if is_admin(user):
        all_stores = get_all_stores(active_only=False)
    else:
        allowed_ids = set(get_user_store_ids(user.get("id", 0)))
        all_stores = [s for s in get_all_stores(active_only=False) if s["id"] in allowed_ids]

    # 筛选
    filtered = all_stores
    if st.session_state.filter_province != "全部":
        filtered = [s for s in filtered if s.get("province") == st.session_state.filter_province]
    if st.session_state.filter_city != "全部":
        filtered = [s for s in filtered if s.get("city") == st.session_state.filter_city]
    if st.session_state.filter_name:
        filtered = [s for s in filtered if st.session_state.filter_name.lower() in s.get("name", "").lower()]

    if not filtered:
        st.info("暂无符合条件的门店数据。")
    else:
        # 提前算权限（用于按钮拦截）
        if not is_admin(user):
            allowed_ids = set(get_user_store_ids(user.get("id", 0)))
        else:
            allowed_ids = set()

        for s in filtered:
            sid = s["id"]

            # 非admin且不在授权列表：只显示信息，不显示操作按钮
            _target_label = {0: "今天", 1: "明天", 2: "后天"}.get(s.get("auto_predict_target_offset", 1), "明天")
            _target_time = s.get("auto_predict_time") or "06:00"
            _target_text = f"{_target_label} {_target_time}"

            if not is_admin(user) and sid not in allowed_ids:
                col1, col2, col3, col4, col5, col6, col7, col8, col9 = st.columns([2.6, 1.6, 1.0, 1.0, 1.8, 2.0, 1.0, 1.0, 1.0])
                col1.write(f"**{s.get('name', '')}**")
                col2.write(f"{s.get('province', '')} {s.get('city', '')}".strip())
                col3.write("✅ 营业中" if s.get("status") == "active" else "🚫 已关闭")
                col4.write("✅ 自动" if s.get("auto_predict", 1) else "⛔ 关闭")
                col5.write(_target_text)
                _lap = s.get("last_auto_predict_at") or ""
                _lps = s.get("last_auto_predict_status") or ""
                col6.write(f"😠 失败 {_lap}" if _lps == "failed" else (_lap and f"{_lap} · {_lps}" or "—"))
                col7.write("🔒 无权限")
                col8.write("")
                col9.write("")
                continue

            col1, col2, col3, col4, col5, col6, col7, col8, col9 = st.columns([2.6, 1.6, 1.0, 1.0, 1.8, 2.0, 1.0, 1.0, 1.0])
            col1.write(f"**{s.get('name', '')}**")
            col2.write(f"{s.get('province', '')} {s.get('city', '')}".strip())
            col3.write("✅ 营业中" if s.get("status") == "active" else "🚫 已关闭")
            col4.write("✅ 自动" if s.get("auto_predict", 1) else "⛔ 关闭")
            col5.write(_target_text)
            _lap = s.get("last_auto_predict_at") or ""
            _lps = s.get("last_auto_predict_status") or ""
            if _lps == "failed":
                col6.write(f"😠 失败 {_lap}")
            elif _lap:
                col6.write(f"{_lap} · {_lps}")
            else:
                col6.write("—")

            label = "关闭" if s.get("status") == "active" else "启用"
            if col7.button(label, key=f"store_toggle_{sid}"):
                new_status = "closed" if s.get("status") == "active" else "active"
                update_store_status(sid, new_status)
                log_action(ACTION_STORE_STATUS, target_type="store", target_id=sid,
                           detail={"name": s.get("name", ""), "from": s.get("status"), "to": new_status}, user=user)
                st.rerun()

            if col8.button("✏️ 编辑", key=f"edit_{sid}"):
                st.session_state.edit_store_id = sid
                st.rerun()

            if col9.button("🗑️ 删除", key=f"del_{sid}"):
                # 二次确认：展示级联清理数据量，避免误删整店数据
                try:
                    _impact = {k: v for k, v in get_store_delete_impact(sid).items() if v}
                except Exception:
                    _impact = {}
                arm_danger(
                    "store", sid, s.get("name", ""),
                    warning=(f"确认删除门店「{s.get('name', '')}」？该门店的菜品、食材、配方、"
                             f"历史销量、预测、POS 数据源等将【一并永久删除】，且不可恢复。"),
                    extra=_impact,
                )
                st.rerun()

        st.caption(f"共 {len(filtered)} 条记录")

# ==================== 删除二次确认 ====================
_pend = render_danger_confirm()
if _pend and _pend["kind"] == "store":
    _sid, _sname = _pend["id"], _pend.get("label", "")
    log_action(ACTION_STORE_DELETE, target_type="store", target_id=_sid,
               detail={"name": _sname}, user=user)
    try:
        delete_store(_sid)
        st.success(f"门店【{_sname}】及其关联数据已删除")
    except Exception as _e:
        st.error(f"删除失败：{_e}")
    st.rerun()

# ==================== 编辑弹窗 ====================
if st.session_state.edit_store_id is not None:
    all_stores = get_all_stores(active_only=False)
    edit_store = next((s for s in all_stores if s["id"] == st.session_state.edit_store_id), None)
    if edit_store:
        # 权限校验：非admin不能编辑非授权门店
        if not is_admin(user):
            allowed_ids = set(get_user_store_ids(user.get("id", 0)))
            if edit_store["id"] not in allowed_ids:
                st.error("无权限编辑该门店")
                st.session_state.edit_store_id = None
                st.stop()

        with st.expander(f"✏️ 编辑门店：{edit_store['name']}", expanded=True):
            edit_name = st.text_input("门店名称 *", value=edit_store.get("name", ""), key="edit_name")

            col_ep, col_ec = st.columns(2)
            ep_idx = PROVINCES.index(edit_store.get("province", "北京市")) if edit_store.get("province") in PROVINCES else 0
            edit_province = col_ep.selectbox("省份 *", PROVINCES[1:], index=max(ep_idx - 1, 0), key="edit_province")

            ec_opts = CITIES.get(edit_province, [""])
            ec_idx = ec_opts.index(edit_store.get("city", ec_opts[0])) if edit_store.get("city") in ec_opts else 0
            edit_city = col_ec.selectbox("城市 *", ec_opts, index=ec_idx, key="edit_city")

            st.divider()
            st.caption("📲 采购单推送配置（移动端）：填写后，每晚生成的采购单会自动推送到这些渠道")
            edit_owner_email = st.text_input(
                "老板邮箱（多个用逗号分隔）",
                value=edit_store.get("owner_email", "") or "",
                key="edit_owner_email",
                help="采购单邮件接收人；留空则不发邮件",
            )
            edit_wecom = st.text_input(
                "企业微信机器人 Webhook",
                value=edit_store.get("wecom_webhook", "") or "",
                key="edit_wecom",
                help="企微群机器人地址，采购单将推送到群；留空则不发企微",
            )

            st.divider()
            st.caption("🤖 自动预测配置")
            edit_auto_predict = st.checkbox(
                "参与自动预测（每日自动按配置预测并推送采购单）",
                value=bool(edit_store.get("auto_predict", 1)),
                key="edit_auto_predict",
                help="关闭后该门店不参与自动预测，仅可手工在预测配置页执行",
            )
            col_ep1, col_ep2 = st.columns(2)
            _cur_offset = int(edit_store.get("auto_predict_target_offset", 1))
            _offset_label = {"今天": 0, "明天": 1, "后天": 2}
            _cur_label = {0: "今天", 1: "明天", 2: "后天"}.get(_cur_offset, "明天")
            edit_target_label = col_ep1.selectbox(
                "预测目标日", options=list(_offset_label.keys()),
                index=list(_offset_label.keys()).index(_cur_label),
                key="edit_target_label",
            )
            edit_auto_predict_time = col_ep2.text_input(
                "每日预测时间", value=edit_store.get("auto_predict_time") or "06:00",
                key="edit_auto_predict_time",
                help="格式 HH:MM，如 06:00；到点后若当天未预测过则自动执行",
            )
            edit_alert_push = st.checkbox(
                "开启预警自动推送（命中预警时邮件/企微通知老板）",
                value=bool(edit_store.get("alert_push_enabled", 1)),
                key="edit_alert_push",
                help="关闭后该门店预警仅站内展示，不自动推送",
            )

            col_save, col_cancel = st.columns(2)
            if col_save.button("💾 保存", type="primary", key="save_edit"):
                if not edit_name.strip():
                    st.error("门店名称不能为空")
                elif not re.match(r'^\d{1,2}\s*:\s*\d{2}$', edit_auto_predict_time.strip()):
                    st.error("每日预测时间格式错误，请使用 HH:MM（如 06:00）")
                else:
                    try:
                        update_store(
                            st.session_state.edit_store_id, edit_name.strip(), edit_city, edit_province,
                            owner_email=edit_owner_email.strip(), wecom_webhook=edit_wecom.strip(),
                            auto_predict=1 if edit_auto_predict else 0,
                            auto_predict_target_offset=_offset_label[edit_target_label],
                            auto_predict_time=edit_auto_predict_time.strip() or "06:00",
                            alert_push_enabled=1 if edit_alert_push else 0,
                        )
                        log_action(ACTION_STORE_UPDATE, target_type="store", target_id=st.session_state.edit_store_id,
                                   detail={"name": edit_name.strip(), "province": edit_province, "city": edit_city}, user=user)
                        st.success("门店信息已更新")
                        st.session_state.edit_store_id = None
                        st.rerun()
                    except ValueError as e:
                        st.error(str(e))

            if col_cancel.button("❌ 取消", key="cancel_edit"):
                st.session_state.edit_store_id = None
                st.rerun()