"""页面注册表（页面键的唯一来源）

所有可访问的页面在此登记：key（稳定逻辑键）、label（展示名）、icon、path（Streamlit 路径）、group（侧边栏分组）。

注意：
- path / label / icon / group 是“页面元信息”，新增页面时在此登记一次即可；
- 某个角色能访问哪些页面，由 roles.pages（页面键列表）决定，在「角色管理」页面配置，
  不需要改任何代码。
- 页面键（key）是逻辑判断用的稳定标识，不要随意改动（历史数据按 key 存储）。
"""

# 侧边栏分组顺序
PAGE_GROUPS = ["首页", "智能预测", "数据管理", "渠道", "分析", "系统管理"]

# 全部页面（key 稳定，用于逻辑与授权；label/icon/path/group 用于展示）
APP_PAGES = [
    {"key": "dashboard",       "label": "工作台首页", "icon": "🏠", "path": "app.py",                 "group": "首页"},
    {"key": "data_upload",     "label": "数据上传",   "icon": "📤", "path": "pages/1_数据上传.py",    "group": "智能预测"},
    {"key": "predict_config",  "label": "执行预测",   "icon": "⚙️", "path": "pages/2_预测配置.py",    "group": "智能预测"},
    {"key": "predict_overview","label": "预测总览",   "icon": "📋", "path": "pages/3_预测总览.py",    "group": "智能预测"},
    {"key": "ingredient",      "label": "食材采购",   "icon": "🧾", "path": "pages/12_食材采购.py",   "group": "智能预测"},
    {"key": "store_manage",    "label": "门店管理",   "icon": "🏪", "path": "pages/4_门店管理.py",    "group": "数据管理"},
    {"key": "dish_manage",     "label": "菜品管理",   "icon": "🍳", "path": "pages/5_菜品管理.py",    "group": "数据管理"},
    {"key": "ingredient_manage","label": "食材管理",  "icon": "🥬", "path": "pages/17_食材管理.py",   "group": "数据管理"},
    {"key": "pos_integration", "label": "POS对接",   "icon": "🔌", "path": "pages/14_POS对接.py",   "group": "数据管理"},
    {"key": "data_init",       "label": "数据初始化", "icon": "📥", "path": "pages/20_数据初始化.py", "group": "数据管理"},
    {"key": "channel_overview","label": "渠道工作台", "icon": "🚚", "path": "pages/18_渠道工作台.py",  "group": "渠道"},
    {"key": "channel_store",   "label": "渠道门店与供货", "icon": "🏬", "path": "pages/21_渠道门店与供货.py", "group": "渠道"},
    {"key": "channel_store_account", "label": "渠道门店账号", "icon": "🏬", "path": "pages/23_渠道门店账号.py", "group": "渠道"},
    {"key": "compare",         "label": "预测对比",   "icon": "📈", "path": "pages/9_预测对比.py",    "group": "分析"},
    {"key": "backtest",        "label": "模型回测",   "icon": "🔁", "path": "pages/10_回测.py",       "group": "分析"},
    {"key": "waste",           "label": "损耗分析",   "icon": "🗑️", "path": "pages/16_损耗分析.py",   "group": "分析"},
    {"key": "alert",           "label": "智能预警",   "icon": "🔔", "path": "pages/15_智能预警.py",   "group": "分析"},
    {"key": "user_manage",     "label": "用户管理",   "icon": "👤", "path": "pages/7_用户管理.py",    "group": "系统管理"},
    {"key": "role_manage",     "label": "角色管理",   "icon": "🔐", "path": "pages/8_角色管理.py",    "group": "系统管理"},
    {"key": "audit",           "label": "操作审计",   "icon": "📝", "path": "pages/11_操作审计.py",   "group": "系统管理"},
    {"key": "contract_manage", "label": "合同管理",   "icon": "📑", "path": "pages/22_合同管理.py",    "group": "系统管理"},
]

# path -> key（供页面内根据当前文件定位自身 key 使用）
PAGE_KEY_BY_PATH = {p["path"]: p["key"] for p in APP_PAGES}

# key -> page 定义
PAGE_BY_KEY = {p["key"]: p for p in APP_PAGES}


def all_page_keys():
    """返回全部页面键（用于角色管理全选）。"""
    return [p["key"] for p in APP_PAGES]


def page_label(key: str) -> str:
    """页面键 -> 展示名"""
    return PAGE_BY_KEY.get(key, {}).get("label", key)
