"""统一鉴权中间件（Phase 1 #6）

收敛所有页面散落的登录校验逻辑：
- 未登录 -> 跳转登录页
- 登录态超时 -> 清除并跳转登录页（时长由环境变量 SESSION_TIMEOUT_SECONDS 控制，默认 8h）
- 可选角色校验 required_role（admin 专用页面）

    用法：
    from guibeicai.web._auth import require_auth, require_page_access
    user = require_auth()                       # 仅校验登录
    user = require_page_access("user_manage")   # 校验登录 + 页面访问权限（由角色 pages 决定）
    """
import logging
import os
import time

import streamlit as st

from guibeicai.web._db import (
    is_admin_role, can_access_page, user_validity_status, set_tenant,
)

logger = logging.getLogger(__name__)

# 12-factor：会话超时可经环境变量覆盖（默认 8 小时）
SESSION_TIMEOUT_SECONDS = int(os.getenv("SESSION_TIMEOUT_SECONDS", "28800"))
_LOGIN_PAGE = "pages/login.py"


def is_admin(user_or_role) -> bool:
    """
    判断用户或角色是否为管理员。
    统一按角色编码判断（role == "admin"），不随展示名变化。
    """
    if isinstance(user_or_role, dict):
        role = user_or_role.get("role", "")
    else:
        role = user_or_role
    return is_admin_role(role) if role else False


def require_page_access(page_key: str) -> dict:
    """
    校验登录态 + 页面访问权限。未登录或超时跳转登录页；
    已登录但角色无该页面权限则提示并停止。返回当前 user dict。
    """
    # 多租户：按会话中的租户 id 重置连接上下文（未设置则连默认单库）
    set_tenant(st.session_state.get("tenant_id"))
    user = st.session_state.get("user")
    login_time = st.session_state.get("login_time")

    if not user:
        _redirect_to_login("not_logged_in")

    if login_time and (time.time() - float(login_time)) > SESSION_TIMEOUT_SECONDS:
        _redirect_to_login("session_timeout")

    if not can_access_page(user.get("role", ""), page_key):
        st.error("⚠️ 权限不足，您没有访问该页面的权限")
        st.stop()

    # 账号有效期校验（按合同期限管控）
    valid, reason = user_validity_status(user)
    if not valid:
        _redirect_to_login("account_inactive")

    return user


def _redirect_to_login(reason: str = "") -> None:
    """清除登录态并跳转登录页。st.switch_page 会中断当前脚本执行。"""
    st.session_state.pop("user", None)
    st.session_state.pop("login_time", None)
    if reason:
        st.session_state["_login_redirect_reason"] = reason
    st.switch_page(_LOGIN_PAGE)


def require_auth(required_role: str = None) -> dict:
    """校验登录态，未登录或超时则跳转登录页。返回当前 user dict。

    required_role: 若指定（如 "admin"），非该角色返回权限不足并停止。
    """
    # 多租户：按会话中的租户 id 重置连接上下文（未设置则连默认单库）
    set_tenant(st.session_state.get("tenant_id"))
    user = st.session_state.get("user")
    login_time = st.session_state.get("login_time")

    if not user:
        _redirect_to_login("not_logged_in")

    # 会话超时检查
    if login_time and (time.time() - float(login_time)) > SESSION_TIMEOUT_SECONDS:
        _redirect_to_login("session_timeout")

    # 角色校验：required_role="admin" 时按权限判断，支持中文角色名
    if required_role == "admin" and not is_admin(user):
        st.error("⚠️ 权限不足，仅管理员可访问该页面")
        st.stop()

    # 账号有效期校验（按合同期限管控）
    valid, reason = user_validity_status(user)
    if not valid:
        _redirect_to_login("account_inactive")

    return user
