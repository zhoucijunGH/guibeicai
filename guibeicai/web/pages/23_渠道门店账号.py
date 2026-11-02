"""渠道门店账号页面（key=channel_store_account，渠道与管理员双视图）

- 渠道商：仅管理「自身权限范围内」的门店（商家用户）账号；角色固定为「商家用户」，
  门店下拉仅限本渠道可访问门店，看不到管理员 / 渠道账号，杜绝越权与提权。
- 管理员：可见本租户全部门店账号，可分配任意角色与任意门店（运营方监管视角）。

账号创建者记入 users.authorized_by，用于「渠道归属统计」。
"""

import datetime
from collections import Counter

import streamlit as st

from guibeicai.web._db import (
    init_db, get_all_users, add_user, update_user,
    update_user_password, delete_user, get_accessible_stores,
    get_user_store_ids, set_user_store_ids, get_all_roles,
    get_visible_store_accounts,
)
from guibeicai.web._audit import (
    log_action, ACTION_USER_CREATE, ACTION_USER_UPDATE, ACTION_USER_DELETE,
)
from guibeicai.web._nav import render_nav
from guibeicai.web.components import arm_danger, render_danger_confirm

init_db()

st.set_page_config(page_title="渠道门店账号", page_icon="🏬", layout="wide")
render_nav()

from guibeicai.web._auth import require_auth, require_page_access, is_admin

# ===== 登录校验 + 页面权限 =====
user = require_auth()
require_page_access("channel_store_account")

is_admin_user = is_admin(user)

st.title("🏬 渠道门店账号")
if is_admin_user:
    st.caption("管理员视图：可查看本租户全部门店账号，并分配任意角色 / 门店（监管视角）。")
else:
    st.caption("渠道商视图：仅管理您权限范围内的门店账号；角色固定为「商家用户」，门店仅限本渠道。")

# ===== 门店下拉（管理员=全部，渠道=自身） =====
stores = get_accessible_stores(user, active_only=True)
store_options = {s["id"]: s["name"] for s in stores}

# 角色编码 -> 名称（管理员可分配全角色；渠道固定商家用户）
_role_rows = get_all_roles()
role_name_by_code = {r["code"]: r["name"] for r in _role_rows}

# ===== 渠道归属统计 =====
accounts = get_visible_store_accounts(user)  # 管理员=全部商家用户；渠道=自身门店商家用户
if is_admin_user:
    st.metric("本租户门店账号总数", len(accounts))
    owner_counter = Counter(a.get("authorized_by") for a in accounts)
    if owner_counter:
        with st.expander("📊 按创建者归属统计", expanded=False):
            owner_names = {u["id"]: u["username"] for u in get_all_users()}
            for oid, cnt in sorted(owner_counter.items(), key=lambda kv: -kv[1]):
                label = owner_names.get(oid, f"未知(id={oid})") if oid else "未知归属"
                st.write(f"- {label}：{cnt} 个")
else:
    my_authorized = sum(1 for a in accounts if a.get("authorized_by") == user["id"])
    st.metric("本渠道已授权门店账号", my_authorized)

# ===== 搜索 =====
search_key = st.text_input("🔍 搜索用户名", placeholder="输入关键字筛选", key="csa_search")

