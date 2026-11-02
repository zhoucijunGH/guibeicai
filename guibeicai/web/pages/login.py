"""登录页（含图形验证码）

布局：用户名 → 密码 → 验证码(输入框+图片+刷新图标同一行) → 登录按钮（常规登录页顺序）。
不使用 st.form：Streamlit 禁止 st.button 置于 form 内，而验证码刷新需要 st.button；
改用普通控件 + 普通按钮，提交时直接读取 st.session_state 当前值，已填值不丢失。
"""

import random
import time
from io import BytesIO

import streamlit as st
from PIL import Image, ImageDraw, ImageFont

from guibeicai.web._db import init_db, authenticate_user, update_user_password, set_tenant
from guibeicai.web._audit import log_action, ACTION_LOGIN, ACTION_LOGIN_FAILED

init_db()


def _render_force_change():
    """首次登录强制改密（S1）：渲染改密表单并阻断其余界面。"""
    user = st.session_state["_pending_user"]
    # 多租户：改密前先按会话租户切库（pending 路径已写入 tenant_id）
    set_tenant(st.session_state.get("tenant_id"))
    st.markdown("#### 首次登录，请修改密码")
    st.warning("为保障账户安全，首次登录必须修改初始密码后才能进入系统。")
    np1 = st.text_input("新密码（至少 6 位）", type="password", key="fc_pwd1")
    np2 = st.text_input("确认新密码", type="password", key="fc_pwd2")
    if st.button("确认修改", type="primary", use_container_width=True, key="fc_submit"):
        if not np1 or len(np1) < 6:
            st.error("密码长度至少 6 位")
        elif np1 != np2:
            st.error("两次输入的密码不一致")
        else:
            update_user_password(user["id"], np1, force_change_pwd=0)
            st.session_state["user"] = user
            st.session_state["login_time"] = time.time()
            log_action(ACTION_LOGIN, target_type="user", target_id=user["id"],
                       detail={"username": user["username"], "event": "first_login_change_pwd"}, user=user)
            st.session_state.pop("_pending_user", None)
            st.session_state.pop("captcha_code", None)
            st.session_state.pop("captcha_img", None)
            st.session_state.pop("login_captcha", None)
            st.success("密码已修改，欢迎使用！")
            st.switch_page("app.py")


# ==================== 验证码生成 ====================

