"""角色管理页面（授权：role_manage 可访问）

职责：维护角色编码(code)、名称(name)、描述，以及该角色可访问的页面(pages)。
页面权限是角色定义的一部分，在此配置；用户绑定角色在「用户管理」/「渠道门店账号」页面。
"""

import streamlit as st
import json
from guibeicai.web._db import (
    init_db, get_all_roles, add_role, update_role, delete_role
)
from guibeicai.web._nav import render_nav
from guibeicai.web._pages import APP_PAGES, all_page_keys, page_label
from guibeicai.web.components import arm_danger, render_danger_confirm

init_db()

st.set_page_config(page_title="角色管理", page_icon="🔐", layout="wide")
render_nav()

from guibeicai.web._auth import require_auth, require_page_access

# 登录 + 页面权限校验
require_auth()
require_page_access("role_manage")

st.title("🔐 角色管理")
st.caption("维护角色编码、名称与可访问页面（页面权限在此配置）")


# ==================== 新增角色 ====================

with st.expander("➕ 新增角色", expanded=False):
    with st.form("add_role_form", clear_on_submit=True):
        col1, col2 = st.columns([1, 2])
        role_code = col1.text_input("角色编码 *", placeholder="如: finance（逻辑用，唯一不可重复）")
        role_name = col2.text_input("角色名称 *", placeholder="如: 财务专员（展示用，可改）")
        role_desc = st.text_input("描述", placeholder="角色说明")

        st.markdown("**可访问页面**")
        page_cols = st.columns(3)
        picked_pages = []
        for i, p in enumerate(APP_PAGES):
            with page_cols[i % 3]:
                if st.checkbox(f"{p['icon']} {p['label']}", value=False, key=f"add_page_{p['key']}"):
                    picked_pages.append(p["key"])

        submitted = st.form_submit_button("创建角色", type="primary")
        if submitted:
            if not role_code or not role_name:
                st.error("角色编码与角色名称均不能为空")
            else:
                pages = ["*"] if set(picked_pages) == set(all_page_keys()) else picked_pages
                if add_role(role_code.strip(), role_name.strip(), role_desc.strip(), pages):
                    st.success(f"角色 {role_name} 创建成功")
                    st.rerun()
                else:
                    st.error("创建失败，角色编码可能已存在")


# ==================== 角色列表 ====================

st.divider()
st.subheader("角色列表")

roles = get_all_roles()

if not roles:
    st.info("暂无角色")
else:
    hcols = st.columns([1.5, 2, 3, 3, 2])
    hcols[0].write("**编码**")
    hcols[1].write("**名称**")
    hcols[2].write("**描述**")
    hcols[3].write("**可访问页面**")
    hcols[4].write("**操作**")
    st.divider()

    for r in roles:
        cols = st.columns([1.5, 2, 3, 3, 2])
        edit_key = f"edit_role_{r['id']}"
        if edit_key not in st.session_state:
            st.session_state[edit_key] = False

        if not st.session_state[edit_key]:
            cols[0].write(r["code"])
            cols[1].write(r["name"])
            cols[2].write(r["description"] or "")
            pages = r.get("pages") or []
            cols[3].write("全部页面" if "*" in pages else "、".join(page_label(k) for k in pages) or "无")

            b1, b2 = cols[4].columns(2)
            if b1.button("✏️ 编辑", key=f"edit_btn_{r['id']}"):
                st.session_state[edit_key] = True
                st.rerun()
            if b2.button("🗑 删除", key=f"del_role_{r['id']}"):
                arm_danger("role", r["id"], r["name"],
                           warning=(f"确认删除角色「{r['name']}」？删除前需确保该角色下没有用户，"
                                    f"否则删除会被拒绝。此操作不可恢复。"))
                st.rerun()

        else:
            new_code = cols[0].text_input("编码", value=r["code"], key=f"edit_code_{r['id']}", label_visibility="collapsed")
            new_name = cols[1].text_input("名称", value=r["name"], key=f"edit_name_{r['id']}", label_visibility="collapsed")
            new_desc = cols[2].text_input("描述", value=r.get("description", ""), key=f"edit_desc_{r['id']}", label_visibility="collapsed")

            cur_pages = set(r.get("pages") or [])
            if "*" in cur_pages:
                cur_pages = set(all_page_keys())
            st.markdown("**可访问页面**")
            edit_page_cols = st.columns(3)
            edit_picked = []
            for i, p in enumerate(APP_PAGES):
                with edit_page_cols[i % 3]:
                    if st.checkbox(f"{p['icon']} {p['label']}", value=(p["key"] in cur_pages), key=f"edit_page_{r['id']}_{p['key']}"):
                        edit_picked.append(p["key"])

            b1, b2 = cols[4].columns(2)
            if b1.button("✅ 保存", key=f"confirm_{r['id']}"):
                new_pages = ["*"] if set(edit_picked) == set(all_page_keys()) else edit_picked
                ok = update_role(
                    r["id"],
                    code=new_code.strip(),
                    name=new_name.strip(),
                    description=new_desc.strip(),
                    pages=new_pages,
                )
                if ok:
                    st.session_state[edit_key] = False
                    st.success("已保存")
                    st.rerun()
                else:
                    st.error("保存失败，角色编码可能与其他角色重复")

            if b2.button("❌ 取消", key=f"cancel_{r['id']}"):
                st.session_state[edit_key] = False
                st.rerun()

        st.divider()

    st.caption("💡 提示：删除角色前需先在「用户管理」或「渠道门店账号」页面把该角色下的用户转移/删除。")

# ==================== 删除二次确认 ====================
_pend = render_danger_confirm()
if _pend and _pend["kind"] == "role":
    try:
        if delete_role(_pend["id"]):
            st.success(f"角色 {_pend.get('label', '')} 已删除")
        else:
            st.error("该角色下还有用户，无法删除（请先在「用户管理」或「渠道门店账号」页解绑）")
    except Exception as _e:
        st.error(f"删除失败：{_e}")
    st.rerun()
