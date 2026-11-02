"""可复用的 Streamlit 桩（mock）——供页面冒烟测试 / 危险操作确认端到端测试共用。

设计要点（对应 streamlit-sqlite-crashsafe-checklist）：
- 假 `st` 用代理类 `__getattr__` 兜底，mock 不全也不会误报。
- 元素可调用、可作上下文管理器；按钮类方法默认返回 False（避免误触发 rerun 分支）。
- 控件返回"值"而非对象（selectbox→options[index]、number_input→0、date_input→date）。
- session_state 同时支持属性访问与字典访问。
- stop/rerun/switch_page 抛自定义异常。

用法：
    from tests.manual._stub_streamlit import install, StubSession
    state = StubSession()
    install(state, user={"id":1,"username":"admin","role":"admin"})
    # 之后 import 的页面会拿到桩 st
"""
from __future__ import annotations

import datetime as _dt
import sys
import time


class StopScript(Exception):
    pass


class RerunScript(Exception):
    pass


class SwitchPage(Exception):
    pass


class El:
    """通用元素 mock，同时充当列/容器。值返回型控件必须返回"值"。"""

    def __init__(self, button_policy=None):
        self._button_policy = button_policy or (lambda key: False)

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    # ---- 值返回型控件 ----
    def selectbox(self, label=None, options=None, index=0, **k):
        opts = list(options or [])
        if not opts:
            return ""
        try:
            return opts[index or 0]
        except Exception:
            return opts[0]

    def radio(self, label=None, options=None, index=0, **k):
        return self.selectbox(label, options, index, **k)

    def text_input(self, *a, **k):
        return ""

    def text_area(self, *a, **k):
        return ""

    def number_input(self, *a, **k):
        return 0

    def slider(self, *a, **k):
        return 0

    def date_input(self, *a, **k):
        return _dt.date.today()

    def time_input(self, *a, **k):
        return _dt.time(0, 0)

    def checkbox(self, *a, **k):
        return False

    def multiselect(self, *a, **k):
        return []

    # ---- 输出型 ----
    def write(self, *a, **k):
        return self

    def markdown(self, *a, **k):
        return self

    def caption(self, *a, **k):
        return self

    def subheader(self, *a, **k):
        return self

    def header(self, *a, **k):
        return self

    def divider(self, *a, **k):
        return self

    def json(self, *a, **k):
        return self

    def dataframe(self, *a, **k):
        return self

    def metric(self, *a, **k):
        return self

    def info(self, *a, **k):
        return self

    def warning(self, *a, **k):
        return self

    def error(self, *a, **k):
        return self

    def success(self, *a, **k):
        return self

    def __getattr__(self, name):
        if name == "columns":
            return lambda n: [El(self._button_policy) for _ in range(n if isinstance(n, int) else len(n))]
        if name == "tabs":
            return lambda *a, **k: tuple(El(self._button_policy) for _ in range(len(a[0]) if a and a[0] else 2))
        if name in ("button", "form_submit_button", "toggle",
                    "download_button", "link_button"):
            return lambda *a, **k: bool(self._button_policy(k.get("key", "")))
        return lambda *a, **k: El(self._button_policy)

    def __getitem__(self, k):
        return El(self._button_policy)

    def __iter__(self):
        return iter(())


class StubSession:
    def __init__(self, data=None):
        object.__setattr__(self, "_data", dict(data or {}))

    def reset(self, data=None):
        object.__setattr__(self, "_data", dict(data or {}))

    def __getattr__(self, k):
        return self._data.get(k)

    def __setattr__(self, k, v):
        self._data[k] = v

    def __getitem__(self, k):
        return self._data[k]

    def __setitem__(self, k, v):
        self._data[k] = v

    def __delitem__(self, k):
        del self._data[k]

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


class _St:
    def __init__(self, state, button_policy):
        self.session_state = state
        self._button_policy = button_policy
        self.sidebar = El(button_policy)

    def __getattr__(self, name):
        bp = self._button_policy
        if name == "columns":
            return lambda n: [El(bp) for _ in range(n if isinstance(n, int) else len(n))]
        if name == "tabs":
            return lambda *a, **k: tuple(El(bp) for _ in range(len(a[0]) if a and a[0] else 2))
        if name in ("selectbox", "radio", "select_slider"):
            def _sb(label=None, options=None, index=0, **k):
                opts = list(options or [])
                if not opts:
                    return ""
                try:
                    return opts[index or 0]
                except Exception:
                    return opts[0]
            return _sb
        if name == "multiselect":
            return lambda *a, **k: []
        if name == "date_input":
            return lambda *a, **k: _dt.date.today()
        if name == "time_input":
            return lambda *a, **k: _dt.time(0, 0)
        if name in ("number_input", "slider"):
            return lambda *a, **k: 0
        if name in ("text_input", "text_area"):
            return lambda *a, **k: ""
        if name in ("checkbox", "button", "form_submit_button", "toggle",
                    "download_button", "link_button"):
            return lambda *a, **k: bool(bp(k.get("key", "")))
        if name == "stop":
            return lambda *a, **k: (_ for _ in ()).throw(StopScript())
        if name == "rerun":
            return lambda *a, **k: (_ for _ in ()).throw(RerunScript())
        if name == "switch_page":
            return lambda *a, **k: (_ for _ in ()).throw(SwitchPage())
        if name in ("cache_resource", "cache_data", "cache"):
            def _cache(*a, **k):
                if len(a) >= 1 and callable(a[0]) and not k:
                    return lambda *aa, **kk: None
                return lambda f: (lambda *aa, **kk: None)
            return _cache
        if name == "fragment":
            # @st.fragment(run_every=...) / @st.fragment 都直接返回原函数，保证片段体可被冒烟执行
            def _frag(*a, **k):
                if len(a) == 1 and callable(a[0]) and not k:
                    return a[0]
                return lambda fn: fn
            return _frag
        return lambda *a, **k: El(bp)


def install(state: StubSession = None, user: dict = None, button_policy=None):
    """注入桩 streamlit。必须在 import 任何页面/组件之前调用。

    button_policy(key) -> bool：按按钮 key 决定是否"被点击"。默认全部 False。
    """
    if state is None:
        state = StubSession()
    if user is not None:
        state["user"] = user
    state["login_time"] = time.time()  # 必须是 float 时间戳
    stub = _St(state, button_policy or (lambda key: False))
    sys.modules["streamlit"] = stub
    return state