# ===== 新增用户 =====
with st.expander("➕ 新增门店账号", expanded=False):
    with st.form("csa_add_form", clear_on_submit=True):
        col1, col2, col3 = st.columns([2, 2, 3])
        new_username = col1.text_input("用户名 *")
        new_password = col2.text_input("密码 *", type="password")
        if is_admin_user:
            role_codes = [r["code"] for r in _role_rows]
            new_role = col3.selectbox("角色 *", options=role_codes,
                                      format_func=lambda c: role_name_by_code.get(c, c),
                                      index=role_codes.index("merchant") if "merchant" in role_codes else 0,
                                      key="csa_add_role")
        else:
            new_role = "merchant"
            col3.text_input("角色", value="商家用户", disabled=True, key="csa_add_role_ro")
        new_store_ids = st.multiselect(
            "绑定门店", options=list(store_options.keys()),
            format_func=lambda i: store_options.get(i, "未知"),
            default=[], key="csa_add_stores"
        )
        vf_col, vu_col = st.columns(2)
        new_valid_from = vf_col.date_input("生效日期", value=None, key="csa_add_vf", help="留空=不限制生效时间")
        new_valid_until = vu_col.date_input("失效日期", value=None, key="csa_add_vu", help="留空=长期有效")
        submitted = st.form_submit_button("创建账号", type="primary")
        if submitted:
            if not new_username or not new_password:
                st.error("用户名和密码不能为空")
            elif new_role == "merchant" and not new_store_ids:
                st.error("商家用户请至少绑定一个门店")
            else:
                vf = new_valid_from.isoformat() if new_valid_from else None
                vu = new_valid_until.isoformat() if new_valid_until else None
                if add_user(new_username, new_password, new_role, new_store_ids,
                            valid_from=vf, valid_until=vu, authorized_by=user["id"]):
                    log_action(ACTION_USER_CREATE, target_type="user", target_id=new_username,
                               detail={"username": new_username, "role": new_role,
                                       "store_ids": list(new_store_ids)}, user=user)
                    st.success(f"账号 {new_username} 创建成功（角色：{role_name_by_code.get(new_role, new_role)}）")
                    st.rerun()
                else:
                    st.error("创建失败，用户名可能已存在")

# ===== 账号列表 =====
st.divider()
st.subheader("门店账号列表")

users = accounts  # 已按角色/门店隔离

if search_key:
    users = [u for u in users if search_key.lower() in u["username"].lower()]

if not users:
    st.info("暂无门店账号")
