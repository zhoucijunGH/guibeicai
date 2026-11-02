# -*- coding: utf-8 -*-
"""验证「一个渠道商租户 + 旗下多个门店账号」的数据库边界与登录路由。

场景：渠道商开通 = 1 个租户库；他在库内录 N 个门店并建 N 个门店账号，
这些账号必须能正确路由回自家租户库并鉴权，且不串到默认库。

指向临时库，不碰真实数据。
"""
import os
import sys
import shutil
import sqlite3
import tempfile

sys.path.insert(0, r"D:\Project\guibeicaiV1.5")

_TMP = tempfile.mkdtemp(prefix="tenancy_acct_")
os.environ["SQLITE_PATH"] = os.path.join(_TMP, "guibeicai.db")
os.environ["GUOBEICAI_TENANT_HOME"] = os.path.join(_TMP, "tenants")

from guibeicai.web import _db, _tenancy

# 真实 app 启动时会对默认库跑 init_db，这里同样先建好默认库 schema
_db.init_db()

_ok = True


def check(name, cond, extra=""):
    global _ok
    print(("  OK " if cond else "  XX ") + name + (f"  [{extra}]" if extra else ""))
    _ok = _ok and cond


# 1) 开通渠道商租户 -> 应只建 1 个库
res = _tenancy.create_tenant(
    "acme-channel", "某食材配送商", "配送中心",
    "chan_admin", "Admin123", city="南宁", province="广西",
)
check("开通渠道商=1个租户库", res["db_path"].replace("\\", "/").endswith("acme-channel/guibeicai.db"), res["db_path"])

# 2) 在租户库内录 2 个门店 + 建 2 个门店账号（模拟渠道商后台操作）
_db.set_tenant("acme-channel")
sid1 = _db.upsert_store("加盟店A", "南宁", "广西")
sid2 = _db.upsert_store("加盟店B", "南宁", "广西")
ok1 = _db.add_user("store_a", "Store123", "商家用户", [sid1])
ok2 = _db.add_user("store_b", "Store123", "商家用户", [sid2])
_db.set_tenant(None)
check("门店账号 store_a 建号成功", ok1)
check("门店账号 store_b 建号成功", ok2)

# 3) 登录路由：门店账号必须解析到自家租户（之前这里会返回 None -> 登录失败）
check("store_a 路由到 acme-channel",
      _tenancy.resolve_tenant_for_username("store_a") == "acme-channel")
check("store_b 路由到 acme-channel",
      _tenancy.resolve_tenant_for_username("store_b") == "acme-channel")
check("chan_admin 路由到 acme-channel",
      _tenancy.resolve_tenant_for_username("chan_admin") == "acme-channel")

# 4) 鉴权：门店账号能在自家库鉴权
_db.set_tenant(_tenancy.resolve_tenant_for_username("store_a"))
ua = _db.authenticate_user("store_a", "Store123")
_db.set_tenant(None)
check("store_a 鉴权成功(连自家库)", bool(ua))
check("store_a 只绑定加盟店A", bool(ua) and ua.get("store_id") == sid1)
_db.set_tenant(_tenancy.resolve_tenant_for_username("store_b"))
check("store_b 鉴权成功", bool(_db.authenticate_user("store_b", "Store123")))
_db.set_tenant(None)

# 5) 隔离：门店账号不在默认库（运营方单库）
def_conn = sqlite3.connect(str(_db.DB_PATH))
found_in_default = def_conn.execute("SELECT 1 FROM users WHERE username='store_a'").fetchone()
def_conn.close()
check("store_a 不泄漏进默认库", found_in_default is None)

# 6) 租户库数据存在性：含首店(配送中心)+2 加盟店、含 3 个目标账号
_db.set_tenant("acme-channel")
store_names = {r[0] for r in _db.get_conn().execute("SELECT name FROM stores")}
user_names = {r[0] for r in _db.get_conn().execute("SELECT username FROM users")}
_db.set_tenant(None)
check("租户库含加盟店A", "加盟店A" in store_names, sorted(store_names))
check("租户库含加盟店B", "加盟店B" in store_names)
check("租户库含渠道+门店3账号", {"chan_admin", "store_a", "store_b"} <= user_names, sorted(user_names))

# 7) 反向：默认库里运营方查不到加盟店
reg = sqlite3.connect(str(_db.DB_PATH))
def_stores = {r[0] for r in reg.execute("SELECT name FROM stores")}
reg.close()
check("默认库不含加盟店A(隔离)", "加盟店A" not in def_stores, f"default_stores={sorted(def_stores)}")

print("\n结果:", "全部通过" if _ok else "存在失败项")
shutil.rmtree(_TMP, ignore_errors=True)
sys.exit(0 if _ok else 1)
