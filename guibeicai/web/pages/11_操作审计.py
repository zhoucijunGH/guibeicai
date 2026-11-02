"""操作审计页面（仅 admin 可访问）

展示系统关键操作的审计日志：登录/登出、手动与自动预测、回测运行与应用、
用户/门店/菜品的增删改等。支持按动作、用户名、日期区间筛选，便于安全追溯与责任定位。
"""

import streamlit as st
import pandas as pd

from guibeicai.web._db import init_db
from guibeicai.web._nav import render_nav
from guibeicai.web._auth import require_auth, require_page_access
from guibeicai.web._audit import load_audit_log, ACTION_LABELS

init_db()

st.set_page_config(page_title="操作审计", page_icon="📝", layout="wide")
render_nav()

# ===== 登录校验 + 页面权限 =====
user = require_auth()
require_page_access("audit")

st.title("📝 操作审计")
st.caption("记录系统关键操作（登录/预测/回测/用户/门店/菜品等），用于安全追溯与责任定位。")

# ==================== 筛选条件 ====================
fcol1, fcol2, fcol3, fcol4 = st.columns([2, 2, 1.5, 1.5])
with fcol1:
    action_options = ["全部"] + list(ACTION_LABELS.values())
    # 反向映射：标签 -> 动作码
    _label_to_action = {v: k for k, v in ACTION_LABELS.items()}
    sel_action_label = st.selectbox("动作", action_options, index=0)
with fcol2:
    username_filter = st.text_input("用户名（模糊）", placeholder="留空=全部")
with fcol3:
    date_from = st.date_input("起始日期", value=None, label_visibility="collapsed")
with fcol4:
    date_to = st.date_input("结束日期", value=None, label_visibility="collapsed")

c1, c2 = st.columns([1, 5])
with c1:
    limit = st.number_input("返回条数", min_value=20, max_value=2000, value=200, step=20)

# ==================== 查询 ====================
action_filter = _label_to_action.get(sel_action_label) if sel_action_label != "全部" else None
date_from_str = date_from.strftime("%Y-%m-%d 00:00:00") if date_from else None
date_to_str = date_to.strftime("%Y-%m-%d 23:59:59") if date_to else None

rows = load_audit_log(
    limit=int(limit),
    username=username_filter.strip() or None,
    action=action_filter,
    date_from=date_from_str,
    date_to=date_to_str,
)

if not rows:
    st.info("暂无符合条件的审计记录。")
else:
    st.caption(f"共 {len(rows)} 条记录（按时间倒序，最多 {limit} 条）")
    display = []
    for r in rows:
        detail = r["detail"] or {}
        # 详情精简：把常见字段拼成一行可读文本
        if isinstance(detail, dict):
            detail_text = "；".join(f"{k}={v}" for k, v in detail.items() if v not in (None, "", []))
        else:
            detail_text = str(detail)
        display.append({
            "时间": r["ts"],
            "用户": r["username"] or "—",
            "角色": r["role"] or "—",
            "动作": ACTION_LABELS.get(r["action"], r["action"]),
            "对象": f'{r["target_type"]}:{r["target_id"]}' if r["target_id"] else r["target_type"],
            "结果": "成功" if r["result"] == "ok" else f'失败({r["result"]})',
            "详情": detail_text,
        })
    df = pd.DataFrame(display)
    st.dataframe(df, use_container_width=True, hide_index=True)
