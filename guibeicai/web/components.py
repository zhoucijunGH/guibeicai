"""
桂备菜 - Streamlit 统一设计系统与可复用组件（V2 餐饮主题版）
=============================================================
特性：
  - 明 / 暗 双主题（session_state["theme"] 驱动，默认 light）
  - 餐饮暖色品牌色板：辣椒红主色 + 桂花金点缀，大面积留白 + 细腻投影
  - 可复用组件：渐变页头、指标卡（图标/环比）、Hero 横幅、快捷入口卡、
    风险标签、空状态、横幅、区块卡、分步向导、数据表封装
  - Plotly 全局图表主题：全站图表自动统一配色与字体
所有组件保持与 V1 相同的函数签名，切换主题无需改动业务代码。
"""
import streamlit as st

# ==================== 主题色板 ====================
THEME = {
    "light": {
        "--bg": "#f6f4f1",
        "--surface": "#ffffff",
        "--surface-2": "#f1ede7",
        "--text": "#241f1a",
        "--text-soft": "#8a8074",
        "--border": "#eae3d9",
        "--primary": "#d6453d",
        "--primary-deep": "#b7362f",
        "--primary-soft": "#fdeeea",
        "--accent": "#e8a33d",
        "--accent-soft": "#fdf3e3",
        "--success": "#2f9e63",
        "--success-soft": "#e9f6ef",
        "--warning": "#e8912d",
        "--warning-soft": "#fdf2e0",
        "--info": "#2e6fa7",
        "--info-soft": "#e9f1f8",
        "--hero-grad": "linear-gradient(135deg, #c93a32 0%, #d6453d 45%, #e0663a 100%)",
        "--shadow": "0 1px 2px rgba(40,30,20,0.04), 0 6px 20px rgba(40,30,20,0.06)",
        "--shadow-hover": "0 4px 10px rgba(40,30,20,0.08), 0 12px 32px rgba(40,30,20,0.10)",
    },
    "dark": {
        "--bg": "#141210",
        "--surface": "#1f1c19",
        "--surface-2": "#292520",
        "--text": "#f0ebe4",
        "--text-soft": "#a89d8f",
        "--border": "#38322a",
        "--primary": "#ff6f61",
        "--primary-deep": "#d6453d",
        "--primary-soft": "#3a2320",
        "--accent": "#f0b45c",
        "--accent-soft": "#33290f",
        "--success": "#4cd68a",
        "--success-soft": "#16301f",
        "--warning": "#ffb547",
        "--warning-soft": "#332714",
        "--info": "#6aabdd",
        "--info-soft": "#16283d",
        "--hero-grad": "linear-gradient(135deg, #7e2620 0%, #96312a 50%, #a94a22 100%)",
        "--shadow": "0 2px 12px rgba(0,0,0,0.35)",
        "--shadow-hover": "0 4px 18px rgba(0,0,0,0.5)",
    },
}

# 图表统一色序（餐饮暖色）
CHART_COLORS = ["#d6453d", "#e8a33d", "#2f9e63", "#2e6fa7", "#8a5fa8", "#c2703d", "#5f9ea0", "#b04a7a"]


def get_theme() -> str:
    return st.session_state.get("theme", "light")


def apply_theme():
    """根据当前主题注入 CSS 变量（应在每个页面顶部调用）。"""
    t = THEME.get(get_theme(), THEME["light"])
    var_block = "\n".join(f"    {k}: {v};" for k, v in t.items())
    st.markdown(f"""
    <style>
    :root {{
{var_block}
    }}
    </style>
    """, unsafe_allow_html=True)


