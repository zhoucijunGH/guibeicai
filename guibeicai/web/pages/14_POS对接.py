"""桂备菜 - POS/收银数据对接配置页（#商业化：通用可配置数据源）

页面可配置：输入 POS/收银 接口地址 + 字段映射 + 认证即可完成对接。
支持多种数据源（HTTP JSON / CSV 下载 / Webhook 推送），拉取后归一化写入 actual_sales，
与现有手工/小程序录入共用同一张表，手工录入作为 POS 异常兜底。
"""

from datetime import date

import streamlit as st

from guibeicai.web._auth import require_auth, require_page_access, is_admin
from guibeicai.web._nav import render_nav
from guibeicai.web.components import page_header, section, banner, arm_danger, render_danger_confirm
from guibeicai.web._db import (
    get_all_stores, get_user_store_ids,
    get_pos_sources_by_store, upsert_pos_source, delete_pos_source,
    get_pos_source, get_pos_sync_log, parse_pos_schedule,
)
from guibeicai.data_import import pos_client

user = require_auth()
require_page_access("pos_integration")

st.set_page_config(page_title="桂备菜 - POS对接", page_icon="🔌", layout="wide")
render_nav()

page_header("POS/收银数据对接", "配置通用数据源接口，自动拉取销量，消灭每日手工录入", icon="🔌")
st.caption("💡 支持 HTTP JSON / CSV 下载 / Webhook 推送 三类数据源；映射按门店独立配置。手工录入始终保留作为兜底。")

# ---------------- 会话状态初始化 ----------------
for _k in ("pos_edit_id", "pos_test_result", "pos_pull_result"):
    if _k not in st.session_state:
        st.session_state[_k] = None

WIDGET_KEYS = ["pos_name", "pos_type", "pos_url", "pos_auth", "pos_apikey_header",
               "pos_apikey", "pos_bearer", "pos_basic_user", "pos_basic_pass",
               "pos_dish", "pos_qty", "pos_date", "pos_amount", "pos_items_path", "pos_date_fmt",
               "pos_auto_create", "pos_schedule", "pos_enabled",
               "pos_method", "pos_content_type", "pos_headers", "pos_params", "pos_body",
               "pos_success_path", "pos_success_value", "pos_message_path"]


def _parse_kv_text(text: str) -> dict:
    """解析 'key=value' 多行文本为 dict；忽略空行与 # 注释。"""
    out = {}
    if not text:
        return out
    for line in str(text).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out[k] = v
    return out


def _clear_form():
    for _k in WIDGET_KEYS:
        if _k in ("pos_auto_create", "pos_enabled"):
            st.session_state[_k] = True
        elif _k in ("pos_method", "pos_content_type"):
            st.session_state[_k] = "GET" if _k == "pos_method" else "json"
        else:
            st.session_state[_k] = ""
    st.session_state["pos_edit_id"] = None


