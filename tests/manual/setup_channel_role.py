"""创建「渠道商」角色与测试账号（走官方 API，与页面创建等价；幂等）"""
import sys
sys.path.insert(0, r"D:/Project/guibeicaiV1.5")

import sqlite3, json
from guibeicai.web._db import (
    init_db, get_conn, add_role, get_role_by_code, add_user,
    get_user_store_ids, set_user_store_ids, can_access_page, get_accessible_stores,
)

init_db()

CODE = "channel"
NAME = "渠道商"
PAGES = ["dashboard", "channel_overview"]
PERMS = {"all": False, "predict": False, "upload": False, "compare": False,
         "user_manage": False, "role_manage": False, "store": False,
         "dish": False, "config": False}

role = get_role_by_code(CODE)
if role:
    print(f"[SKIP] 角色已存在 id={role.get('id')} code={CODE} pages={role.get('pages')}")
else:
    ok = add_role(CODE, NAME, "渠道/配送商只读视角：查看旗下门店跨店需求汇总与活跃度，不做预测与配置", PAGES)
    print("[CREATE] add_role ->", ok)
    conn = get_conn(); c = conn.cursor()
    c.execute("UPDATE roles SET permissions = ? WHERE code = ?", (json.dumps(PERMS, ensure_ascii=False), CODE))
    conn.commit(); conn.close()
    print("[SET] permissions 已写入（全 false）")

role = get_role_by_code(CODE)
print("  角色信息:", role)

# 测试账号
USERNAME = "channel_demo"
PWD = "Guibeicai@2026"
BIND_STORES = [2, 3]

conn = get_conn(); c = conn.cursor()
row = c.execute("SELECT id, role FROM users WHERE username = ?", (USERNAME,)).fetchone()
conn.close()
if row:
    uid = row[0]
    print(f"[SKIP] 用户已存在 id={uid}，更新绑定门店为 {BIND_STORES}")
    set_user_store_ids(uid, BIND_STORES)
else:
    ok = add_user(USERNAME, PWD, role=CODE, store_ids=BIND_STORES)
    print("[CREATE] add_user ->", ok)
    conn = get_conn(); c = conn.cursor()
    row = c.execute("SELECT id FROM users WHERE username = ?", (USERNAME,)).fetchone()
    conn.close()
    uid = row[0] if row else 0
    print(f"  新用户 id={uid} 初始密码={PWD}（首次登录强制改密）")

print("\n=== 权限边界验证 ===")
checks = [
    ("可访问 渠道工作台", can_access_page(CODE, "channel_overview") is True),
    ("可访问 工作台首页", can_access_page(CODE, "dashboard") is True),
    ("不可访问 执行预测", can_access_page(CODE, "predict_config") is False),
    ("不可访问 数据上传", can_access_page(CODE, "data_upload") is False),
    ("不可访问 门店管理", can_access_page(CODE, "store_manage") is False),
    ("不可访问 用户管理", can_access_page(CODE, "user_manage") is False),
    ("不可访问 角色管理", can_access_page(CODE, "role_manage") is False),
]
for name, cond in checks:
    print(f"  {'[PASS]' if cond else '[FAIL]'} {name}")

stores = get_accessible_stores({"id": uid, "role": CODE}, active_only=True)
print(f"\n  该账号可见门店: {[(s['id'], s['name']) for s in stores]}  绑定: {get_user_store_ids(uid)}")
print(f"  [{'PASS' if {s['id'] for s in stores} == set(get_user_store_ids(uid)) else 'FAIL'}] 可见范围 = 绑定门店")

fails = [n for n, c in checks if not c]
print(f"\n===== 结果: PASS {len(checks)-len(fails)} / FAIL {len(fails)} =====")