def _apply_plotly_theme():
    """设置 Plotly 全局默认模板：全站图表统一餐饮暖色配色、字体与网格。"""
    try:
        import plotly.io as pio
        if "guibeicai" in pio.templates:
            return
        import plotly.graph_objects as go
        dark = get_theme() == "dark"
        t = go.layout.Template()
        t.layout.font = dict(
            family='"Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif',
            size=13, color="#a89d8f" if dark else "#5c554b",
        )
        t.layout.paper_bgcolor = "rgba(0,0,0,0)"
        t.layout.plot_bgcolor = "rgba(0,0,0,0)"
        t.layout.colorway = CHART_COLORS
        t.layout.margin = dict(l=10, r=10, t=44, b=10)
        t.layout.hoverlabel = dict(
            bgcolor="#1f1c19" if dark else "#ffffff",
            font=dict(size=13, color="#f0ebe4" if dark else "#241f1a"),
            bordercolor="#38322a" if dark else "#eae3d9",
        )
        grid = "#332c24" if dark else "#efe9e0"
        t.layout.xaxis = dict(gridcolor=grid, zerolinecolor=grid, linecolor=grid, fixedrange=True)
        t.layout.yaxis = dict(gridcolor=grid, zerolinecolor=grid, linecolor=grid)
        t.layout.legend = dict(orientation="h", yanchor="bottom", y=1.02, x=0,
                               bgcolor="rgba(0,0,0,0)")
        t.layout.title = dict(font=dict(size=15, color="#f0ebe4" if dark else "#241f1a"), x=0)
        pio.templates["guibeicai"] = t
        pio.templates.default = "guibeicai"
    except Exception:
        pass


def inject_global_css():
    """注入全局 CSS：字体、侧边栏、按钮、表格、滚动条、隐藏默认菜单。"""
    apply_theme()
    _apply_plotly_theme()
    st.markdown("""
    <style>
    /* ===== 全局字体与底色 ===== */
    html, body, .stApp, .stApp > header, section[data-testid="stSidebar"] {
        font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", Arial, sans-serif;
    }
    .stApp { background: var(--bg); color: var(--text); }
    .stApp h1, .stApp h2, .stApp h3, .stApp h4 { color: var(--text); font-weight: 700; }
    #MainMenu, footer, .stDeployButton { display: none; }
    header[data-testid="stHeader"] { background: transparent; }
    .block-container { padding-top: 1.4rem; max-width: 1400px; }

    /* ===== 侧边栏 ===== */
    section[data-testid="stSidebar"] {
        background: var(--surface) !important;
        border-right: 1px solid var(--border);
    }
    section[data-testid="stSidebar"] * { color: var(--text); }
    section[data-testid="stSidebar"] .stMarkdown { color: var(--text-soft); }
    section[data-testid="stSidebar"] hr { border-color: var(--border); }
    [data-testid="stSidebar"] .stPageLink > a {
        padding: 0.45rem 0.7rem !important;
        border-radius: 10px;
        font-size: 14.5px;
        transition: all .15s ease;
    }
    [data-testid="stSidebar"] .stPageLink > a:hover { background: var(--surface-2); }

    /* ===== 按钮 ===== */
    .stButton > button {
        border-radius: 10px;
        border: 1px solid var(--border);
        background: var(--surface);
        color: var(--text);
        font-weight: 500;
        transition: all .18s ease;
    }
    .stButton > button:hover {
        border-color: var(--primary); color: var(--primary);
        box-shadow: 0 2px 10px rgba(214,69,61,0.18);
        transform: translateY(-1px);
    }
    .stButton > button[kind="primary"] {
        background: var(--primary) !important; color: #fff !important;
        border-color: var(--primary) !important;
        box-shadow: 0 4px 14px rgba(214,69,61,0.30);
    }
    .stButton > button[kind="primary"]:hover { filter: brightness(1.06); }

    /* ===== 输入框 / 选择框 ===== */
    .stTextInput input, .stNumberInput input, .stDateInput input {
        background: var(--surface) !important; color: var(--text) !important;
        border: 1px solid var(--border) !important; border-radius: 10px;
    }
    .stTextInput input:focus, .stNumberInput input:focus {
        border-color: var(--primary) !important;
        box-shadow: 0 0 0 3px rgba(214,69,61,0.12) !important;
    }
    div[data-baseweb="select"] > div {
        background: var(--surface) !important; color: var(--text) !important;
        border: 1px solid var(--border) !important; border-radius: 10px !important;
    }
    div[data-baseweb="popover"] > div { background: var(--surface); border: 1px solid var(--border); }

    /* ===== 数据表 ===== */
    .stDataFrame, .stTable { background: var(--surface); border-radius: 14px; }
    div[data-testid="stDataFrame"] { border: 1px solid var(--border); border-radius: 14px; overflow: hidden; }

    /* ===== 指标卡 st.metric 统一风格 ===== */
    div[data-testid="stMetric"] {
        background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
        padding: 16px 18px; box-shadow: var(--shadow);
    }
    div[data-testid="stMetricLabel"] p { color: var(--text-soft); font-size: 13px; }
    div[data-testid="stMetricValue"] { color: var(--text); font-variant-numeric: tabular-nums; }

    /* ===== Expander / Tabs ===== */
    div[data-testid="stExpander"] {
        background: var(--surface); border: 1px solid var(--border);
        border-radius: 14px; box-shadow: var(--shadow);
    }
    .stTabs [data-baseweb="tab-list"] { gap: 6px; border-bottom: 1px solid var(--border); }
    .stTabs [data-baseweb="tab"] {
        border-radius: 10px 10px 0 0; padding: 8px 18px; font-weight: 600; color: var(--text-soft);
    }
    .stTabs [aria-selected="true"] { color: var(--primary) !important; }
    .stTabs [data-baseweb="tab-highlight"] { background-color: var(--primary); }

    /* ===== 进度条 / 分隔线 ===== */
    .stProgress > div > div { background: var(--primary); }
    hr { border-color: var(--border); opacity: .7; }

    /* ===== 滚动条 ===== */
    ::-webkit-scrollbar { width: 9px; height: 9px; }
    ::-webkit-scrollbar-track { background: transparent; }
    ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 6px; }
    ::-webkit-scrollbar-thumb:hover { background: var(--text-soft); }

    /* ===== 通用动画 ===== */
    @keyframes gbc-fade-up { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
    .gbc-fade { animation: gbc-fade-up .35s ease both; }
    </style>
    """, unsafe_allow_html=True)