def _load_into_form(src: dict):
    st.session_state["pos_edit_id"] = src["id"]
    st.session_state["pos_name"] = src.get("name", "")
    st.session_state["pos_type"] = src.get("source_type", "http_json")
    st.session_state["pos_url"] = src.get("base_url", "")
    st.session_state["pos_auth"] = src.get("auth_type", "none")
    # [S3] 凭据不回显明文：编辑时敏感字段留空，表单以「留空表示不修改」语义保留原值
    ac = src.get("auth_config") if isinstance(src.get("auth_config"), dict) else {}
    st.session_state["pos_apikey_header"] = ac.get("header", "X-API-Key")
    st.session_state["pos_apikey"] = ""
    st.session_state["pos_bearer"] = ""
    st.session_state["pos_basic_user"] = ""
    st.session_state["pos_basic_pass"] = ""
    fm = src.get("field_mapping") or {}
    st.session_state["pos_dish"] = fm.get("dish", "dish_name")
    st.session_state["pos_qty"] = fm.get("qty", "qty")
    st.session_state["pos_date"] = fm.get("date", "")
    st.session_state["pos_amount"] = fm.get("amount", "")
    st.session_state["pos_items_path"] = fm.get("items_path", "")
    st.session_state["pos_date_fmt"] = fm.get("date_format", "%Y-%m-%d")
    st.session_state["pos_auto_create"] = fm.get("auto_create_dish", True)
    st.session_state["pos_schedule"] = src.get("schedule", "")
    st.session_state["pos_enabled"] = bool(src.get("enabled", True))
    # 请求报文配置
    rc = src.get("request_config") if isinstance(src.get("request_config"), dict) else {}
    st.session_state["pos_method"] = (rc.get("method") or "GET").upper()
    st.session_state["pos_content_type"] = (rc.get("content_type") or "json").lower()
    st.session_state["pos_headers"] = "\n".join(f"{k}={v}" for k, v in (rc.get("headers") or {}).items())
    st.session_state["pos_params"] = "\n".join(f"{k}={v}" for k, v in (rc.get("params") or {}).items())
    st.session_state["pos_body"] = rc.get("body") or ""
    fm = src.get("field_mapping") or {}
    st.session_state["pos_success_path"] = fm.get("success_path", "")
    st.session_state["pos_success_value"] = ("" if fm.get("success_value") is None else str(fm.get("success_value")))
    st.session_state["pos_message_path"] = fm.get("message_path", "")


def _gather_dict():
    """从当前控件值组装一份数据源配置 dict（未入库草稿也可用于测试/拉取）。"""
    auth_type = st.session_state.get("pos_auth", "none")
    auth_config = {}
    if auth_type == "api_key":
        auth_config = {"header": st.session_state.get("pos_apikey_header", "X-API-Key"),
                       "api_key": st.session_state.get("pos_apikey", "")}
    elif auth_type == "bearer":
        auth_config = {"token": st.session_state.get("pos_bearer", "")}
    elif auth_type == "basic":
        auth_config = {"user": st.session_state.get("pos_basic_user", ""),
                       "pass": st.session_state.get("pos_basic_pass", "")}
    field_mapping = {
        "dish": st.session_state.get("pos_dish", "dish_name"),
        "qty": st.session_state.get("pos_qty", "qty"),
        "date": st.session_state.get("pos_date", ""),
        "amount": st.session_state.get("pos_amount", ""),
        "items_path": st.session_state.get("pos_items_path", ""),
        "date_format": st.session_state.get("pos_date_fmt", "%Y-%m-%d"),
        "auto_create_dish": bool(st.session_state.get("pos_auto_create", True)),
        "success_path": st.session_state.get("pos_success_path", "").strip(),
        "success_value": st.session_state.get("pos_success_value", "").strip(),
        "message_path": st.session_state.get("pos_message_path", "").strip(),
    }
    request_config = {
        "method": st.session_state.get("pos_method", "GET").upper(),
        "content_type": st.session_state.get("pos_content_type", "json").lower(),
        "headers": _parse_kv_text(st.session_state.get("pos_headers", "")),
        "params": _parse_kv_text(st.session_state.get("pos_params", "")),
        "body": st.session_state.get("pos_body", "").strip(),
    }
    return {
        "name": st.session_state.get("pos_name", ""),
        "source_type": st.session_state.get("pos_type", "http_json"),
        "base_url": st.session_state.get("pos_url", ""),
        "auth_type": auth_type,
        "auth_config": auth_config,
        "field_mapping": field_mapping,
        "request_config": request_config,
        "schedule": st.session_state.get("pos_schedule", ""),
        "enabled": bool(st.session_state.get("pos_enabled", True)),
    }


# ---------------- 门店选择 ----------------
all_stores = get_all_stores(active_only=True)
if is_admin(user):
    store_options = {s["name"]: s["id"] for s in all_stores}
else:
    allowed_ids = set(get_user_store_ids(user.get("id", 0)))
    store_options = {s["name"]: s["id"] for s in all_stores if s["id"] in allowed_ids}
if not store_options:
    st.error("❌ 没有可用门店，请先创建门店并绑定权限。")
    st.stop()

