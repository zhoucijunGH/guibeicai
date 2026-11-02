"""角色/授权数据模型功能验证（隔离库，不污染生产数据）。
验证：迁移产出角色、admin=["*"] 全通、merchant 仅业务页通、增删改角色、改用户角色。
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent

# 用临时库，避免污染生产 data/guibeicai.db
_tmp = os.path.join(tempfile.gettempdir(), "guibeicai_role_verify.db")
if os.path.exists(_tmp):
    os.remove(_tmp)
if os.path.exists(_tmp + "-wal"):
    os.remove(_tmp + "-wal")
if os.path.exists(_tmp + "-shm"):
    os.remove(_tmp + "-shm")
os.environ["SQLITE_PATH"] = _tmp

sys.path.insert(0, str(ROOT))

from guibeicai.web._db import (
    init_db, get_all_roles, get_role_by_code, can_access_page, is_admin_role,
    add_role, update_role, delete_role, get_all_users, update_user,
    DEFAULT_MERCHANT_PAGES,
)
from guibeicai.web._pages import all_page_keys

checks = []


def ok(name, cond, detail=""):
    checks.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


init_db()

# 1) 角色数量与编码/名称
roles = {r["code"]: r for r in get_all_roles()}
ok("存在 admin 角色", "admin" in roles)
ok("存在 merchant 角色", "merchant" in roles)
ok("admin 编码=admin 名称=系统管理员", roles.get("admin", {}).get("name") == "系统管理员")
ok("merchant 编码=merchant 名称=商家用户", roles.get("merchant", {}).get("name") == "商家用户")

# 2) admin 页面权限 = ["*"]
admin_pages = roles.get("admin", {}).get("pages")
ok('admin pages == ["*"]', admin_pages == ["*"], str(admin_pages))

# 3) merchant 默认业务页
merchant_pages = set(roles.get("merchant", {}).get("pages", []))
ok("merchant 默认页 == DEFAULT_MERCHANT_PAGES",
   merchant_pages == set(DEFAULT_MERCHANT_PAGES),
   f"缺: {set(DEFAULT_MERCHANT_PAGES) - merchant_pages}")

# 4) 访问矩阵：admin 全通 / merchant 仅业务页通
all_keys = all_page_keys()
ok("页面注册表共 16 个键", len(all_keys) == 16, f"实际={len(all_keys)}")
admin_matrix = {k: can_access_page("admin", k) for k in all_keys}
merchant_matrix = {k: can_access_page("merchant", k) for k in all_keys}
ok("admin 可访问全部 16 页", all(admin_matrix.values()))
missing_for_merchant = [k for k in all_keys if k in DEFAULT_MERCHANT_PAGES and not merchant_matrix[k]]
ok("merchant 可访问全部默认业务页", not missing_for_merchant, f"缺: {missing_for_merchant}")

# 5) 未知角色无权限（防越权）
ok("未知角色(foo)任何页均无权限", not can_access_page("foo", "dashboard"))

# 6) 增角色 + 配页面 + 撤销页面（验证纯前端可调，不碰代码）
assert add_role("viewer", "只读访客", "仅查看预测", ["dashboard", "predict_overview"])
ok("新增角色 viewer 成功", get_role_by_code("viewer") is not None)
ok("viewer 初仅能访问 dashboard/predict_overview",
   can_access_page("viewer", "dashboard") and can_access_page("viewer", "predict_overview")
   and not can_access_page("viewer", "user_manage"))
# 用户管理等价操作：给 viewer 增加 user_manage
update_role(get_role_by_code("viewer")["id"], pages=["dashboard", "predict_overview", "user_manage"])
ok("改角色页面后 viewer 可访问 user_manage", can_access_page("viewer", "user_manage"))
# 改名（展示名变动不影响逻辑编码）
vid = get_role_by_code("viewer")["id"]
update_role(vid, name="只读访客(改名)")
ok("改名后编码不变仍可定位", get_role_by_code("viewer")["name"] == "只读访客(改名)")
# 删角色（无关联用户）
ok("删除 viewer 角色成功", delete_role(vid))
ok("删除后角色不存在", get_role_by_code("viewer") is None)

# 7) 用户管理等价操作：改用户角色
users = {u["username"]: u for u in get_all_users()}
ok("admin 用户 role 存编码 admin", users.get("admin", {}).get("role") == "admin")
ok("test 用户 role 存编码 merchant", users.get("test", {}).get("role") == "merchant")
test_uid = users["test"]["id"]
update_user(test_uid, role="admin")
ok("test 改角色为 admin 后 is_admin 判定为真",
   is_admin_role(get_all_users_dict := {u['id']: u for u in get_all_users()}[test_uid]["role"]))
# 还原
update_user(test_uid, role="merchant")
ok("test 角色还原为 merchant",
   {u['id']: u for u in get_all_users()}[test_uid]["role"] == "merchant")

# 8) 改角色编码时同步用户（update_role 联动）
assert add_role("ops", "运营", "运营角色", ["dashboard"])
ops_id = get_role_by_code("ops")["id"]
# 把一个用户绑到 ops
update_user(test_uid, role="ops")
update_role(ops_id, code="ops2")
synced = {u['id']: u for u in get_all_users()}[test_uid]["role"]
ok("改角色编码 ops->ops2 时同步用户 role", synced == "ops2", f"test.role={synced}")
# 清理：把 test 还原 merchant，删 ops2
update_user(test_uid, role="merchant")
delete_role(get_role_by_code("ops2")["id"])

print("=" * 60)
fails = [c for c in checks if not c[1]]
print(f"TOTAL={len(checks)} PASS={len(checks)-len(fails)} FAIL={len(fails)}")
if os.path.exists(_tmp):
    os.remove(_tmp)
if os.path.exists(_tmp + "-wal"):
    os.remove(_tmp + "-wal")
if os.path.exists(_tmp + "-shm"):
    os.remove(_tmp + "-shm")
sys.exit(1 if fails else 0)