def generate_captcha(length: int = 4) -> tuple[str, bytes]:
    """生成随机验证码字符串和图片 bytes"""
    # 去掉容易混淆的字符：0/O/1/I
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    code = "".join(random.choices(chars, k=length))

    width, height = 140, 48
    img = Image.new("RGB", (width, height), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    # 背景噪点
    for _ in range(200):
        x = random.randint(0, width - 1)
        y = random.randint(0, height - 1)
        draw.point((x, y), fill=(
            random.randint(180, 255),
            random.randint(180, 255),
            random.randint(180, 255),
        ))

    # 尝试加载字体
    try:
        font = ImageFont.truetype("arial.ttf", 28)
    except Exception:
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        except Exception:
            font = ImageFont.load_default()

    # 每个字符随机颜色和位置
    char_width = width // (length + 1)
    for i, ch in enumerate(code):
        color = (
            random.randint(30, 120),
            random.randint(30, 120),
            random.randint(30, 120),
        )
        x = char_width * (i + 1) + random.randint(-4, 4)
        y = random.randint(4, 14)
        draw.text((x, y), ch, fill=color, font=font)

    # 干扰线
    for _ in range(3):
        x1 = random.randint(0, width // 3)
        y1 = random.randint(0, height)
        x2 = random.randint(width * 2 // 3, width)
        y2 = random.randint(0, height)
        draw.line(
            [(x1, y1), (x2, y2)],
            fill=(
                random.randint(100, 200),
                random.randint(100, 200),
                random.randint(100, 200),
            ),
            width=1,
        )

    # 转 bytes 存 session
    buf = BytesIO()
    img.save(buf, format="PNG")
    return code, buf.getvalue()


def refresh_captcha():
    code, img_bytes = generate_captcha(4)
    st.session_state["captcha_code"] = code
    st.session_state["captcha_img"] = img_bytes


if "captcha_code" not in st.session_state:
    refresh_captcha()


# ==================== 页面样式（V2 餐饮主题） ====================

st.set_page_config(page_title="桂备菜 - 登录", page_icon="🥘", layout="centered")

st.markdown("""
<style>
html, body, .stApp {
    font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", Arial, sans-serif;
}
.stApp {
    background: linear-gradient(150deg, #fdf6f0 0%, #f9e9dd 45%, #f6ddd0 100%) !important;
}
header[data-testid="stHeader"] { background: transparent; }
#MainMenu, footer, .stDeployButton { display: none; }
.block-container {
    padding: 2.4rem 2.4rem 2.8rem;
    max-width: 470px;
    background: rgba(255,255,255,0.88);
    backdrop-filter: blur(8px);
    border: 1px solid rgba(255,255,255,0.95);
    border-radius: 24px;
    box-shadow: 0 6px 18px rgba(140,70,40,0.08), 0 16px 44px rgba(140,70,40,0.12);
    margin-top: 3.5rem !important;
    animation: login-fade-up .6s ease both;
}

.login-brand {
    display: flex; flex-direction: column; align-items: center;
    margin-bottom: 1.6rem;
    animation: login-fade-up .5s ease both;
}
.login-logo {
    width: 84px; height: 84px; border-radius: 24px;
    background: linear-gradient(135deg, #c93a32 0%, #d6453d 45%, #e0663a 100%);
    display: flex; align-items: center; justify-content: center;
    font-size: 44px;
    box-shadow: 0 10px 28px rgba(200,60,45,0.35);
    margin-bottom: 14px;
}
.login-title {
    font-size: 1.9rem; font-weight: 800; color: #241f1a; letter-spacing: 2px;
    margin: 0;
}
.login-subtitle {
    color: #8a8074; font-size: 0.92rem; margin-top: 4px; letter-spacing: 1px;
}
.login-card h4 {
    margin: 0 0 14px 0; color: #241f1a; font-weight: 700;
}
.stTextInput input {
    border-radius: 12px !important;
    border: 1px solid #e8ddd2 !important;
    background: #fff !important;
    height: 2.6rem;
}
.stTextInput input:focus {
    border-color: #d6453d !important;
    box-shadow: 0 0 0 3px rgba(214,69,61,0.12) !important;
}
.stButton > button {
    border-radius: 12px !important;
    font-weight: 600;
}
.stButton > button[kind="primary"] {
    background: linear-gradient(135deg, #c93a32, #e0663a) !important;
    border: none !important;
    height: 2.8rem;
    box-shadow: 0 6px 18px rgba(214,69,61,0.35) !important;
}
.stButton > button[kind="primary"]:hover { filter: brightness(1.05); }
.login-footer {
    text-align: center; color: #b3a898; font-size: 12px; margin-top: 18px;
}
@keyframes login-fade-up {
    from { opacity: 0; transform: translateY(14px); }
    to { opacity: 1; transform: none; }
}
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="login-brand">
    <div class="login-logo">🥘</div>
    <h1 class="login-title">桂备菜</h1>
    <div class="login-subtitle">智能备菜 · AI 销量与损耗预测系统</div>
</div>
""", unsafe_allow_html=True)

# 登录跳转原因提示（会话超时 / 账号已失效等）
_login_reason = st.session_state.get("_login_redirect_reason")
if _login_reason == "session_timeout":
    st.warning("登录态已超时，请重新登录")
elif _login_reason == "account_inactive":
    st.error("当前账号不在有效期内（已过期或尚未生效），请联系管理员")
st.session_state.pop("_login_redirect_reason", None)

# ==================== 首次登录强制改密（S1）：优先渲染，阻断登录表单 ====================
if st.session_state.get("_pending_user"):
    _render_force_change()
    st.stop()


# ==================== 登录表单（常规布局：用户名→密码→验证码→登录） ====================

st.markdown("#### 登录")

# 用户名、密码：普通控件，key 自动进 st.session_state
st.text_input("用户名", placeholder="请输入用户名", key="login_user")
st.text_input("密码", type="password", placeholder="请输入密码", key="login_pwd")

# 验证码行（输入框 + 图片 + 刷新图标，同一行，位于密码下方）
cap_col1, cap_col2, cap_col3 = st.columns([2, 3, 1])
with cap_col1:
    st.text_input(
                "验证码",
                placeholder="请输入右侧验证码",
                key="login_captcha",
                label_visibility="collapsed",
            )
with cap_col2:
    st.image(st.session_state["captcha_img"], width=120)
with cap_col3:
    if st.button("🔄", key="refresh_captcha_btn", help="点击刷新验证码",
                 use_container_width=True):
        refresh_captcha()

# 登录按钮（普通按钮，提交时直接读 st.session_state 当前值，已填值不丢失）
if st.button("登录", type="primary", use_container_width=True, key="login_btn"):
    # 关键：从 st.session_state 读当前输入值
    username = (st.session_state.get("login_user") or "").strip()
    password = (st.session_state.get("login_pwd") or "")
    captcha_input = (st.session_state.get("login_captcha") or "").strip()

    if not username or not password:
        st.error("请输入用户名和密码")
        st.stop()

    if not captcha_input:
        st.error("请输入验证码")
        st.stop()

    if captcha_input.upper() != st.session_state.get("captcha_code", ""):
        st.error("验证码错误")
        refresh_captcha()
        st.stop()

    # ---- 多租户登录路由：先查注册表确定该账号属于哪个租户，再切库鉴权 ----
    from guibeicai.web import _tenancy
    from guibeicai.web._db import set_tenant
    _tid = _tenancy.resolve_tenant_for_username(username)
    if _tid == "":  # 租户已暂停/退租：拒绝登录，数据保留
        log_action(ACTION_LOGIN_FAILED, target_type="user", target_id=username,
                   detail={"username": username, "reason": "tenant_inactive"}, result="fail")
        st.error("该账号所属租户已暂停或已退租，请联系管理员")
        refresh_captcha()
        st.stop()
    set_tenant(_tid)  # None=默认单库（向后兼容），否则切到对应租户库再鉴权

    user = authenticate_user(username, password)
    if user:
        # 账号有效期校验（按合同期限管控）：过期或尚未生效均拒绝登录
        from guibeicai.web._db import user_validity_status
        valid, valid_reason = user_validity_status(user)
        if not valid:
            log_action(ACTION_LOGIN_FAILED, target_type="user", target_id=user["id"],
                       detail={"username": user["username"], "reason": valid_reason}, result="fail")
            st.error(f"登录失败：{valid_reason}，请联系管理员")
            st.stop()
        # 首次登录强制改密（S1）：转入改密界面，不直接进系统
        if user.get("need_change_pwd"):
            st.session_state["_pending_user"] = user
            st.session_state["tenant_id"] = _tid
            st.rerun()
        st.session_state["user"] = user
        st.session_state["login_time"] = time.time()
        st.session_state["tenant_id"] = _tid  # 供后续页面按租户路由（未设置=默认单库）
        # 操作审计：登录成功
        log_action(ACTION_LOGIN, target_type="user", target_id=user["id"], detail={"username": user["username"]}, user=user)
        # 登录成功清除验证码残留
        st.session_state.pop("captcha_code", None)
        st.session_state.pop("captcha_img", None)
        st.session_state.pop("login_captcha", None)
        st.success(f"欢迎回来，{user['username']}！")
        st.switch_page("app.py")
    else:
        # 操作审计：登录失败（记录尝试的用户名，不记录密码）
        log_action(ACTION_LOGIN_FAILED, target_type="user", target_id=username, detail={"username": username}, result="fail")
        st.error("用户名或密码错误")
        refresh_captcha()
        st.stop()

# st.caption("商家测试账号：test / test ｜ 管理员：admin / admin")
