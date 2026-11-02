"""用户管理页面（仅管理员可访问；key=user_manage）

管理员在此新建 / 编辑 / 删除账号，并直接分配角色与绑定门店。
渠道商无 user_manage 权限，进不来本页；其门店账号管理见「渠道门店账号」页。
账号创建者记入 users.authorized_by（渠道归属统计）。
"""

import datetime

import streamlit as st

from guibeicai.web._db import (
    init_db, get_all_users, add_user, update_user,
    update_user_password, delete_user, unbind_openid, get_all_stores,
    get_user_store_ids, set_user_store_ids, get_all_roles,
    get_user_by_phone,
)
from guibeicai.web._audit import (
    log_action, ACTION_USER_CREATE, ACTION_USER_UPDATE, ACTION_USER_DELETE,
)
from guibeicai.web._nav import render_nav
from guibeicai.web.components import arm_danger, render_danger_confirm

init_db()

st.set_page_config(page_title="用户管理", page_icon="👤", layout="wide")
render_nav()

from guibeicai.web._auth import require_auth, require_page_access

# ===== 登录校验 + 页面权限（仅管理员）=====
user = require_auth()
require_page_access("user_manage")

st.title("👤 用户管理")
st.caption("管理系统用户账号、角色与门店绑定")

# ===== 搜索 =====
search_key = st.text_input("🔍 搜索用户名", placeholder="输入关键字筛选", key="user_search")

# ===== 门店列表（用于绑定） =====
stores = get_all_stores(active_only=True)
store_options = {s["id"]: s["name"] for s in stores}

# 角色编码 -> 名称
_role_rows = get_all_roles()
role_name_by_code = {r["code"]: r["name"] for r in _role_rows}
role_codes = [r["code"] for r in _role_rows]

# ===== 新增用户（含角色选择） =====
with st.expander("➕ 新增用户", expanded=False):
    with st.form("add_user_form", clear_on_submit=True):
        col1, col2, col3 = st.columns([2, 2, 3])
        new_username = col1.text_input("用户名 *")
        new_password = col2.text_input("密码 *", type="password")
        new_role = col3.selectbox("角色 *", options=role_codes,
                                  format_func=lambda c: role_name_by_code.get(c, c),
                                  index=role_codes.index("merchant") if "merchant" in role_codes else 0,
                                  key="add_user_role")
        # 门店绑定（多选）
        new_store_ids = st.multiselect(
            "绑定门店", options=list(store_options.keys()),
            format_func=lambda i: store_options.get(i, "未知"),
            default=[], key="add_user_stores"
        )
        # 手机号（小程序微信绑定的匹配键；留空则该用户暂不能在小程序自助绑定）
        new_phone = st.text_input(
            "手机号", key="add_phone",
            help="用户在小程序首次登录时用此手机号匹配账号并自动绑定微信。"
                 "留空则该用户无法自助绑定（可后续在编辑里补）。"
        )
        # 账号有效期（按合同期限；留空表示长期有效）
        vf_col, vu_col = st.columns(2)
        new_valid_from = vf_col.date_input("生效日期", value=None, key="add_vf", help="留空=不限制生效时间")
        new_valid_until = vu_col.date_input("失效日期", value=None, key="add_vu", help="留空=长期有效")
        submitted = st.form_submit_button("创建用户", type="primary")
        if submitted:
            if not new_username or not new_password:
                st.error("用户名和密码不能为空")
            elif new_role == "merchant" and not new_store_ids:
                st.error("商家用户请至少绑定一个门店")
            else:
                ph = (new_phone or "").strip()
                if ph and (not ph.isdigit() or len(ph) != 11):
                    st.error("手机号格式不正确（应为 11 位数字）")
                elif ph and get_user_by_phone(ph):
                    st.error("该手机号已被其他账号使用")
                else:
                    vf = new_valid_from.isoformat() if new_valid_from else None
                    vu = new_valid_until.isoformat() if new_valid_until else None
                    if add_user(new_username, new_password, new_role, new_store_ids,
                                valid_from=vf, valid_until=vu, authorized_by=user["id"],
                                phone=ph or None):
                        log_action(ACTION_USER_CREATE, target_type="user", target_id=new_username,
                                   detail={"username": new_username, "role": new_role,
                                           "store_ids": list(new_store_ids), "phone": bool(ph)}, user=user)
                        st.success(f"用户 {new_username} 创建成功（角色：{role_name_by_code.get(new_role, new_role)}）")
                        st.rerun()
                    else:
                        st.error("创建失败，用户名或手机号可能已存在")