# ==================== 渐变 Hero 横幅（工作台/重要页面顶部） ====================
def hero_banner(title: str, subtitle: str = "", chips: list = None, icon: str = "🥘"):
    """大气渐变横幅：标题 + 副标题 + 标签胶囊。chips: list[str]

    HTML 拼接为单行输出，避免 Markdown 把缩进标签解析成代码块。
    """
    chips_html = ""
    if chips:
        chips_html = "".join(
            f'<span style="background:rgba(255,255,255,0.16);border:1px solid rgba(255,255,255,0.28);'
            f'color:#fff;padding:4px 14px;border-radius:999px;font-size:13px;">{c}</span>'
            for c in chips
        )
        chips_html = (f'<div style="display:flex;gap:8px;margin-top:16px;'
                      f'flex-wrap:wrap;">{chips_html}</div>')
    html = (
        f'<div class="gbc-fade" style="background:var(--hero-grad);border-radius:20px;'
        f'padding:28px 32px;margin:4px 0 22px;position:relative;overflow:hidden;'
        f'box-shadow:0 10px 30px rgba(180,50,40,0.25);">'
        f'<div style="position:absolute;right:-40px;top:-40px;width:220px;height:220px;'
        f'border-radius:50%;background:rgba(255,255,255,0.08);"></div>'
        f'<div style="position:absolute;right:60px;bottom:-70px;width:180px;height:180px;'
        f'border-radius:50%;background:rgba(255,255,255,0.06);"></div>'
        f'<div style="display:flex;align-items:center;gap:14px;">'
        f'<div style="width:54px;height:54px;border-radius:16px;background:rgba(255,255,255,0.18);'
        f'display:flex;align-items:center;justify-content:center;font-size:30px;">{icon}</div>'
        f'<div><div style="color:#fff;font-size:26px;font-weight:800;letter-spacing:.5px;">{title}</div>'
        f'<div style="color:rgba(255,255,255,0.85);font-size:14.5px;margin-top:2px;">{subtitle}</div></div>'
        f'</div>{chips_html}</div>'
    )
    st.markdown(html, unsafe_allow_html=True)