sel_name = st.selectbox("选择门店", options=list(store_options.keys()), key="pos_store")
store_id = store_options[sel_name]

# ---------------- 已配置数据源列表 ----------------
section("已配置的数据源", icon="📡")
sources = get_pos_sources_by_store(store_id)
if not sources:
    st.info("该门店尚未配置任何 POS/收银数据源。在下方表单新增一个即可。")
else:
    for src in sources:
        status = "✅ 启用" if src.get("enabled") else "⛔ 禁用"
        last = src.get("last_sync_at") or "—"
        sync = src.get("last_sync_status") or "—"
        sched_disp = (src.get("schedule") or "").strip()
        if not sched_disp:
            sched_disp = "默认 23:30"
        else:
            _sp = parse_pos_schedule(sched_disp)
            if _sp:
                sched_disp = f"{_sp[0]:02d}:{_sp[1]:02d}"
        cols = st.columns([1.5, 1.1, 2.0, 0.9, 1.0, 1.4, 0.8, 0.8, 0.8])
        cols[0].write(f"**{src['name']}**")
        cols[1].write(src.get("source_type"))
        cols[2].write(src.get("base_url") or "—")
        cols[3].write(status)
        cols[4].write(f"⏰ {sched_disp}")
        cols[5].write(f"{last} / {sync}")
        if cols[6].button("编辑", key=f"edit_{src['id']}"):
            _load_into_form(src)
            st.rerun()
        if cols[7].button("拉取", key=f"pull_{src['id']}"):
            res = pos_client.pull_source(src["id"])
            st.session_state["pos_pull_result"] = res
            st.rerun()
        if cols[8].button("删除", key=f"del_{src['id']}"):
            arm_danger(
                "pos_source", src["id"], src.get("name", ""),
                warning=f"确认删除数据源「{src.get('name', '')}」？删除后该门店将无法再自动拉取 POS 销量（已回灌的历史数据保留）。",
            )
            st.rerun()

# ==================== 删除二次确认 ====================
_pend = render_danger_confirm(confirm_label="✅ 确认删除")
if _pend and _pend["kind"] == "pos_source":
    try:
        delete_pos_source(_pend["id"])
        st.success("已删除数据源")
    except Exception as _e:
        st.error(f"删除失败：{_e}")
    st.rerun()

# ---------------- 新增/编辑表单 ----------------
st.divider()
editing = st.session_state.get("pos_edit_id") is not None
section("➕ 新增数据源" if not editing else "✏️ 编辑数据源", icon="⚙️")

c1, c2 = st.columns(2)
with c1:
    st.text_input("数据源名称", key="pos_name", placeholder="如：美团收银-总店")
    st.selectbox("数据源类型", options=["http_json", "csv_http", "webhook"],
                 format_func=lambda x: {"http_json": "HTTP JSON 接口", "csv_http": "CSV 下载地址",
                                        "webhook": "Webhook 推送（POS 主动 POST）"}.get(x, x),
                 key="pos_type", help="webhook 模式由 POS 方把销售数据 POST 到本系统 /api/actual-sales，无需主动拉取")
    st.text_input("接口地址 (base_url)", key="pos_url",
                  placeholder="https://api.example.com/pos/sales")
    st.text_input("每日同步时间 (可选)", key="pos_schedule",
                  placeholder="如 23:30 或 每天 23:30；留空则用全局默认(23:30)",
                  help="设定后调度器会在每天该时刻自动拉取本数据源；支持多个数据源各自不同时间。")
    _sched_hint = parse_pos_schedule(st.session_state.get("pos_schedule", "") or "")
    if st.session_state.get("pos_schedule", "").strip() and _sched_hint:
        st.caption(f"⏰ 已解析为每天 {_sched_hint[0]:02d}:{_sched_hint[1]:02d} 自动同步")
    elif st.session_state.get("pos_schedule", "").strip():
        st.caption("⚠️ 时间格式无法识别，请使用 时:分（如 23:30）")
