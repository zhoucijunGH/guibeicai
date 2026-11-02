"""渠道门店与供货：渠道商（配送商）专属工作区

定位：
- 渠道商在系统中录入其负责的旗下门店；
- 并为每个门店配置「食材供货范围」——即该门店需求（门店上传/自动生成的食材）中，
  渠道商实际供货的品类子集。「门店需多种、渠道只供几样」通过 store_supply_scope 表达。

多租户隔离：
- 渠道账号只能看到自己 user_stores 绑定的门店；新建门店会即时绑定到当前账号。
- 管理员可进入本页做全局监督（roles.pages 含 '*'，不受渠道隔离限制）。

说明：本页为「可写」页面，渠道商角色已从只读升级为可写（2026-09-09 重构决策）。
"""

import re

import streamlit as st

from guibeicai.web._nav import render_nav
from guibeicai.web._db import (
    init_db, normalize_name,
    get_accessible_stores, upsert_store, add_user_store,
    get_ingredients_by_store,
    get_store_supply_scope, get_store_supply_scope_norms, set_store_supply_scope,
)
from guibeicai.web._audit import log_action, ACTION_STORE_CREATE, ACTION_SUPPLY_SCOPE
from guibeicai.web.components import page_header, section, empty_state, metric_card, banner

init_db()

from guibeicai.web._auth import require_auth, require_page_access, is_admin

user = require_auth()
require_page_access("channel_store")

st.set_page_config(page_title="渠道门店与供货", page_icon="🏬", layout="wide")
render_nav()

page_header("渠道门店与供货",
            "录入您负责的门店，并为每个门店配置实际供货的食材范围（门店可能需多种，您可只供几样）",
            icon="🏬")

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

# session_state 初始化（省市联动选择）
for k, v in {"cs_province": "全部", "cs_city": "全部"}.items():
    if k not in st.session_state:
        st.session_state[k] = v

is_admin_user = is_admin(user)
is_channel = (user.get("role") == "channel")

# ==================== 新增门店（渠道账号自助录入，即时绑定多租户） ====================
with st.expander("➕ 新增门店", expanded=False):
    new_name = st.text_input("门店名称 *", key="cs_new_name")

    col_p, col_c = st.columns(2)
    p_idx = PROVINCES.index(st.session_state.cs_province)
    new_p = col_p.selectbox("省份 *", PROVINCES, index=p_idx, key="cs_sb_province")

    if new_p != st.session_state.cs_province:
        st.session_state.cs_province = new_p
        st.session_state.cs_city = "全部"
        st.rerun()

    city_opts = CITIES.get(st.session_state.cs_province, ["全部"])
    c_idx = city_opts.index(st.session_state.cs_city) if st.session_state.cs_city in city_opts else 0
    new_c = col_c.selectbox("城市 *", city_opts, index=c_idx, key="cs_sb_city")

    if new_c != st.session_state.cs_city:
        st.session_state.cs_city = new_c

    st.divider()
    st.caption("📲 采购单推送配置（移动端）")
    new_owner_email = st.text_input("老板邮箱（多个用逗号分隔）", key="cs_new_owner_email")
    new_wecom = st.text_input("企业微信机器人 Webhook", key="cs_new_wecom")

    st.divider()
    st.caption("🤖 自动预测配置")
    new_auto_predict = st.checkbox("参与自动预测（每日自动按配置预测并推送采购单）",
                                   value=True, key="cs_new_auto_predict",
                                   help="关闭后该门店不参与自动预测，仅可手工在预测配置页执行")
    col_np1, col_np2 = st.columns(2)
    target_opts = {"今天": 0, "明天": 1, "后天": 2}
    new_target_label = col_np1.selectbox("预测目标日", options=list(target_opts.keys()),
                                         index=1, key="cs_new_target_label")
    new_auto_predict_time = col_np2.text_input("每日预测时间", value="06:00", key="cs_new_auto_predict_time",
                                               help="格式 HH:MM，如 06:00；到点后若当天未预测过则自动执行")
    new_alert_push = st.checkbox("开启预警自动推送（命中预警时邮件/企微通知老板）",
                                 value=True, key="cs_new_alert_push",
                                 help="关闭后该门店预警仅站内展示，不自动推送")

    if st.button("创建门店", type="primary", key="cs_btn_create"):
        if not new_name or not new_name.strip():
            st.error("门店名称不能为空")
        elif st.session_state.cs_province == "全部" or st.session_state.cs_city == "全部":
            st.error("请选择省份和城市")
        elif not re.match(r'^\d{1,2}\s*:\s*\d{2}$', new_auto_predict_time.strip()):
            st.error("每日预测时间格式错误，请使用 HH:MM（如 06:00）")
        else:
            sid = upsert_store(new_name.strip(), st.session_state.cs_city, st.session_state.cs_province,
                              owner_email=new_owner_email.strip(), wecom_webhook=new_wecom.strip(),
                              auto_predict=1 if new_auto_predict else 0,
                              auto_predict_target_offset=target_opts[new_target_label],
                              auto_predict_time=new_auto_predict_time.strip() or "06:00",
                              alert_push_enabled=1 if new_alert_push else 0)
            # 多租户隔离：新建门店即时绑定到当前账号
            add_user_store(user["id"], sid)
            log_action(ACTION_STORE_CREATE, target_type="store", target_id=sid,
                       detail={"name": new_name.strip(), "province": st.session_state.cs_province,
                               "city": st.session_state.cs_city, "bound_to": user["id"]}, user=user)
            st.success(f"门店【{new_name.strip()}】已创建并绑定到您的账号")
            st.session_state.cs_province = "全部"
            st.session_state.cs_city = "全部"
            st.rerun()