# ==================== 指标卡片 ====================
def metric_card(title: str, value: str, subtitle: str = "", variant: str = "default",
                icon: str = "", delta: str = ""):
    """指标卡：图标圆片 + 大数字 + 说明 + 可选环比。

    variant: default | primary | success | warning | info | accent
    icon:    可选 emoji，展示在左侧圆片
    delta:   可选环比文案，如 "↑ 12.5%"（含 ↑ 显绿色，↓ 显红色）
    """
    color_map = {
        "default": "var(--info)", "primary": "var(--primary)", "success": "var(--success)",
        "warning": "var(--warning)", "info": "var(--info)", "accent": "var(--accent)",
    }
    soft_map = {
        "default": "var(--info-soft)", "primary": "var(--primary-soft)", "success": "var(--success-soft)",
        "warning": "var(--warning-soft)", "info": "var(--info-soft)", "accent": "var(--accent-soft)",
    }
    c = color_map.get(variant, color_map["default"])
    bg = soft_map.get(variant, soft_map["default"])
    # 注意：整卡 HTML 必须拼接为单行字符串输出。
    # 若用多行 f-string 且 delta/subtitle 为空，会留下空白行，
    # Markdown 会把后续缩进的 <div> 当作代码块原样显示（标签错乱 bug）。
    icon_html = (f'<div style="width:42px;height:42px;border-radius:12px;background:{bg};'
                 f'display:flex;align-items:center;justify-content:center;font-size:21px;'
                 f'flex-shrink:0;">{icon}</div>') if icon else ""
    inner = (f'<div style="display:flex;align-items:center;gap:12px;">{icon_html}'
             f'<div style="flex:1;min-width:0;">'
             f'<div style="font-size:13px;color:var(--text-soft);">{title}</div>'
             f'<div style="font-size:27px;font-weight:800;color:var(--text);line-height:1.25;'
             f'font-variant-numeric:tabular-nums;letter-spacing:.3px;">{value}</div>'
             f'</div></div>')
    if delta:
        d_color = "var(--success)" if ("↑" in delta or "+" in delta) else "var(--primary)"
        inner += (f'<div style="font-size:12.5px;font-weight:700;color:{d_color};'
                  f'margin-top:6px;">{delta}</div>')
    if subtitle:
        inner += (f'<div style="font-size:12px;color:var(--text-soft);'
                  f'margin-top:4px;">{subtitle}</div>')
    html = (f'<div class="gbc-fade" style="background:var(--surface);'
            f'border:1px solid var(--border);border-radius:16px;padding:18px 20px;'
            f'margin:6px 0;box-shadow:var(--shadow);transition:transform .18s ease, '
            f'box-shadow .18s ease;" onmouseover="this.style.transform=\'translateY(-3px)\';'
            f'this.style.boxShadow=\'var(--shadow-hover)\'" onmouseout='
            f'"this.style.transform=\'none\';this.style.boxShadow=\'var(--shadow)\'">{inner}</div>')
    st.markdown(html, unsafe_allow_html=True)


# ==================== 快捷入口卡片 ====================
def quick_link_card(icon: str, title: str, desc: str, target: str,
                    color: str = "#d6453d", soft: str = "#fdeeea"):
    """大尺寸快捷入口卡片：图标 + 标题 + 描述 + 底部跳转链接。

    color/soft 传十六进制色值（图标圆片底色 soft，强调色 color）。
    单行 HTML 输出，避免 Markdown 缩进解析问题。
    """
    html = (
        f'<div style="background:var(--surface);border:1px solid var(--border);'
        f'border-radius:16px;padding:18px 18px 12px;box-shadow:var(--shadow);'
        f'height:calc(100% - 34px);transition:transform .18s ease, box-shadow .18s ease, '
        f'border-color .18s ease;" onmouseover="this.style.transform=\'translateY(-4px)\';'
        f'this.style.boxShadow=\'var(--shadow-hover)\';this.style.borderColor=\'{color}\'" '
        f'onmouseout="this.style.transform=\'none\';this.style.boxShadow=\'var(--shadow)\';'
        f'this.style.borderColor=\'var(--border)\'">'
        f'<div style="width:44px;height:44px;border-radius:13px;background:{soft};display:flex;'
        f'align-items:center;justify-content:center;font-size:23px;margin-bottom:10px;">{icon}</div>'
        f'<div style="font-size:15.5px;font-weight:700;color:var(--text);">{title}</div>'
        f'<div style="font-size:12.5px;color:var(--text-soft);margin:4px 0 10px;'
        f'line-height:1.5;">{desc}</div></div>'
    )
    st.markdown(html, unsafe_allow_html=True)
    st.page_link(target, label="前往 →", use_container_width=True)


