# -*- coding: utf-8 -*-
"""多租户三场景验证：直签门店 / 渠道签约 / 平台管理员。

指向临时库，不污染真实数据。验证点：
  场景① 直签门店：入口账号可为 merchant（单店老板）或 admin（连锁管理员），
               且角色存的是编码（can_access_page 能匹配到页面）。
  场景② 渠道签约：入口账号为 channel，且渠道能自助在库内建门店账号
               （add_user 自动登记路由 → 门店账号可独立登录、绑定自家门店）。
  场景③ 平台管理员：用户名不在注册表 → 走默认库（resolve 返回 None）。
  额外：入口角色非法（非编码）应抛 ValueError。
"""
import os
import sys
import shutil
import tempfile

TMP = tempfile.mkdtemp(prefix="tenancy_scenarios_")
os.environ["SQLITE_PATH"] = os.path.join(TMP, "guibeicai.db")
os.environ["GUOBEICAI_TENANT_HOME"] = os.path.join(TMP, "tenants")
sys.path.insert(0, r"D:\Project\guibeicaiV1.5")

from guibeicai.web import _db, _tenancy

PASS, FAIL = [], []
def check(name, ok, extra=""):
    (PASS if ok else FAIL).append(name)
    print(("  OK  " if ok else "  XX  ") + name + (f"  -> {extra}" if extra else ""))

# 默认库 schema 就绪（真实 app 启动时做，测试里补上）
_db.init_db()

def auth_as_tenant(tid, username, password):
    """模拟真实登录：先切到租户库，再认证（认证发生在租户上下文内）。"""
    _db.set_tenant(tid)
    try:
        return _db.authenticate_user(username, password)
    finally:
        _db.set_tenant(None)

# ---------- 场景① 直签门店（单店老板 = merchant） ----------
res1 = _tenancy.create_tenant(
    "direct-store", "南宁老友粉（望州店）", "望州店",
    admin_username="boss", admin_password="Boss123",
    entry_role="merchant",
)
check("① 直签门店：租户库生成", os.path.exists(res1["db_path"]), res1["db_path"])
check("① 入口账号角色存的是编码 merchant", res1["entry_role"] == "merchant")
auth1 = auth_as_tenant("direct-store", "boss", "Boss123")
check("① 入口账号可登录", bool(auth1))
check("① merchant 能访问 data_upload 页", bool(auth1) and _db.can_access_page(auth1["role"], "data_upload"))
check("① merchant 不能访问 contract_manage（运营方页）",
      bool(auth1) and not _db.can_access_page(auth1["role"], "contract_manage"))

# ---------- 场景② 渠道签约（channel） ----------
res2 = _tenancy.create_tenant(
    "acme-channel", "ACME 食材配送", "配送中心",
    admin_username="chan", admin_password="Chan123",
    entry_role="channel",
)
check("② 渠道签约：租户库生成", os.path.exists(res2["db_path"]))
check("② 入口账号角色=channel(编码)", res2["entry_role"] == "channel")
auth2 = auth_as_tenant("acme-channel", "chan", "Chan123")
check("② 渠道账号可登录", bool(auth2))
check("② 渠道能访问 channel_store 页", bool(auth2) and _db.can_access_page("channel", "channel_store"))
check("② 渠道能访问 channel_store_account 页（可自助建门店账号）",
      bool(auth2) and _db.can_access_page("channel", "channel_store_account"))
check("② 渠道不应访问 user_manage 页（该页仅管理员）",
      bool(auth2) and not _db.can_access_page("channel", "user_manage"))

# 渠道在库内为门店建登录账号
_db.set_tenant("acme-channel")
sid = _db.upsert_store(name="加盟店A", city="南宁", province="广西")
ok_add = _db.add_user("store_a", "Store123", "merchant", store_ids=[sid])
_db.set_tenant(None)
check("② 渠道建的门店账号写入成功", ok_add)
# 门店账号登录路由：先查注册表拿租户
tid_resolved = _tenancy.resolve_tenant_for_username("store_a")
check("② 门店账号已登记登录路由", tid_resolved == "acme-channel", str(tid_resolved))
auth_store = auth_as_tenant("acme-channel", "store_a", "Store123")
check("② 门店账号可独立登录", bool(auth_store))
# 查绑定关系需切到租户库（get_user_store_ids 走当前租户上下文）
_db.set_tenant("acme-channel")
sa_stores = _db.get_user_store_ids(auth_store["id"]) if auth_store else []
_db.set_tenant(None)
check("② 门店账号只绑定自家门店", bool(auth_store) and sa_stores == [sid], str(sa_stores))

# ---------- 场景③ 平台管理员 ----------
tid_none = _tenancy.resolve_tenant_for_username("superadmin_platform")
check("③ 平台管理员（注册表无记录）走默认库", tid_none is None)
# 默认库里建一个平台管理员，验证能进运营方页
_db.add_user("superadmin_platform", "Root123", "admin", store_ids=[0])
auth_admin = auth_as_tenant(None, "superadmin_platform", "Root123")
check("③ 平台管理员可登录默认库", bool(auth_admin))
check("③ 平台管理员能访问 contract_manage（运营方页）",
      bool(auth_admin) and _db.can_access_page("admin", "contract_manage"))

# ---------- 隔离性：渠道门店不串默认库 ----------
default_conn = _db.get_conn()
try:
    has = default_conn.execute("SELECT 1 FROM users WHERE username='store_a'").fetchone()
finally:
    default_conn.close()
check("隔离：门店账号 store_a 不出现在默认库", has is None)

# ---------- 入口角色非法应报错 ----------
try:
    _tenancy.create_tenant("bad", "坏角色", "店", "x", "y", entry_role="不存在的角色")
    check("入口角色非法应抛 ValueError", False)
except ValueError:
    check("入口角色非法应抛 ValueError", True)

# ---------- 汇总 ----------
print(f"\n通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("失败项：", FAIL)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
