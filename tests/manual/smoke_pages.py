"""轻量页面冒烟测试：用代理 streamlit 执行每个页面的顶层代码，
捕获 IndexError(cols[N] 越界)、NameError、TypeError(函数签名不匹配) 等运行时错误。
代理 st 对任意缺失属性都返回安全默认值，避免 mock 不全导致误报。
不涉及按钮回调逻辑（回调只在交互时触发，顶层执行已覆盖列表渲染等崩溃点）。
"""
import sys
import types
import traceback
from pathlib import Path


class _StopScript(Exception):
    pass


class _RerunScript(Exception):
    pass


class El:
    """通用 Streamlit 元素 mock（同时充当容器/布局元素）。"""

    def write(self, *a, **k):
        return self

    def markdown(self, *a, **k):
        return self

    def caption(self, *a, **k):
        return self

    def subheader(self, *a, **k):
        return self

    def divider(self, *a, **k):
        return self

    def button(self, *a, **k):
        return False

    def text_input(self, *a, **k):
        return ""

    def selectbox(self, label=None, options=None, index=0, **k):
        opts = list(options or [])
        if not opts:
            return ""
        try:
            return opts[index]
        except Exception:
            return opts[0]

    def multiselect(self, *a, **k):
        return []

    def checkbox(self, *a, **k):
        return False

    def columns(self, n):
        cnt = n if isinstance(n, int) else len(n)
        return [El() for _ in range(cnt)]

    def page_link(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _St:
    """代理 streamlit：已知属性给精确返回，未知属性一律返回安全默认值(El)。"""

    def __init__(self, session_state):
        self.session_state = session_state
        self.sidebar = El()

    def __getattr__(self, name):
        if name == "columns":
            return lambda n: [El() for _ in range(n if isinstance(n, int) else len(n))]
        if name == "tabs":
            return lambda *a, **k: (El(), El())
        if name == "selectbox":
            return lambda label=None, options=None, index=0, **k: (
                (list(options or []) or [""])[0] if options else "")
        if name == "multiselect":
            return lambda *a, **k: []
        if name == "date_input":
            import datetime as _dt
            return lambda *a, **k: _dt.date.today()
        if name == "time_input":
            import datetime as _dt
            return lambda *a, **k: _dt.time(0, 0)
        if name in ("number_input", "slider"):
            return lambda *a, **k: 0
        if name in ("text_input", "text_area"):
            return lambda *a, **k: ""
        if name in ("checkbox", "button", "form_submit_button", "toggle"):
            return lambda *a, **k: False
        if name in ("dataframe", "table", "line_chart", "bar_chart", "area_chart",
                    "scatter_chart", "metric", "json", "code", "map", "plotly_chart",
                    "pyplot", "title", "header", "subheader", "caption", "markdown",
                    "write", "info", "success", "error", "warning", "exception",
                    "spinner", "progress", "balloons", "snow", "toast", "divider",
                    "expander", "container", "form", "file_uploader", "slider", "radio",
                    "select_slider", "color_picker", "image", "audio", "video",
                    "set_page_config", "help", "vega_lite_chart", "altair_chart", "graphviz_chart",
                    "bokeh_chart", "pydeck_chart"):
            return lambda *a, **k: El()
        if name == "stop":
            return lambda *a, **k: (_ for _ in ()).throw(_StopScript())
        if name == "rerun":
            return lambda *a, **k: (_ for _ in ()).throw(_RerunScript())
        if name == "switch_page":
            return lambda *a, **k: (_ for _ in ()).throw(_StopScript())
        if name in ("cache_resource", "cache_data"):
            # 装饰器语义：@st.cache_resource / @st.cache_resource(...) 都返回可调用，
            # 冒烟期不真正执行被装饰函数体（避免外部 API/耗时），直接返回 None 可调用。
            def _cache(*a, **k):
                if len(a) >= 1 and callable(a[0]) and not k:
                    return lambda *aa, **kk: None
                def _dec(f):
                    return lambda *aa, **kk: None
                return _dec
            return _cache
        # 兜底：任意未知属性返回 El（可链式调用，不会因调用而崩溃）
        return lambda *a, **k: El()


class _SessionState:
    """模拟 Streamlit session_state：同时支持属性访问与字典访问，并提供 .get/.pop 等方法。"""

    def __init__(self, data=None):
        object.__setattr__(self, "_data", dict(data or {}))

    def __getattr__(self, k):
        return object.__getattribute__(self, "_data").get(k)

    def __setattr__(self, k, v):
        object.__getattribute__(self, "_data")[k] = v

    def __getitem__(self, k):
        return self._data[k]

    def __setitem__(self, k, v):
        self._data[k] = v

    def get(self, k, default=None):
        return self._data.get(k, default)

    def __contains__(self, k):
        return k in self._data

    def pop(self, *a):
        return self._data.pop(*a)

    def update(self, *a, **k):
        self._data.update(*a, **k)

    def setdefault(self, k, d=None):
        return self._data.setdefault(k, d)


# session_state：预置 admin 登录态
session_state = _SessionState()

# 注入假 streamlit（必须在 import 任何页面之前）
sys.modules["streamlit"] = _St(session_state)

# 预置登录态（admin，拥有全部页面）
session_state["user"] = {"id": 1, "username": "admin", "role": "admin"}
session_state["login_time"] = __import__("time").time()

PAGES = [
    "app.py",
    "pages/1_数据上传.py",
    "pages/2_预测配置.py",
    "pages/3_预测总览.py",
    "pages/4_门店管理.py",
    "pages/5_菜品管理.py",
    "pages/7_用户管理.py",
    "pages/8_角色管理.py",
    "pages/9_预测对比.py",
    "pages/10_回测.py",
    "pages/11_操作审计.py",
    "pages/12_食材采购.py",
    "pages/14_POS对接.py",
    "pages/16_损耗分析.py",
    "pages/15_智能预警.py",
    "pages/17_食材管理.py",
    "pages/18_渠道工作台.py",
]

import importlib.util
import os

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent  # tests/manual/ -> 项目根
ROOT = str(_PROJECT_ROOT / "guibeicai" / "web")
sys.path.insert(0, str(_PROJECT_ROOT))

results = []
for rel in PAGES:
    path = os.path.join(ROOT, rel)
    mod_name = "pagestest_" + rel.replace("\\", "_").replace("/", "_").replace(".", "_")
    try:
        spec = importlib.util.spec_from_file_location(mod_name, path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        results.append((rel, "PASS", ""))
    except _StopScript:
        results.append((rel, "PASS(stop)", ""))
    except _RerunScript:
        results.append((rel, "PASS(rerun)", ""))
    except Exception as e:
        results.append((rel, "FAIL", traceback.format_exc()))

print("=" * 70)
for rel, status, tb in results:
    if status.startswith("PASS"):
        print(f"[OK]   {rel:32} {status}")
    else:
        print(f"[FAIL] {rel:32} {status}")
        print(tb)
print("=" * 70)
fails = [r for r in results if r[1] == "FAIL"]
print(f"TOTAL={len(results)} PASS={len(results)-len(fails)} FAIL={len(fails)}")
