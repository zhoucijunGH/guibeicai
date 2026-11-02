"""页面级冒烟：mock streamlit 后 importlib 执行 7_用户管理.py 顶层代码。

目的：验证 ImportError 修复后页面能完整跑过（含列表行渲染分支），
不依赖真实 Streamlit / 不碰真实库（SQLITE_PATH 指向临时库）。

用法： python tests/manual/smoke_page_user_manage.py
"""
import datetime
import importlib.util
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

# 临时库（先设 env，再 import _db）
_tmpdir = tempfile.mkdtemp(prefix="smoke_user_mgr_")
os.environ["SQLITE_PATH"] = os.path.join(_tmpdir, "smoke.db")


# ---------------- fake streamlit ----------------
class _Stop(Exception):
    pass


class _Rerun(Exception):
    pass


class _Switch(Exception):
    pass


_WIDGET_FALSE = {"button", "form_submit_button", "checkbox", "toggle",
                 "download_button", "link_button"}


class El:
    def __getattr__(self, name):
        # 未点击的按钮/复选框应返回 False（真实 Streamlit 语义），
        # 否则 cols[i].button(...) 会返回真值、误触发其分支里的 st.rerun()。
        if name in _WIDGET_FALSE:
            return lambda *a, **k: False

        def _f(*a, **k):
            return El()
        return _f

    def __call__(self, *a, **k):
        return El()

    def __getitem__(self, k):
        return El()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class SS:
    def __init__(self, d=None):
        object.__setattr__(self, "_d", dict(d or {}))

    def __getattr__(self, k):
        if k.startswith("_"):
            raise AttributeError(k)
        return self._d.get(k)

    def __setattr__(self, k, v):
        self._d[k] = v

    def __getitem__(self, k):
        return self._d[k]

    def __setitem__(self, k, v):
        self._d[k] = v

    def __contains__(self, k):
        return k in self._d

    def get(self, k, d=None):
        return self._d.get(k, d)

    def pop(self, k, d=None):
        return self._d.pop(k, d)

    def update(self, *a, **k):
        return self._d.update(*a, **k)


class FakeSt(types.ModuleType):
    def __init__(self):
        super().__init__("streamlit")
        self.session_state = SS({
            "user": {"id": 1, "username": "admin", "role": "admin",
                     "valid_from": None, "valid_until": None},
            "login_time": __import__("time").time(),
        })
        self.sidebar = El()

    def __getattr__(self, name):
        if name in ("cache_resource", "cache_data"):
            def deco(*a, **k):
                def wrap(f):
                    return f
                return wrap
            return deco
        if name == "columns":
            def _c(spec, *a, **k):
                n = spec if isinstance(spec, int) else len(spec)
                return [El() for _ in range(n)]
            return _c
        if name in ("button", "form_submit_button", "checkbox", "toggle", "radio"):
            if name == "radio":
                def _r(label, options=None, index=0, **k):
                    options = options or []
                    try:
                        return options[index or 0]
                    except Exception:
                        return options[0] if options else None
                return _r
            return lambda *a, **k: False
        if name == "date_input":
            def _d(*a, **k):
                return k.get("value") if "value" in k else datetime.date.today()
            return _d
        if name in ("number_input", "slider"):
            def _n(*a, **k):
                return k.get("value") if "value" in k else 0
            return _n
        if name == "selectbox":
            def _s(label, options=None, index=0, **k):
                options = options or []
                try:
                    return options[index or 0]
                except Exception:
                    return options[0] if options else None
            return _s
        if name == "multiselect":
            def _m(*a, **k):
                return k.get("default") or []
            return _m
        if name == "text_input":
            def _t(*a, **k):
                return k.get("value") if k.get("value") is not None else ""
            return _t
        if name in ("text_area", "number_input_area"):
            return lambda *a, **k: ""
        if name in ("expander", "form", "container", "popover"):
            return lambda *a, **k: El()
        if name == "tabs":
            def _tabs(*a, **k):
                spec = a[0] if a else [1]
                n = len(spec) if isinstance(spec, (list, tuple)) else 1
                return tuple(El() for _ in range(n))
            return _tabs
        if name == "stop":
            return lambda: (_ for _ in ()).throw(_Stop())
        if name == "rerun":
            return lambda *a, **k: (_ for _ in ()).throw(_Rerun())
        if name == "switch_page":
            return lambda *a, **k: (_ for _ in ()).throw(_Switch())
        # 其余任何属性：当成返回 El 的可调用 / 上下文对象
        return El()


sys.modules["streamlit"] = FakeSt()

# ---------------- 预置数据（先 init_db 再播种）----------------
from guibeicai.web import _db  # noqa: E402

_db.init_db()

# 先建一个合法门店，拿到真实 store id（外键开启，绑定门店必须存在）
with _db._tx() as _c:
    _cur = _c.cursor()
    _cur.execute("INSERT INTO stores(name, city, status) VALUES (?, ?, ?)", ("冒烟门店", "X", "active"))
    _sid = _cur.lastrowid
print(f"[seed] 门店 store_id = {_sid}")

# 种两个用户：一个已绑微信 + 带有效期；一个未绑 + 无手机号
ok1 = _db.add_user("boss_a", "pw123456", "merchant", [_sid], phone="13800000001")
u1 = _db.get_user_by_phone("13800000001")
if u1:
    _db.bind_openid(u1["id"], "oSMOKE123456")
    _db.update_user(u1["id"], valid_from="2026-01-01", valid_until="2027-12-31")

ok2 = _db.add_user("boss_b", "pw123456", "merchant", [], phone=None)

_seed = _db.get_all_users()
print(f"[seed] add_user ok1={ok1} ok2={ok2}; 用户数={len(_seed)}")

# ---------------- 执行页面顶层代码 ----------------
PAGE = os.path.join(ROOT, "guibeicai", "web", "pages", "7_用户管理.py")
spec = importlib.util.spec_from_file_location("page_user_manage_smoke", PAGE)
mod = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(mod)
    print("PASS: 页面顶层代码执行完毕（无异常）")
    rc = 0
except (_Stop, _Rerun, _Switch) as e:
    print(f"PASS(stop/rerun/switch): {type(e).__name__}")
    rc = 0
except Exception as e:  # noqa: BLE001
    import traceback
    traceback.print_exc()
    print(f"FAIL: {type(e).__name__}: {e}")
    rc = 1

# 断言：get_all_users 透出 phone 且绑定的 openid 可查
users = _db.get_all_users()
assert all("phone" in u for u in users), "get_all_users 未透出 phone 字段"
assert _db.get_user_by_openid("oSMOKE123456"), "绑定的 openid 应可查到（=小程序登录读取路径）"
print("ASSERT OK: phone 字段透出 + openid 可查")

try:
    import shutil
    shutil.rmtree(_tmpdir, ignore_errors=True)
except Exception:
    pass

sys.exit(rc)
