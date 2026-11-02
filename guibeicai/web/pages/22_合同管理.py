"""合同管理页面（仅系统管理员可见）

功能：维护客户/渠道合同（编号、名称、金额、支付方式、甲乙方、签订/生效/到期日、附件、备注），
并可将合同有效期同步到关联账号，实现「按合同期限管控账号可用区间」。
"""

import os
import re
from pathlib import Path

import streamlit as st

from guibeicai.web._db import (
    init_db, get_all_contracts, get_contract, add_contract, update_contract,
    set_contract_attachment, delete_contract, sync_contract_to_user,
    get_all_users, get_user_by_id, get_tenant,
)
from guibeicai.web._audit import (
    log_action, ACTION_CONTRACT_CREATE, ACTION_CONTRACT_UPDATE,
    ACTION_CONTRACT_DELETE, ACTION_CONTRACT_SYNC,
)
from guibeicai.web._nav import render_nav
from guibeicai.web.components import page_header, section, empty_state, banner, arm_danger, render_danger_confirm

init_db()

st.set_page_config(page_title="合同管理", page_icon="📑", layout="wide")
render_nav()

from guibeicai.web._auth import require_auth, require_page_access

# 仅管理员可见
require_auth("admin")
require_page_access("contract_manage")

# 多租户：合同管理是运营方（默认单库）专属功能，租户内账号不可进入
if get_tenant() is not None:
    st.error("⚠️ 合同管理仅运营方可用，租户账号无权限")
    st.stop()

page_header("合同管理", "维护客户/渠道合同，并可将合同有效期同步到关联账号（按合同期限管控账号）", icon="📑")

# 附件存储目录（项目根 /data/contracts）
_CONTRACT_DIR = Path(__file__).resolve().parents[3] / "data" / "contracts"
_CONTRACT_DIR.mkdir(parents=True, exist_ok=True)


def _sanitize(filename: str) -> str:
    name = re.sub(r"[^\w一-龥.\-]", "_", filename or "file")
    return name[:80]


def _user_options():
    users = get_all_users()
    # value=用户id, label=用户名(角色)
    opts = {u["id"]: f"{u['username']}（{u['role']}）" for u in users}
    return opts


def _save_attachment(cid: int, uploaded) -> tuple:
    """保存上传附件到本地，返回 (path, original_name)。uploaded 为 st.file_uploader 返回对象。"""
    if not uploaded:
        return "", ""
    fname = _sanitize(uploaded.name)
    dest = _CONTRACT_DIR / f"{cid}_{fname}"
    with open(dest, "wb") as f:
        f.write(uploaded.getbuffer())
    rel = f"data/contracts/{dest.name}"
    return rel, uploaded.name


