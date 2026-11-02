"""操作审计模块（#15）

统一记录系统中的关键操作（登录/登出、手动与自动预测、回测运行与应用、
用户/门店/菜品的增删改等），支撑安全审计与责任追溯。

设计要点：
- 审计记录落 audit_log 表（与业务表同库，未来换 PG 仅需改 _db 连接）。
- log_action 内部全程 try/except 兜底：审计写入失败绝不影响主业务流程。
- 用户身份优先取传入的 user 参数；未传时尝试从 Streamlit session 自动识别。
- detail 以 JSON 存储，便于扩展结构化字段，又不影响主表结构。
"""

import json
import logging

logger = logging.getLogger(__name__)

# ==================== 动作常量（页面/筛选统一引用） ====================
ACTION_LOGIN = "login"
ACTION_LOGIN_FAILED = "login_failed"
ACTION_LOGOUT = "logout"

ACTION_PREDICT_MANUAL = "predict_manual"
ACTION_PREDICT_AUTO = "predict_auto"

ACTION_BACKTEST_RUN = "backtest_run"
ACTION_BACKTEST_APPLY = "backtest_apply"

ACTION_USER_CREATE = "user_create"
ACTION_USER_UPDATE = "user_update"
ACTION_USER_DELETE = "user_delete"

ACTION_STORE_CREATE = "store_create"
ACTION_STORE_UPDATE = "store_update"
ACTION_STORE_DELETE = "store_delete"
ACTION_STORE_STATUS = "store_status"

ACTION_SUPPLY_SCOPE = "supply_scope"

ACTION_DISH_STATUS = "dish_status"

ACTION_DATA_INIT = "data_init"

ACTION_CONTRACT_CREATE = "contract_create"
ACTION_CONTRACT_UPDATE = "contract_update"
ACTION_CONTRACT_DELETE = "contract_delete"
ACTION_CONTRACT_SYNC = "contract_sync"

# 供页面下拉筛选使用的可读中文标签
ACTION_LABELS = {
    ACTION_LOGIN: "登录",
    ACTION_LOGIN_FAILED: "登录失败",
    ACTION_LOGOUT: "登出",
    ACTION_PREDICT_MANUAL: "手动预测",
    ACTION_PREDICT_AUTO: "自动预测(定时)",
    ACTION_BACKTEST_RUN: "回测运行",
    ACTION_BACKTEST_APPLY: "回测校正应用",
    ACTION_USER_CREATE: "新增用户",
    ACTION_USER_UPDATE: "修改用户",
    ACTION_USER_DELETE: "删除用户",
    ACTION_STORE_CREATE: "新增门店",
    ACTION_STORE_UPDATE: "修改门店",
    ACTION_STORE_DELETE: "删除门店",
    ACTION_STORE_STATUS: "门店启停",
    ACTION_SUPPLY_SCOPE: "渠道供货范围配置",
    ACTION_DISH_STATUS: "菜品启停",
    ACTION_DATA_INIT: "数据初始化导入",
    ACTION_CONTRACT_CREATE: "新增合同",
    ACTION_CONTRACT_UPDATE: "修改合同",
    ACTION_CONTRACT_DELETE: "删除合同",
    ACTION_CONTRACT_SYNC: "合同同步账号有效期",
}


def _resolve_user(user=None) -> dict:
    """解析当前操作用户；优先用显式传入，否则尝试从 Streamlit session 读取。"""
    if user is not None:
        return user
    try:
        import streamlit as st

        u = st.session_state.get("user")
        if u:
            return {"id": u.get("id"), "username": u.get("username"), "role": u.get("role")}
    except Exception:
        pass
    return None


def log_action(
    action: str,
    target_type: str = "",
    target_id: str = "",
    detail: dict = None,
    result: str = "ok",
    error_msg: str = "",
    user=None,
    ip: str = "",
) -> None:
    """写入一条审计记录。任何异常都被吞掉，确保不影响业务主流程。"""
    try:
        u = _resolve_user(user)
        user_id = (u or {}).get("id") or 0
        username = (u or {}).get("username", "") if u else ""
        role = (u or {}).get("role", "") if u else ""
        detail_json = json.dumps(detail, ensure_ascii=False) if detail is not None else "{}"

        from guibeicai.web._db import get_conn

        conn = get_conn()
        c = conn.cursor()
        c.execute(
            """INSERT INTO audit_log
               (ts, user_id, username, role, action, target_type, target_id, detail_json, result, error_msg, ip)
               VALUES (datetime('now','localtime'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                int(user_id),
                username,
                role,
                action,
                target_type,
                str(target_id),
                detail_json,
                result,
                error_msg,
                ip or "",
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:  # 审计失败绝不应阻断业务
        logger.warning("[audit] 写入失败 action=%s: %s", action, e)


def load_audit_log(
    limit: int = 200,
    username: str = None,
    action: str = None,
    date_from: str = None,
    date_to: str = None,
) -> list:
    """读取审计记录（倒序），支持按用户名/动作/日期区间筛选。"""
    from guibeicai.web._db import get_conn

    clauses = []
    params = []
    if username:
        clauses.append("username LIKE ?")
        params.append(f"%{username}%")
    if action:
        clauses.append("action = ?")
        params.append(action)
    if date_from:
        clauses.append("ts >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("ts <= ?")
        params.append(date_to)

    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "SELECT id, ts, user_id, username, role, action, target_type, target_id, detail_json, result, error_msg, ip "
        f"FROM audit_log{where} ORDER BY id DESC LIMIT ?",
        params + [int(limit)],
    )
    rows = c.fetchall()
    conn.close()

    out = []
    for r in rows:
        out.append(
            {
                "id": r[0],
                "ts": r[1],
                "user_id": r[2],
                "username": r[3],
                "role": r[4],
                "action": r[5],
                "target_type": r[6],
                "target_id": r[7],
                "detail": json.loads(r[8]) if r[8] else {},
                "result": r[9],
                "error_msg": r[10],
                "ip": r[11],
            }
        )
    return out