# ==================== 风险标签 ====================
def risk_badge(risk_level: str) -> str:
    badges = {
        "高": '<span style="background:var(--primary);color:#fff;padding:3px 12px;border-radius:999px;font-weight:600;font-size:13px;">⚠️ 高损耗风险</span>',
        "中": '<span style="background:var(--warning);color:#fff;padding:3px 12px;border-radius:999px;font-weight:600;font-size:13px;">⚡ 中等风险</span>',
        "低": '<span style="background:var(--success);color:#fff;padding:3px 12px;border-radius:999px;font-weight:600;font-size:13px;">✅ 低风险</span>',
    }
    return badges.get(risk_level, badges["低"])


# ==================== 页面头部 ====================
def page_header(title: str, subtitle: str = "", icon: str = "🥘"):
    """页面标题 + 主色渐变短杠 + 副标题（全站统一页头）。单行 HTML 输出。"""
    sub_html = ""
    if subtitle:
        sub_html = (f'<div style="color:var(--text-soft);font-size:14px;'
                    f'margin-top:2px;">{subtitle}</div>')
    html = (
        f'<div class="gbc-fade" style="margin-bottom:6px;">'
        f'<div style="display:flex;align-items:center;gap:12px;">'
        f'<div style="width:46px;height:46px;border-radius:14px;background:var(--primary-soft);'
        f'display:flex;align-items:center;justify-content:center;font-size:25px;flex-shrink:0;">{icon}</div>'
        f'<div><h1 style="margin:0;font-size:1.65rem;line-height:1.2;">{title}</h1>{sub_html}</div>'
        f'</div>'
        f'<div style="height:3px;width:64px;border-radius:2px;margin-top:12px;'
        f'background:linear-gradient(90deg, var(--primary), var(--accent));"></div>'
        f'<div style="height:1px;background:var(--border);margin-top:12px;margin-bottom:20px;"></div>'
        f'</div>'
    )
    st.markdown(html, unsafe_allow_html=True)


# ==================== 区块卡 ====================
def section(title: str, icon: str = "📌"):
    st.markdown(f"""
    <div style="display:flex;align-items:center;gap:9px;margin:20px 0 12px;">
        <div style="width:6px;height:20px;border-radius:3px;
                    background:linear-gradient(180deg, var(--primary), var(--accent));"></div>
        <span style="font-size:19px;">{icon}</span>
        <h3 style="margin:0;font-size:1.12rem;color:var(--text);">{title}</h3>
    </div>
    """, unsafe_allow_html=True)


# ==================== 横幅 ====================
def banner(kind: str, text: str):
    """kind: info | success | warning | error"""
    style = {
        "info": ("var(--info-soft)", "var(--info)"),
        "success": ("var(--success-soft)", "var(--success)"),
        "warning": ("var(--warning-soft)", "var(--warning)"),
        "error": ("var(--primary-soft)", "var(--primary)"),
    }.get(kind, ("var(--info-soft)", "var(--info)"))
    bg, fg = style
    icon = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "❌"}.get(kind, "ℹ️")
    st.markdown(f"""
    <div style="background:{bg};border:1px solid {fg}55;border-left:4px solid {fg};border-radius:12px;
                padding:11px 16px;color:{fg};font-size:14px;margin:8px 0;">
        {icon} {text}
    </div>
    """, unsafe_allow_html=True)