with c2:
    st.selectbox("认证方式", options=["none", "api_key", "bearer", "basic"],
                 format_func=lambda x: {"none": "无", "api_key": "API Key 请求头",
                                        "bearer": "Bearer Token", "basic": "Basic 账号密码"}.get(x, x),
                 key="pos_auth")
    if st.session_state.get("pos_auth") == "api_key":
        st.text_input("请求头名称", key="pos_apikey_header", placeholder="X-API-Key")
        st.text_input("API Key", key="pos_apikey", type="password",
                      placeholder="留空表示不修改")
    elif st.session_state.get("pos_auth") == "bearer":
        st.text_input("Token", key="pos_bearer", type="password",
                      placeholder="留空表示不修改")
    elif st.session_state.get("pos_auth") == "basic":
        st.text_input("账号", key="pos_basic_user", placeholder="留空表示不修改")
        st.text_input("密码", key="pos_basic_pass", type="password",
                      placeholder="留空表示不修改")

# 请求报文配置（高级）：适配不同 POS 厂商的请求方式/参数/请求体
with st.expander("🔧 请求报文配置（高级，适配不同 POS 厂商）", expanded=False):
    st.caption("默认 GET + 无额外参数即可对接大多数接口；若厂商要求 POST / 带参数 / 带请求体，在此配置。")
    rc1, rc2 = st.columns(2)
    with rc1:
        st.selectbox("请求方法", options=["GET", "POST"], key="pos_method")
        st.selectbox("POST 内容类型", options=["json", "form", "text"], key="pos_content_type",
                     help="POST 且选 json 时请求体按 JSON 发送；form 按表单；text 原样发送")
    with rc2:
        st.text_area("额外请求头 (每行 key=value)", key="pos_headers",
                     placeholder="X-Shop-Id=12345\nX-Sign=abc",
                     help="除认证头外，厂商要求的其它固定请求头")
        st.text_area("查询参数 (每行 key=value)", key="pos_params",
                     placeholder="shopId=12345\ndate={sale_date}",
                     help="GET 查询参数或 POST 表单字段；支持 {sale_date} 占位，拉取时替换为当日日期")
    st.text_area("请求体 (POST 时；JSON 文本，支持 {sale_date} 占位)", key="pos_body",
                 placeholder='{"shopId":"12345","date":"{sale_date}"}',
                 help="POST 方法的请求体；留空则不带 body")

st.divider()
st.markdown("**字段映射**（告诉系统从返回数据里取哪些字段）")
m1, m2, m3 = st.columns(3)
with m1:
    st.text_input("菜品名称字段", key="pos_dish", placeholder="dish_name")
    st.text_input("销量字段", key="pos_qty", placeholder="qty")
with m2:
    st.text_input("日期字段 (可选)", key="pos_date", placeholder="sale_date")
    st.text_input("金额字段 (可选)", key="pos_amount", placeholder="amount",
                  help="POS 返回每条销量的销售额字段；留空则不记录金额（不影响销量）")
with m3:
    st.text_input("日期格式", key="pos_date_fmt", placeholder="%Y-%m-%d")
    st.checkbox("源中新增菜品自动建档", key="pos_auto_create", value=True,
                help="POS 出现菜单未收录的菜品时，自动建档并写入销量")

st.checkbox("启用该数据源", key="pos_enabled", value=True)

with st.expander("📦 响应信封校验（可选，适配带 code/msg 的返回结构）", expanded=False):
    st.caption("若 POS 返回外层包了 code/msg（如 {\"code\":0,\"data\":[...],\"msg\":\"ok\"}），在此配置成功判定与错误抽取；不配则任何 2xx 都视为成功。")
    e1, e2, e3 = st.columns(3)
    with e1:
        st.text_input("成功判定路径", key="pos_success_path", placeholder="code",
                      help="响应 JSON 中代表状态码的字段路径，如 code 或 result.code")
    with e2:
        st.text_input("成功期望值", key="pos_success_value", placeholder="0",
                      help="该字段等于此值才算成功（如 0）；留空表示不校验")
    with e3:
        st.text_input("错误信息路径", key="pos_message_path", placeholder="msg",
                      help="失败时从哪个字段抽取错误文案展示")