# ==================== 门店范围（多租户隔离） ====================
stores = get_accessible_stores(user, active_only=True)
if not stores:
    empty_state("🏪", "暂无可管理的门店", "请先使用上方「新增门店」录入您负责的门店。")
    st.stop()

store_names = {s["id"]: s["name"] for s in stores}
sel_store_id = st.selectbox("选择门店", options=list(store_names.keys()),
                            format_func=lambda sid: store_names[sid], key="cs_store_sel")
store = next(s for s in stores if s["id"] == sel_store_id)

# 切换门店时重置手工补录缓存
if st.session_state.get("_cs_last_store") != sel_store_id:
    st.session_state["_cs_last_store"] = sel_store_id
    st.session_state["cs_custom"] = []

# ==================== 供货范围配置 ====================
section(f"「{store['name']}」供货范围配置", icon="🥬")

ings = get_ingredients_by_store(store["id"])
current_norms = get_store_supply_scope_norms(store["id"])

# 门店食材 -> 选项（以「展示名（单位）」做唯一标签，避免同名不同单位冲突）
opts = []
norm_by_label = {}
store_norm_set = set()
for ing in ings:
    label = f"{ing.get('display_name') or ing.get('name')}（{ing.get('base_unit') or '份'}）"
    opts.append(label)
    nn = normalize_name(ing.get("name") or "")
    store_norm_set.add(nn)
    norm_by_label[label] = (nn, ing.get("display_name") or ing.get("name"))

defaults = [label for label in opts if norm_by_label[label][0] in current_norms]

selected = st.multiselect(
    "勾选您实际供货的食材（门店需求中您不勾选的品类，将作为未覆盖缺口单独提示）",
    options=opts, default=defaults, key="cs_supply_multiselect",
    help="仅列出该门店已上传/自动生成的食材。如需供货门店暂无的食材，可用下方「手工补录」。",
)

# 手工补录：门店暂无该食材、但渠道确需供货的品类（待后续门店匹配）
custom_items = st.session_state.get("cs_custom", [])
with st.expander("➕ 手工补录其他供货食材（门店暂无此食材时）", expanded=bool(custom_items)):
    st.caption("用于提前登记渠道可供货、但门店尚未建档的食材；门店后续上传同名食材后将自动匹配。")
    custom_name = st.text_input("食材名称", key="cs_custom_name", placeholder="如：螺丝椒")
    if st.button("添加到供货范围", key="cs_custom_add"):
        nm = (custom_name or "").strip()
        if not nm:
            st.error("请输入食材名称")
        else:
            nn = normalize_name(nm)
            # 去重：不与已选项、已有手工项重复
            dup = any(norm_by_label[l][0] == nn for l in selected) or any(c[0] == nn for c in custom_items)
            if dup:
                st.warning(f"「{nm}」已在供货范围内，无需重复添加")
            else:
                custom_items.append((nn, nm))
                st.session_state["cs_custom"] = custom_items
                st.rerun()
    if custom_items:
        st.write("已补录（待门店匹配）：" + "、".join(f"`{n}`" for _, n in custom_items))
        if st.button("清空补录", key="cs_custom_clear"):
            st.session_state["cs_custom"] = []
            st.rerun()

# 指标卡：覆盖情况一览
total = len(ings)
supplied = len(selected) + len(custom_items)
uncovered = total - len(selected)
c1, c2, c3 = st.columns(3)
with c1:
    metric_card("门店食材需求", f"{total}", "该门店已建档的食材种类", variant="info")
with c2:
    metric_card("您供货", f"{supplied}", "已在供货范围内的品类（含手工补录）", variant="success")
with c3:
    metric_card("未覆盖缺口", f"{max(uncovered, 0)}", "门店需、但您未勾选供货的品类", variant="default")

if max(uncovered, 0) > 0:
    banner("info", f"该门店有 **{max(uncovered, 0)}** 种食材您未勾选供货，将在渠道工作台中作为「未覆盖需求」单独提示，"
                   f"由您线下或经 ERP 另行处理。")

if st.button("保存供货范围", type="primary", key="cs_save_scope"):
    items = [norm_by_label[label] for label in selected] + list(custom_items)
    set_store_supply_scope(store["id"], items)
    log_action(ACTION_SUPPLY_SCOPE, target_type="store", target_id=store["id"],
               detail={"supplied": supplied, "total": total, "uncovered": max(uncovered, 0),
                       "custom": len(custom_items)}, user=user)
    st.success(f"已保存「{store['name']}」的供货范围（共 {supplied} 种）")
    st.session_state["cs_custom"] = []
    st.rerun()