# ===== 用户列表 =====
st.divider()
st.subheader("用户列表")

users = get_all_users()

# 搜索过滤
if search_key:
    users = [u for u in users if search_key.lower() in u["username"].lower()]

if not users:
    st.info("暂无用户")
else:
    header_cols = st.columns([1.6, 1.4, 1.9, 0.9, 1.2, 0.7, 0.7])
    header_cols[0].write("**用户名**")
    header_cols[1].write("**手机号**")
    header_cols[2].write("**绑定门店**")
    header_cols[3].write("**角色**")
    header_cols[4].write("**微信**")
    header_cols[5].write("**编辑**")
    header_cols[6].write("**删除**")
    st.divider()

    for u in users:
        cols = st.columns([1.6, 1.4, 1.9, 0.9, 1.2, 0.7, 0.7])

        cols[0].write(u["username"])

        # 账号有效期状态（按合同期限管控）
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

        # 手机号（小程序微信绑定的匹配键）
        _phone = (u.get("phone") or "").strip()
        cols[1].write(_phone if _phone else "—")

        # 显示所有绑定门店
        u_store_ids = u.get("store_ids") or []
        u_store_display = "、".join(store_options.get(i, "未知") for i in u_store_ids) if u_store_ids else "未绑定"
        cols[2].write(u_store_display)

        cols[3].write(role_name_by_code.get(u["role"], u["role"]))

        # 微信绑定状态 + 解绑（openid 由用户在小程序用手机号自助绑定后自动写入，不在此手输）
        _openid = (u.get("openid") or "").strip()
        if _openid:
            cols[4].write(f"✅ 已绑 …{_openid[-6:]}")
            if cols[4].button("解绑", key=f"unbind_{u['id']}", help="解除该账号的微信绑定"):
                unbind_openid(u["id"])
                log_action(ACTION_USER_UPDATE, target_type="user", target_id=u["id"],
                           detail={"username": u["username"], "unbind_wechat": True}, user=user)
                st.success(f"已解除 {u['username']} 的微信绑定")
                st.rerun()
        else:
            cols[4].write("— 未绑")

        if cols[5].button("✏️", key=f"edit_{u['id']}", help="编辑用户"):
            st.session_state[f"edit_open_{u['id']}"] = not st.session_state.get(f"edit_open_{u['id']}", False)
            st.rerun()

        if cols[6].button("🗑", key=f"del_{u['id']}", help="删除用户"):
            if u["id"] == user["id"]:
                st.warning("不能删除当前登录用户")
            else:
                arm_danger("user", u["id"], u["username"],
                           warning=(f"确认删除用户「{u['username']}」？其门店绑定关系将一并解除，"
                                    f"该账号将无法再登录，且不可恢复。"))
                st.rerun()

        # 编辑区域
        if st.session_state.get(f"edit_open_{u['id']}", False):
            with st.form(key=f"edit_form_{u['id']}"):
                ecol1, ecol2, ecol3, ecol4, ecol5 = st.columns([2, 2, 3, 1.2, 1.2])
                edit_username = ecol1.text_input("用户名", value=u["username"], key=f"edit_uname_{u['id']}")
                edit_password = ecol2.text_input("密码", type="password", placeholder="留空不改", key=f"edit_pwd_{u['id']}")
                edit_role = ecol3.selectbox("角色", options=role_codes,
                                            format_func=lambda c: role_name_by_code.get(c, c),
                                            index=role_codes.index(u["role"]) if u["role"] in role_codes else 0,
                                            key=f"edit_role_{u['id']}")
                # 门店绑定（多选，独立一行避免拥挤）
                edit_store_ids = st.multiselect(
                    "绑定门店", options=list(store_options.keys()),
                    format_func=lambda i: store_options.get(i, "未知"),
                    default=u_store_ids, key=f"edit_stores_{u['id']}"
                )

                # 账号有效期（按合同期限）
                def _to_date(s):
                    if not s:
                        return None
                    try:
                        return datetime.date.fromisoformat(s)
                    except Exception:
                        return None
                evf_col, evu_col = st.columns(2)
                edit_valid_from = evf_col.date_input("生效日期", value=_to_date(u.get("valid_from")),
                                                    key=f"edit_vf_{u['id']}", help="留空=不限制生效时间")
                edit_valid_until = evu_col.date_input("失效日期", value=_to_date(u.get("valid_until")),
                                                     key=f"edit_vu_{u['id']}", help="留空=长期有效")

                # 手机号（小程序微信绑定的匹配键）+ 微信绑定状态（只读，不手输 openid）
                st.divider()
                edit_phone = st.text_input(
                    "手机号", value=(u.get("phone") or ""), key=f"edit_phone_{u['id']}",
                    help="用户在小程序用此手机号匹配账号并自动绑定微信；"
                         "清空则移除该匹配键（已建立的微信绑定不受影响）。"
                )
                _cur_oid = (u.get("openid") or "").strip()
                if _cur_oid:
                    st.caption(f"🟢 已绑定微信（…{_cur_oid[-6:]}）　更换方式：该用户在小程序端重新绑定，或在上方列表点「解绑」")
                else:
                    st.caption("⚪ 未绑定微信（用户首次在小程序用手机号自助绑定后自动写入）")

                edit_submitted = ecol4.form_submit_button("确认修改", type="primary")
                edit_cancel = ecol5.form_submit_button("取消")

                if edit_cancel:
                    st.session_state[f"edit_open_{u['id']}"] = False
                    st.rerun()

                if edit_submitted:
                    if edit_role == "merchant" and not edit_store_ids:
                        st.error("商家用户请至少绑定一个门店")
                    else:
                        new_phone = (edit_phone or "").strip()
                        _old_phone = (u.get("phone") or "").strip()
                        if new_phone and (not new_phone.isdigit() or len(new_phone) != 11):
                            st.error("手机号格式不正确（应为 11 位数字）")
                        elif new_phone and new_phone != _old_phone and get_user_by_phone(new_phone):
                            st.error("该手机号已被其他账号使用")
                        else:
                            e_changed = False
                            if edit_username != u["username"]:
                                update_user(u["id"], username=edit_username)
                                e_changed = True
                            if edit_password:
                                # 管理员重置他人密码后，强制该用户下次登录改密
                                update_user_password(u["id"], edit_password, force_change_pwd=1)
                                e_changed = True
                            if edit_role != u["role"]:
                                update_user(u["id"], role=edit_role)
                                e_changed = True

                            # 账号有效期（仅当发生变更时更新；空串表示清除为长期有效）
                            vf_str = edit_valid_from.isoformat() if edit_valid_from else ""
                            vu_str = edit_valid_until.isoformat() if edit_valid_until else ""
                            if (vf_str or None) != (u.get("valid_from") or None) or (vu_str or None) != (u.get("valid_until") or None):
                                update_user(u["id"], valid_from=vf_str, valid_until=vu_str)
                                e_changed = True

                            # 比较门店列表是否变化
                            old_ids = sorted(set(u_store_ids))
                            new_ids = sorted(set(int(x) for x in edit_store_ids if int(x) != 0))
                            if old_ids != new_ids:
                                set_user_store_ids(u["id"], edit_store_ids)
                                e_changed = True

                            # 手机号（小程序微信绑定匹配键）变更
                            if new_phone != _old_phone:
                                update_user(u["id"], phone=new_phone)
                                e_changed = True

                            if e_changed:
                                log_action(ACTION_USER_UPDATE, target_type="user", target_id=u["id"],
                                           detail={"username": edit_username, "changed_password": bool(edit_password),
                                                   "role": edit_role, "store_ids": new_ids,
                                                   "phone_changed": new_phone != _old_phone}, user=user)
                                st.success("修改成功")
                                st.session_state[f"edit_open_{u['id']}"] = False
                                st.rerun()
                            else:
                                st.info("没有修改")

    st.caption("💡 提示：商家角色可绑定多个门店；users.store_id 作为主门店兼容字段。渠道商的门店账号管理见「渠道门店账号」页。")

# ===== 删除二次确认 =====
_pend = render_danger_confirm()
if _pend and _pend["kind"] == "user":
    _uid, _uname = _pend["id"], _pend.get("label", "")
    if _uid == user["id"]:
        st.warning("不能删除当前登录用户")
    else:
        log_action(ACTION_USER_DELETE, target_type="user", target_id=_uid,
                   detail={"username": _uname}, user=user)
        try:
            delete_user(_uid)
            st.success(f"用户 {_uname} 已删除")
        except Exception as _e:
            st.error(f"删除失败：{_e}")
    st.rerun()