else:
    header_cols = st.columns([2, 2.5, 1.2, 1, 1])
    header_cols[0].write("**用户名**")
    header_cols[1].write("**绑定门店**")
    header_cols[2].write("**角色**")
    header_cols[3].write("**编辑**")
    header_cols[4].write("**删除**")
    st.divider()

    for u in users:
        cols = st.columns([2, 2.5, 1.2, 1, 1])
        cols[0].write(u["username"])

        vf = (u.get("valid_from") or "").strip()
        vu = (u.get("valid_until") or "").strip()
        if vf or vu:
            today = datetime.date.today().isoformat()
            if vu and today > vu:
                cols[0].caption(f"⛔ 已过期({vu})")
            elif vf and today < vf:
                cols[0].caption(f"⏳ 未生效({vf})")
            else:
                cols[0].caption(f"📅 {vf or '—'}~{vu or '长期'}")
        else:
            cols[0].caption("♾️ 长期有效")

        u_store_ids = u.get("store_ids") or []
        u_store_display = "、".join(store_options.get(i, "未知") for i in u_store_ids) if u_store_ids else "未绑定"
        cols[1].write(u_store_display)

        cols[2].write(role_name_by_code.get(u["role"], u["role"]))

        if cols[3].button("✏️", key=f"csa_edit_{u['id']}", help="编辑账号"):
            st.session_state[f"csa_edit_open_{u['id']}"] = not st.session_state.get(f"csa_edit_open_{u['id']}", False)
            st.rerun()

        if cols[4].button("🗑", key=f"csa_del_{u['id']}", help="删除账号"):
            if not is_admin_user and u.get("role") != "merchant":
                st.warning("无权删除该账号")
            elif u["id"] == user["id"]:
                st.warning("不能删除当前登录账号")
            else:
                arm_danger("channel_user", u["id"], u["username"],
                           warning=(f"确认删除账号「{u['username']}」？其门店绑定关系将一并解除，"
                                    f"该账号将无法再登录，且不可恢复。"))
                st.rerun()

        if st.session_state.get(f"csa_edit_open_{u['id']}", False):
            with st.form(key=f"csa_edit_form_{u['id']}"):
                ecol1, ecol2, ecol3, ecol4, ecol5 = st.columns([2, 2, 3, 1.2, 1.2])
                edit_username = ecol1.text_input("用户名", value=u["username"], key=f"csa_euname_{u['id']}")
                edit_password = ecol2.text_input("密码", type="password", placeholder="留空不改", key=f"csa_epwd_{u['id']}")

                if is_admin_user:
                    edit_role = ecol3.selectbox("角色", options=[r["code"] for r in _role_rows],
                                                format_func=lambda c: role_name_by_code.get(c, c),
                                                index=[r["code"] for r in _role_rows].index(u["role"]) if u["role"] in [r["code"] for r in _role_rows] else 0,
                                                key=f"csa_erole_{u['id']}")
                else:
                    edit_role = "merchant"
                    ecol3.text_input("角色", value="商家用户", disabled=True, key=f"csa_erole_ro_{u['id']}")

                # 绑定门店（独立一行，避免与角色同列拥挤）
                edit_store_ids = st.multiselect(
                    "绑定门店", options=list(store_options.keys()),
                    format_func=lambda i: store_options.get(i, "未知"),
                    default=u_store_ids, key=f"csa_estores_{u['id']}"
                )

                evf_col, evu_col = st.columns(2)
                def _to_date(s):
                    if not s:
                        return None
                    try:
                        return datetime.date.fromisoformat(s)
                    except Exception:
                        return None
                edit_valid_from = evf_col.date_input("生效日期", value=_to_date(u.get("valid_from")),
                                                     key=f"csa_evf_{u['id']}", help="留空=不限制生效时间")
                edit_valid_until = evu_col.date_input("失效日期", value=_to_date(u.get("valid_until")),
                                                      key=f"csa_evu_{u['id']}", help="留空=长期有效")

                edit_submitted = ecol4.form_submit_button("确认修改", type="primary")
                edit_cancel = ecol5.form_submit_button("取消")

                if edit_cancel:
                    st.session_state[f"csa_edit_open_{u['id']}"] = False
                    st.rerun()

                if edit_submitted:
                    if edit_role == "merchant" and not edit_store_ids:
                        st.error("商家用户请至少绑定一个门店")
                    else:
                        e_changed = False
                        if edit_username != u["username"]:
                            update_user(u["id"], username=edit_username)
                            e_changed = True
                        if edit_password:
                            update_user_password(u["id"], edit_password, force_change_pwd=1)
                            e_changed = True
                        if is_admin_user and edit_role != u["role"]:
                            update_user(u["id"], role=edit_role)
                            e_changed = True
                        vf_str = edit_valid_from.isoformat() if edit_valid_from else ""
                        vu_str = edit_valid_until.isoformat() if edit_valid_until else ""
                        if (vf_str or None) != (u.get("valid_from") or None) or (vu_str or None) != (u.get("valid_until") or None):
                            update_user(u["id"], valid_from=vf_str, valid_until=vu_str)
                            e_changed = True
                        old_ids = sorted(set(u_store_ids))
                        new_ids = sorted(set(int(x) for x in edit_store_ids if int(x) != 0))
                        if old_ids != new_ids:
                            set_user_store_ids(u["id"], edit_store_ids)
                            e_changed = True
                        if e_changed:
                            log_action(ACTION_USER_UPDATE, target_type="user", target_id=u["id"],
                                       detail={"username": edit_username, "changed_password": bool(edit_password),
                                               "store_ids": new_ids}, user=user)
                            st.success("修改成功")
                            st.session_state[f"csa_edit_open_{u['id']}"] = False
                            st.rerun()
                        else:
                            st.info("没有修改")

    st.caption("💡 提示：商家用户可绑定多个门店；本页账号由「创建者」归属（渠道归属统计）。")

# ===== 删除二次确认 =====
_pend = render_danger_confirm()
if _pend and _pend["kind"] == "channel_user":
    _uid, _uname = _pend["id"], _pend.get("label", "")
    if _uid == user["id"]:
        st.warning("不能删除当前登录账号")
    else:
        log_action(ACTION_USER_DELETE, target_type="user", target_id=_uid,
                   detail={"username": _uname}, user=user)
        try:
            delete_user(_uid)
            st.success(f"账号 {_uname} 已删除")
        except Exception as _e:
            st.error(f"删除失败：{_e}")
    st.rerun()