# ---------------- 操作按钮 ----------------
b1, b2, b3, b4 = st.columns([1, 1, 1, 1])
with b1:
    if st.button("💾 保存配置", use_container_width=True, type="primary"):
        draft = _gather_dict()
        raw_sched = (draft.get("schedule") or "").strip()
        if raw_sched and parse_pos_schedule(raw_sched) is None:
            st.error("同步时间格式不正确，请使用 时:分（如 23:30 或 每天 23:30）")
        elif not draft["name"] or not draft["base_url"]:
            st.error("请填写数据源名称和接口地址")
        else:
            try:
                sid = upsert_pos_source(
                    store_id=store_id, name=draft["name"], source_type=draft["source_type"],
                    base_url=draft["base_url"], auth_type=draft["auth_type"],
                    auth_config=draft["auth_config"], field_mapping=draft["field_mapping"],
                    request_config=draft["request_config"],
                    schedule=draft["schedule"], enabled=1 if draft["enabled"] else 0,
                    source_id=st.session_state.get("pos_edit_id"),
                )
            except ValueError as e:
                st.error(f"保存失败：{e}")
            else:
                st.success(f"已保存数据源（ID={sid}）")
                _clear_form()
                st.rerun()
with b2:
    if st.button("🔌 测试连接", use_container_width=True):
        draft = _gather_dict()
        draft["store_id"] = store_id
        res = pos_client.test_connection(draft)
        st.session_state["pos_test_result"] = res
        st.rerun()
with b3:
    if st.button("⬇️ 立即拉取", use_container_width=True):
        draft = _gather_dict()
        if not draft["name"] or not draft["base_url"]:
            st.error("请先填写并保存数据源")
        else:
            try:
                sid = upsert_pos_source(
                    store_id=store_id, name=draft["name"], source_type=draft["source_type"],
                    base_url=draft["base_url"], auth_type=draft["auth_type"],
                    auth_config=draft["auth_config"], field_mapping=draft["field_mapping"],
                    request_config=draft["request_config"],
                    schedule=draft["schedule"], enabled=1 if draft["enabled"] else 0,
                    source_id=st.session_state.get("pos_edit_id"),
                )
            except ValueError as e:
                st.error(f"保存失败：{e}")
            else:
                res = pos_client.pull_source(sid)
                st.session_state["pos_pull_result"] = res
                st.rerun()
with b4:
    if st.button("🧹 清空表单", use_container_width=True):
        _clear_form()
        st.rerun()

# ---------------- 结果展示 ----------------
if st.session_state.get("pos_test_result"):
    r = st.session_state["pos_test_result"]
    if r.get("ok"):
        banner("success", f"✅ 测试成功：{r.get('msg')}")
    else:
        banner("error", f"❌ 测试失败：{r.get('msg')}")
    if r.get("note") == "push_mode":
        st.caption("提示：Webhook 为推送模式，无需主动拉取；请由 POS 方 POST 到本系统 /api/actual-sales。")

if st.session_state.get("pos_pull_result"):
    r = st.session_state["pos_pull_result"]
    if r.get("ok"):
        stats = r.get("stats")
        if stats:
            banner("success", f"✅ {r.get('msg')} ｜ 解析 {stats['total']} 条，写入 {stats['upserted']} 条，"
                              f"自动建档 {stats['auto_created']} 条，跳过 {stats['skipped']} 条")
        else:
            banner("info", f"ℹ️ {r.get('msg')}")
    else:
        banner("error", f"❌ 拉取失败：{r.get('msg')}")

# ---------------- 同步日志 ----------------
st.divider()
section("同步日志", icon="📜")
log = get_pos_sync_log(limit=30)
if not log:
    st.caption("暂无同步记录。")
else:
    import pandas as pd
    df = pd.DataFrame(log)[["run_at", "status", "msg"]]
    df.columns = ["时间", "状态", "说明"]
    st.dataframe(df, use_container_width=True, hide_index=True)