# ==================== 新增合同 ====================
with st.expander("➕ 新增合同", expanded=False):
    with st.form("add_contract_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        new_no = c1.text_input("合同编号 *", placeholder="如: HT-2026-001")
        new_name = c2.text_input("合同名称 *", placeholder="如: 某某渠道供货合同")
        c3, c4 = st.columns(2)
        new_amount = c3.number_input("合同金额(元)", min_value=0.0, value=0.0, step=100.0)
        new_pay = c4.text_input("支付方式", placeholder="如: 月结/预付/对公转账")
        c5, c6 = st.columns(2)
        new_pa = c5.text_input("甲方", placeholder="我方/平台方")
        new_pb = c6.text_input("乙方", placeholder="客户/渠道方")
        c7, c8, c9 = st.columns(3)
        new_sign = c7.date_input("签订日期", value=None)
        new_eff = c8.date_input("生效日期", value=None)
        new_exp = c9.date_input("到期日期", value=None)
        # 关联账号
        uopts = _user_options()
        new_linked = st.selectbox("关联账号（可选，用于同步有效期）",
                                 options=[None] + list(uopts.keys()),
                                 format_func=lambda x: "不关联" if x is None else uopts[x],
                                 index=0)
        new_sync = st.checkbox("创建后立即将合同有效期同步到关联账号", value=bool(new_linked))
        new_remark = st.text_area("备注", placeholder="补充说明")
        new_attach = st.file_uploader("合同附件（PDF/图片/文档）", type=["pdf", "png", "jpg", "jpeg", "doc", "docx", "xls", "xlsx"])

        submitted = st.form_submit_button("保存合同", type="primary")
        if submitted:
            if not new_no or not new_name:
                st.error("合同编号和名称为必填项")
            else:
                cid = add_contract(
                    contract_no=new_no, name=new_name, amount=new_amount,
                    payment_method=new_pay, party_a=new_pa, party_b=new_pb,
                    sign_date=new_sign.isoformat() if new_sign else "",
                    effective_date=new_eff.isoformat() if new_eff else "",
                    expiry_date=new_exp.isoformat() if new_exp else "",
                    linked_user_id=new_linked, remark=new_remark,
                )
                if new_attach:
                    rel, oname = _save_attachment(cid, new_attach)
                    if rel:
                        set_contract_attachment(cid, rel, oname)
                log_action(ACTION_CONTRACT_CREATE, target_type="contract", target_id=cid,
                           detail={"contract_no": new_no, "name": new_name,
                                   "linked_user_id": new_linked}, user=st.session_state.get("user"))
                msg = f"合同 {new_no} 创建成功"
                if new_linked and new_sync:
                    res = sync_contract_to_user(cid)
                    msg += f"；{res['msg']}"
                    if res["ok"]:
                        log_action(ACTION_CONTRACT_SYNC, target_type="contract", target_id=cid,
                                   detail={"linked_user_id": new_linked}, user=st.session_state.get("user"))
                st.success(msg)
                st.rerun()

# ==================== 合同列表 ====================
section("合同列表", icon="📋")

contracts = get_all_contracts()
if not contracts:
    empty_state("📑", "暂无合同", hint="点击上方「新增合同」录入第一份合同")
else:
    for ct in contracts:
        with st.container(border=True):
            col_main, col_op = st.columns([5, 1])
            with col_main:
                st.markdown(f"**{ct['contract_no']} · {ct['name']}**")
                meta = []
                if ct["amount"]:
                    meta.append(f"金额 ¥{ct['amount']:,.2f}")
                if ct["payment_method"]:
                    meta.append(f"支付方式：{ct['payment_method']}")
                if ct["party_a"] or ct["party_b"]:
                    meta.append(f"甲方：{ct['party_a'] or '—'} ／ 乙方：{ct['party_b'] or '—'}")
                if ct["effective_date"] or ct["expiry_date"]:
                    meta.append(f"有效期：{ct['effective_date'] or '—'} ~ {ct['expiry_date'] or '长期'}")
                if ct["linked_user_id"]:
                    lu = get_user_by_id(ct["linked_user_id"])
                    meta.append(f"关联账号：{lu['username'] if lu else '已删除'}")
                if ct["attachment_name"]:
                    meta.append(f"附件：{ct['attachment_name']}")
                if meta:
                    st.caption(" ｜ ".join(meta))
                if ct["remark"]:
                    st.caption(f"备注：{ct['remark']}")
            with col_op:
                if st.button("编辑", key=f"edit_ct_{ct['id']}"):
                    st.session_state[f"ct_edit_{ct['id']}"] = not st.session_state.get(f"ct_edit_{ct['id']}", False)
                    st.rerun()
                if ct["linked_user_id"]:
                    if st.button("同步有效期", key=f"sync_ct_{ct['id']}", help="将合同生效/到期日写入关联账号有效期"):
                        res = sync_contract_to_user(ct["id"])
                        if res["ok"]:
                            log_action(ACTION_CONTRACT_SYNC, target_type="contract", target_id=ct["id"],
                                       detail={"linked_user_id": ct["linked_user_id"]}, user=st.session_state.get("user"))
                            st.success(res["msg"])
                        else:
                            st.error(res["msg"])
                        st.rerun()
                if st.button("🗑 删除", key=f"del_ct_{ct['id']}"):
                    arm_danger(
                        "contract", ct["id"], ct.get("contract_no") or ct.get("name", ""),
                        warning=(f"确认删除合同「{ct.get('contract_no') or ct.get('name', '')}」？"
                                 + ("其附件文件将一并删除，" if ct.get("attachment_path") else "")
                                 + "此操作不可恢复。"),
                    )
                    st.rerun()

            # 附件下载
            if ct["attachment_path"] and ct["attachment_name"]:
                fpath = _CONTRACT_DIR / Path(ct["attachment_path"]).name
                if fpath.exists():
                    with open(fpath, "rb") as f:
                        st.download_button("📎 下载附件", f.read(), file_name=ct["attachment_name"],
                                           key=f"dl_ct_{ct['id']}")

            # 编辑区
            if st.session_state.get(f"ct_edit_{ct['id']}", False):
                with st.form(key=f"edit_ct_form_{ct['id']}"):
                    e1, e2 = st.columns(2)
                    e_no = e1.text_input("合同编号", value=ct["contract_no"], key=f"e_no_{ct['id']}")
                    e_name = e2.text_input("合同名称", value=ct["name"], key=f"e_name_{ct['id']}")
                    e3, e4 = st.columns(2)
                    e_amount = e3.number_input("合同金额(元)", min_value=0.0, value=float(ct["amount"] or 0),
                                               step=100.0, key=f"e_amt_{ct['id']}")
                    e_pay = e4.text_input("支付方式", value=ct["payment_method"] or "", key=f"e_pay_{ct['id']}")
                    e5, e6 = st.columns(2)
                    e_pa = e5.text_input("甲方", value=ct["party_a"] or "", key=f"e_pa_{ct['id']}")
                    e_pb = e6.text_input("乙方", value=ct["party_b"] or "", key=f"e_pb_{ct['id']}")
                    e7, e8, e9 = st.columns(3)
                    from datetime import date as _date
                    e_sign = e7.date_input("签订日期", value=_date.fromisoformat(ct["sign_date"]) if ct["sign_date"] else None, key=f"e_sign_{ct['id']}")
                    e_eff = e8.date_input("生效日期", value=_date.fromisoformat(ct["effective_date"]) if ct["effective_date"] else None, key=f"e_eff_{ct['id']}")
                    e_exp = e9.date_input("到期日期", value=_date.fromisoformat(ct["expiry_date"]) if ct["expiry_date"] else None, key=f"e_exp_{ct['id']}")
                    eopts = _user_options()
                    cur_linked = ct["linked_user_id"] if ct["linked_user_id"] in eopts else None
                    e_linked = st.selectbox("关联账号", options=[None] + list(eopts.keys()),
                                            format_func=lambda x: "不关联" if x is None else eopts[x],
                                            index=([None] + list(eopts.keys())).index(cur_linked) if cur_linked else 0,
                                            key=f"e_link_{ct['id']}")
                    e_sync = st.checkbox("保存后同步有效期到关联账号", value=False, key=f"e_sync_{ct['id']}")
                    e_remark = st.text_area("备注", value=ct["remark"] or "", key=f"e_rem_{ct['id']}")
                    e_attach = st.file_uploader("更换附件（留空不改动）", type=["pdf", "png", "jpg", "jpeg", "doc", "docx", "xls", "xlsx"], key=f"e_att_{ct['id']}")

                    esub = st.form_submit_button("保存修改", type="primary")
                    ecancel = st.form_submit_button("取消")
                    if ecancel:
                        st.session_state[f"ct_edit_{ct['id']}"] = False
                        st.rerun()
                    if esub:
                        update_contract(ct["id"], contract_no=e_no, name=e_name, amount=e_amount,
                                        payment_method=e_pay, party_a=e_pa, party_b=e_pb,
                                        sign_date=e_sign.isoformat() if e_sign else "",
                                        effective_date=e_eff.isoformat() if e_eff else "",
                                        expiry_date=e_exp.isoformat() if e_exp else "",
                                        linked_user_id=e_linked, remark=e_remark)
                        if e_attach:
                            rel, oname = _save_attachment(ct["id"], e_attach)
                            if rel:
                                set_contract_attachment(ct["id"], rel, oname)
                        log_action(ACTION_CONTRACT_UPDATE, target_type="contract", target_id=ct["id"],
                                   detail={"contract_no": e_no}, user=st.session_state.get("user"))
                        msg = "合同已更新"
                        if e_linked and e_sync:
                            res = sync_contract_to_user(ct["id"])
                            msg += f"；{res['msg']}"
                            if res["ok"]:
                                log_action(ACTION_CONTRACT_SYNC, target_type="contract", target_id=ct["id"],
                                           detail={"linked_user_id": e_linked}, user=st.session_state.get("user"))
                        st.session_state[f"ct_edit_{ct['id']}"] = False
                        st.success(msg)
                        st.rerun()

# ==================== 删除二次确认 ====================
_pend = render_danger_confirm()
if _pend and _pend["kind"] == "contract":
    _ctid = _pend["id"]
    _ct = get_contract(_ctid)
    if _ct and _ct.get("attachment_path"):
        try:
            _fp = _CONTRACT_DIR / Path(_ct["attachment_path"]).name
            if _fp.exists():
                _fp.unlink()
        except Exception:
            pass
    try:
        delete_contract(_ctid)
        log_action(ACTION_CONTRACT_DELETE, target_type="contract", target_id=_ctid,
                   detail={"contract_no": _pend.get("label", "")}, user=st.session_state.get("user"))
        st.success("合同已删除")
    except Exception as _e:
        st.error(f"删除失败：{_e}")
    st.rerun()

st.divider()
st.caption("💡 合同仅管理员可见。将合同生效/到期日「同步到关联账号」后，该账号在合同期限外将无法登录（详见账号有效期规则）。")
