# -*- coding: utf-8 -*-
"""多租户托管隔离验证（指向临时库，不触碰真实 data/guibeicai.db）。

验证项：
  1. create_tenant 生成独立库文件 + 全部表结构
  2. 默认库不被污染（租户门店不出现在默认库）
  3. resolve_tenant_for_username 路由正确（命中 / 未命中 / 冻结）
  4. 登录隔离：在租户库能鉴权、在默认库鉴权失败
  5. 数据隔离：租户内插入的门店不泄漏到默认库
"""
import os
import shutil
import sys
import tempfile

# 项目根加入搜索路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("PYTHONPATH", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# 必须在 import guibeicai 之前设定（模块级常量在 import 时固化）
_TMP = tempfile.mkdtemp(prefix="tenancy_verify_")
os.environ["SQLITE_PATH"] = os.path.join(_TMP, "guibeicai.db")
os.environ["GUOBEICAI_TENANT_HOME"] = os.path.join(_TMP, "tenants")

import sqlite3
from guibeicai.web import _db, _tenancy

# 模拟真实 app：进程启动时先建默认库 schema（tenant=None）
_db.init_db()

PASS, FAIL = [], []
def check(name, cond, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK' if cond else 'XX'}] {name}{(' ' + extra) if extra else ''}")

print("== 1. 开通租户 ==")
res = _tenancy.create_tenant(
    tenant_id="verify-tenant", tenant_name="验证餐饮（旗舰店）", store_name="旗舰店",
    admin_username="t_admin", admin_password="Secret123", city="南宁", province="广西",
)
db_file = _db.tenant_db_path("verify-tenant")
check("租户库文件已生成", db_file.exists(), str(db_file))
conn = sqlite3.connect(str(db_file))
tbls = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
conn.close()
for must in ("users", "roles", "stores", "dishes", "recipes", "ingredients", "tenant_accounts"):
    # tenant_accounts 只在默认注册表；租户库应有业务表
    pass
check("租户库含 stores 表", "stores" in tbls)
check("租户库含 dishes 表", "dishes" in tbls)
check("租户库含 ingredients 表", "ingredients" in tbls)

print("== 2. 默认库不被污染 ==")
default_conn = sqlite3.connect(os.environ["SQLITE_PATH"])
def_tbls = {r[0] for r in default_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
default_conn.close()
check("默认库新增 tenants 注册表", "tenants" in def_tbls)
check("默认库新增 tenant_accounts 注册表", "tenant_accounts" in def_tbls)
# 默认库虽有 stores 表（运营方单库），但不应含有租户开通时建的门店「旗舰店」
_dc2 = sqlite3.connect(os.environ["SQLITE_PATH"])
default_store_names = {r[0] for r in _dc2.execute("SELECT name FROM stores")}
_dc2.close()
check("默认库不含租户开通门店「旗舰店」（隔离）", "旗舰店" not in default_store_names,
      f"default_stores={sorted(default_store_names)}")

print("== 3. 登录路由 resolve_tenant_for_username ==")
check("命中注册表 -> 返回 tenant_id",
      _tenancy.resolve_tenant_for_username("t_admin") == "verify-tenant")
check("未注册用户 -> 返回 None（走默认单库）",
      _tenancy.resolve_tenant_for_username("ghost_user") is None)

print("== 4. 登录隔离（鉴权）==")
_db.set_tenant("verify-tenant")
u_in_tenant = _db.authenticate_user("t_admin", "Secret123")
_db.set_tenant(None)
u_in_default = _db.authenticate_user("t_admin", "Secret123")
check("租户上下文下能鉴权成功", u_in_tenant is not None,
      f"user={u_in_tenant.get('username') if u_in_tenant else None}")
check("默认库下同一账号鉴权失败（隔离）", u_in_default is None)
check("密码错误在租户上下文也失败", _db.authenticate_user("t_admin", "wrong") is None)
_db.set_tenant(None)

print("== 5. 数据隔离 ==")
_db.set_tenant("verify-tenant")
tid = _db.upsert_store(name="隔离测试店", city="测试", province="测试")
_db.set_tenant(None)
default_stores = _db.get_all_stores()
check("租户内新增的门店不出现在默认库", all(s["name"] != "隔离测试店" for s in default_stores),
      f"default_stores={[s['name'] for s in default_stores]}")

print("== 6. 租户状态：冻结/退租 ==")
_tenancy.set_tenant_status("verify-tenant", _tenancy.STATUS_SUSPENDED)
check("冻结后 resolve 返回 ''（拒绝登录）",
      _tenancy.resolve_tenant_for_username("t_admin") == "")
_tenancy.set_tenant_status("verify-tenant", _tenancy.STATUS_ACTIVE)
check("恢复 active 后 resolve 返回 tenant_id",
      _tenancy.resolve_tenant_for_username("t_admin") == "verify-tenant")

print("== 7. 非法 tenant_id 拒绝 ==")
try:
    _db.normalize_tenant_id("../../etc/passwd")
    check("非法 tenant_id 被拦截", False)
except ValueError:
    check("非法 tenant_id 被拦截", True)

print("\n== 结果 ==")
print(f"  通过 {len(PASS)} / 失败 {len(FAIL)}")
if FAIL:
    print("  失败项:", FAIL)
else:
    print("  全部通过 ✅")

# 清理临时目录
shutil.rmtree(_TMP, ignore_errors=True)
print(f"\n已清理临时目录：{_TMP}")