# ==================== 空状态 ====================
def empty_state(icon: str, title: str, hint: str = "", action_label: str = "", action_target: str = ""):
    """空状态卡片。单行 HTML 输出，避免 Markdown 缩进解析问题。"""
    hint_html = ""
    if hint:
        hint_html = f'<div style="color:var(--text-soft);font-size:14px;">{hint}</div>'
    html = (
        f'<div style="text-align:center;padding:44px 20px;background:var(--surface);'
        f'border:1px dashed var(--border);border-radius:18px;margin:16px 0;">'
        f'<div style="width:76px;height:76px;margin:0 auto;border-radius:24px;'
        f'background:var(--surface-2);display:flex;align-items:center;justify-content:center;'
        f'font-size:38px;">{icon}</div>'
        f'<div style="font-size:17px;font-weight:700;color:var(--text);margin:14px 0 4px;">{title}</div>'
        f'{hint_html}</div>'
    )
    st.markdown(html, unsafe_allow_html=True)
    if action_label and action_target:
        st.page_link(action_target, label=action_label, use_container_width=True)


# ==================== 分步向导 ====================
def stepper(steps: list, current: int):
    """steps: list[str]; current: 当前步索引(0-based)"""
    html = '<div style="display:flex;align-items:center;gap:6px;margin:10px 0 18px;flex-wrap:wrap;">'
    for i, s in enumerate(steps):
        if i < current:
            color, bg = "var(--success)", "var(--success-soft)"
        elif i == current:
            color, bg = "#fff", "var(--primary)"
        else:
            color, bg = "var(--text-soft)", "var(--surface-2)"
        shadow = ("box-shadow:0 3px 10px rgba(214,69,61,0.35);" if i == current else "")
        html += (f'<div style="background:{bg};color:{color};border-radius:999px;'
                 f'padding:6px 16px;font-size:13px;font-weight:600;{shadow}">{i+1}. {s}</div>')
        if i < len(steps) - 1:
            html += '<span style="color:var(--border);">━━</span>'
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)


# ==================== 打印/导出辅助 ====================
def print_button():
    st.markdown("""
    <button onclick="window.print()" style="
        background:var(--info);color:#fff;border:none;
        padding:8px 20px;border-radius:10px;font-size:15px;cursor:pointer;margin:8px 0;
    ">🖨️ 打印 / 导出 PDF</button>
    """, unsafe_allow_html=True)


def theme_toggle():
    """渲染主题切换按钮（在导航顶部调用）。"""
    cur = get_theme()
    label = "🌙 暗色" if cur == "light" else "☀️ 亮色"
    if st.button(label, key="theme_toggle", use_container_width=True,
                 help="切换亮色 / 暗色主题"):
        st.session_state["theme"] = "dark" if cur == "light" else "light"
        st.rerun()


# ==================== 危险操作二次确认 ====================
# 用法（两步式，避免误点即删）：
#   1) 触发按钮处调用 arm_danger(kind, id, label, warning, extra)，然后 st.rerun()
#   2) 在列表渲染【之后】的页面顶层调用
#        pend = render_danger_confirm()
#        if pend and pend["kind"] == "store":
#            ... 执行删除 ...
# 组件内部用 session_state 记录待确认动作，点击「取消」不会误删。
_DANGER_KEY = "_pending_danger"


def arm_danger(kind: str, target_id, label: str = "", warning: str = None,
               extra: dict = None, pending_key: str = _DANGER_KEY):
    """登记一个待二次确认的危险操作（由触发按钮调用）。"""
    st.session_state[pending_key] = {
        "kind": kind, "id": target_id, "label": label,
        "warning": warning, "extra": extra or {},
    }


def render_danger_confirm(pending_key: str = _DANGER_KEY, confirm_label: str = "✅ 确认删除") -> dict:
    """在页面顶层渲染危险操作二次确认块。

    返回被确认的动作 dict（调用方据此执行删除），未确认则返回 None。
    """
    pend = st.session_state.get(pending_key)
    if not pend:
        return None
    warn = pend.get("warning") or f"确认删除「{pend.get('label', '')}」？此操作不可恢复。"
    st.error("⚠️ " + warn)
    extra = pend.get("extra") or {}
    if extra:
        with st.expander("🔍 查看将一并清理的数据量", expanded=True):
            st.json(extra)
    c1, c2 = st.columns([1, 4])
    if c1.button(confirm_label, key=f"{pending_key}__yes", type="primary"):
        st.session_state.pop(pending_key, None)
        return pend
    if c2.button("取消", key=f"{pending_key}__no"):
        st.session_state.pop(pending_key, None)
        st.rerun()
    return None
