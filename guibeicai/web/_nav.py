"""桂备菜 Web 公共侧边栏导航（V2 餐饮主题设计系统版）"""

import time
import streamlit as st

from guibeicai.web.components import inject_global_css, theme_toggle
from guibeicai.web._auth import is_admin
from guibeicai.web._pages import APP_PAGES, PAGE_GROUPS
from guibeicai.web._db import can_access_page

# 会话过期时间（秒）
SESSION_TIMEOUT = 8 * 3600


def check_session():
    """检查登录态和会话过期，未登录或过期则跳登录页"""
    if "user" not in st.session_state:
        st.switch_page("pages/login.py")
    login_time = st.session_state.get("login_time", 0)
    if time.time() - login_time > SESSION_TIMEOUT:
        st.session_state.pop("user", None)
        st.session_state.pop("login_time", None)
        st.switch_page("pages/login.py")


def render_nav() -> None:
    """每个页面顶部调用，渲染左侧统一菜单。"""
    inject_global_css()
    check_session()

    user = st.session_state.get("user", {})
    user_is_admin = is_admin(user)

    with st.sidebar:
        st.markdown(
            """
            <style>
            [data-testid="stSidebar"] .stPageLink > a {
                padding: 0.5rem 0.7rem !important;
                border-radius: 10px;
            }
            [data-testid="stSidebar"] .stPageLink > a:hover {
                background: var(--surface-2);
            }
            [data-testid="stSidebar"] .stPageLink > a [data-testid="stIconMaterial"] {
                color: var(--text-soft);
            }
            .nav-group-title {
                font-size: 11.5px !important;
                font-weight: 700;
                color: var(--text-soft) !important;
                text-transform: uppercase;
                letter-spacing: 1.5px;
                margin: 1.1rem 0 0.3rem 0.5rem;
                display: flex;
                align-items: center;
                gap: 6px;
            }
            .nav-group-title::after {
                content: "";
                flex: 1;
                height: 1px;
                background: var(--border);
            }
            .nav-version {
                font-size: 11.5px !important;
                color: var(--text-soft) !important;
                line-height: 1.7 !important;
            }
            .nav-version .ver-num {
                font-weight: 800; color: var(--primary); font-size: 12.5px;
            }
            .nav-brand {
                background: var(--hero-grad);
                border-radius: 16px;
                padding: 16px 14px;
                margin-bottom: 10px;
                box-shadow: 0 6px 18px rgba(180,50,40,0.22);
            }
            .nav-brand .brand-name {
                color: #fff; font-size: 19px; font-weight: 800; letter-spacing: 1px;
                display: flex; align-items: center; gap: 8px;
            }
            .nav-brand .brand-slogan {
                color: rgba(255,255,255,0.82); font-size: 11.5px; margin-top: 3px;
            }
            .nav-user {
                display: flex; align-items: center; gap: 9px;
                background: var(--surface-2); border: 1px solid var(--border);
                border-radius: 12px; padding: 8px 12px; margin: 4px 0 8px;
            }
            .nav-user .avatar {
                width: 32px; height: 32px; border-radius: 10px; flex-shrink: 0;
                background: var(--accent-soft); display: flex; align-items: center;
                justify-content: center; font-size: 17px;
            }
            .nav-user .u-name { font-size: 13.5px; font-weight: 700; color: var(--text); }
            .nav-user .u-role { font-size: 11.5px; color: var(--text-soft); }
            </style>
            """,
            unsafe_allow_html=True,
        )

        # 品牌区块（渐变卡）
        st.markdown(
            """
            <div class="nav-brand">
                <div class="brand-name">🥘 桂备菜</div>
                <div class="brand-slogan">AI 智能备菜 · 降损耗 · 精准备货</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        theme_toggle()

        # 用户信息卡
        st.markdown(
            f"""
            <div class="nav-user">
                <div class="avatar">👤</div>
                <div>
                    <div class="u-name">{user.get('username', '未登录')}</div>
                    <div class="u-role">{'🔑 管理员' if user_is_admin else '🏪 商家'}</div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if st.button("🚪 退出登录", use_container_width=True, key="logout_btn"):
            try:
                from guibeicai.web._audit import log_action, ACTION_LOGOUT
                _logout_user = st.session_state.get("user")
                log_action(
                    ACTION_LOGOUT, target_type="user",
                    target_id=_logout_user.get("id") if _logout_user else "",
                    detail={"username": _logout_user.get("username") if _logout_user else ""},
                    user=_logout_user,
                )
            except Exception:
                pass
            st.session_state.pop("user", None)
            st.session_state.pop("login_time", None)
            st.switch_page("pages/login.py")

        st.divider()

        # 导航完全由页面注册表 + 角色页面授权驱动，无需改代码即可调整可见页面
        role_code = user.get("role", "")
        for group in PAGE_GROUPS:
            group_pages = [
                p for p in APP_PAGES
                if p["group"] == group and can_access_page(role_code, p["key"])
            ]
            if not group_pages:
                continue
            st.markdown(f'<div class="nav-group-title">{group}</div>', unsafe_allow_html=True)
            for p in group_pages:
                st.page_link(p["path"], label=p["label"], icon=p["icon"])

        st.divider()
        st.markdown(
            """
            <div class="nav-version">
                <span class="ver-num">v2.0 · UI 焕新版</span><br>
                餐饮智能备菜 · AI 销量预测<br>
                降损耗 · 降本 · 精准备货
            </div>
            """,
            unsafe_allow_html=True,
        )
