"""验证「渠道门店账号」隔离与归属（方案A）。

重要：通过 SQLITE_PATH 指向临时库，绝不污染真实演示数据。
用法：python tests/manual/verify_channel_store_account.py

验证点：
1. 渠道角色 pages 正确：含 channel_store_account / data_init，不含 user_manage；
2. get_visible_store_accounts 对管理员返回全部门店账号，对渠道仅返回「门店全部属于自身范围」的商家用户；
3. 渠道看不到管理员 / 渠道账号；
4. 渠道建号写入 authorized_by，归属统计可定位；
5. 门店导入 bind_user_id 绑定到渠道账号后，渠道可见范围扩大。
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="gb_csa_")
os.environ["SQLITE_PATH"] = _tmp.name

from guibeicai.web import _db as db  # noqa: E402
from guibeicai.web._db import (  # noqa: E402
    init_db, add_user, add_user_store, get_all_users, get_user_store_ids,
    get_visible_store_accounts, get_accessible_stores, get_role_by_code,
)
from guibeicai.web import _bulk_import as bi  # noqa: E402
import pandas as pd  # noqa: E402

fail = 0


def check(cond, msg):
    global fail
    if cond:
        print(f"  ✅ {msg}")
    else:
        print(f"  ❌ {msg}")
        fail += 1


def uid_of(username):
    return next(u["id"] for u in get_all_users() if u["username"] == username)


print("== 初始化临时库 ==")
init_db()

# ---- 角色 pages 校验 ----
print("\n[1] 渠道角色页面权限")
role = get_role_by_code("channel")
pages = role["pages"]
check("channel_store_account" in pages, "渠道含 channel_store_account 页")
check("data_init" in pages, "渠道含 data_init 页")
check("user_manage" not in pages, "渠道不含 user_manage（杜绝越权改密/删号）")
check("channel_store" in pages, "渠道含 channel_store 页")

# ---- 造数据：门店 + 账号 ----
print("\n[2] 造数：门店 S1/S2/S3 + 管理员/渠道/商家账号")
s1 = db.upsert_store("S1", "南宁市", "广西壮族自治区")
s2 = db.upsert_store("S2", "柳州市", "广西壮族自治区")
s3 = db.upsert_store("S3", "桂林市", "广西壮族自治区")

add_user("admin01", "x", "admin", store_ids=[], authorized_by=None)
add_user("channel01", "x", "channel", store_ids=[], authorized_by=None)
add_user("merchant_in", "x", "merchant", store_ids=[s1], authorized_by=None)      # 门店属渠道范围
add_user("merchant_out", "x", "merchant", store_ids=[s3], authorized_by=None)     # 门店越权
# 渠道仅可访问 S1/S2
add_user_store(uid_of("channel01"), s1)
add_user_store(uid_of("channel01"), s2)

channel_user = next(u for u in get_all_users() if u["username"] == "channel01")
admin_user = next(u for u in get_all_users() if u["username"] == "admin01")

print("\n[3] 管理员视图（全部门店账号）")
admin_accounts = get_visible_store_accounts(admin_user)
admin_names = {a["username"] for a in admin_accounts}
check("merchant_in" in admin_names and "merchant_out" in admin_names, "管理员可见全部门店账号（含越权门店的商家）")
# 注：init_db 会创建默认测试账号 test（商家用户），故数量 >=2 即可，重点校成员可见性
check(len(admin_accounts) >= 2, f"管理员门店账号数>=2（实际 {len(admin_accounts)}）")

print("\n[4] 渠道视图（仅自身范围）")
channel_accounts = get_visible_store_accounts(channel_user)
channel_names = {a["username"] for a in channel_accounts}
check("merchant_in" in channel_names, "渠道可见自身范围内门店账号 merchant_in")
check("merchant_out" not in channel_names, "渠道不可见越权门店账号 merchant_out")
check("admin01" not in channel_names and "channel01" not in channel_names,
      "渠道看不到管理员/渠道账号（仅商家用户）")

print("\n[5] 渠道建号归属 authorized_by")
add_user("merchant_auth", "x", "merchant", store_ids=[s2], authorized_by=uid_of("channel01"))
reload_channel = get_visible_store_accounts(channel_user)
auth_row = next((a for a in reload_channel if a["username"] == "merchant_auth"), None)
check(auth_row is not None, "渠道可见归属自建的 merchant_auth")
check(auth_row is not None and auth_row.get("authorized_by") == uid_of("channel01"),
      "authorized_by 记录为渠道账号 id（归属统计可用）")

print("\n[6] 门店导入 bind_user_id 扩大渠道可见范围")
df = pd.DataFrame([
    {"门店": "S4", "省份": "广西壮族自治区", "城市": "北海市", "自动预测": "否"},
])
bi.import_stores_csv(df, bind_user_id=uid_of("channel01"))
bound = get_user_store_ids(uid_of("channel01"))
s4 = db.upsert_store("S4", "北海市", "广西壮族自治区")  # 已存在则取同记录 id
check(s4 in bound, f"导入门店 S4 已绑定渠道账号（bound 含 {s4}）")
# 现在把 merchant_out 迁到 S4（渠道范围），渠道应可见
db.set_user_store_ids(uid_of("merchant_out"), [s4])
reload2 = get_visible_store_accounts(channel_user)
check("merchant_out" in {a["username"] for a in reload2}, "渠道范围内新增门店后，该门店商家账号转为可见")

print("\n==============================")
if fail == 0:
    print("✅ 全部验证通过")
else:
    print(f"❌ 失败 {fail} 项")
print(f"临时库：{_tmp.name}（验证结束，可手动删除）")
