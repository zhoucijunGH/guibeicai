"""验证批量导入模块（数据初始化）。

重要：通过 SQLITE_PATH 指向临时库，绝不污染真实演示数据。
用法：python tests/manual/verify_bulk_import.py

覆盖：门店导入（含 bind_user_id 绑定）、菜品配方导入（自动建菜品+食材）、幂等。
（供应商相关导入已在 #158 移除；clear_all_business_data 已删除，生产割接改为删库重建。）
"""

import os
import sys
import tempfile

# 注入项目根到 sys.path，确保从任意目录运行都能 import guibeicai
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 必须在导入 guibeicai 之前设置，DB_PATH 在模块加载时即解析
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False, prefix="gb_verify_")
os.environ["SQLITE_PATH"] = _tmp.name

import pandas as pd  # noqa: E402
from guibeicai.web import _db as db  # noqa: E402
from guibeicai.web._db import (  # noqa: E402
    init_db, get_all_stores, get_dishes_by_store, get_ingredients_by_store,
    add_user, get_user_store_ids,
)
from guibeicai.web import _bulk_import as bi  # noqa: E402

fail = 0


def check(cond, msg):
    global fail
    if cond:
        print(f"  ✅ {msg}")
    else:
        print(f"  ❌ {msg}")
        fail += 1


print("== 初始化临时库 ==")
init_db()

# ---------- 1. 门店 ----------
print("\n[1] 门店导入")
stores_df = pd.DataFrame([
    {"门店": "验证门店A", "省份": "广西壮族自治区", "城市": "南宁市", "老板邮箱": "a@x.com", "自动预测": "是", "预测时间": "06:00", "预测目标日": "明天"},
    {"门店": "验证门店B", "省份": "广西壮族自治区", "城市": "柳州市", "自动预测": "否"},
])
r = bi.import_stores_csv(stores_df)
check(r["created"] == 2, f"门店新增 2（实际 {r['created']}）")
check(len(get_all_stores(active_only=False)) == 2, "系统门店数=2")

# 重导一次应走更新不新增
r2 = bi.import_stores_csv(stores_df)
check(r2["created"] == 0 and r2["updated"] == 2, f"门店幂等：新增 {r2['created']} 更新 {r2['updated']}")

# ---------- 2. 菜品配方（自动建菜品+食材） ----------
print("\n[2] 菜品配方导入（自动建菜品与食材）")
rec_df = pd.DataFrame([
    {"门店": "验证门店A", "菜品": "老友粉", "菜品单价": 15, "食材": "米粉", "品类": "米面", "每份用量": 0.2, "单位": "kg", "食材单价": 6},
    {"门店": "验证门店A", "菜品": "老友粉", "菜品单价": 15, "食材": "酸笋", "品类": "干货", "每份用量": 0.05, "单位": "kg", "食材单价": 12},
    {"门店": "验证门店A", "菜品": "柠檬鸭", "菜品单价": 48, "食材": "鸭肉", "品类": "肉类", "每份用量": 0.5, "单位": "kg", "食材单价": 22},
])
r = bi.import_recipes_csv(rec_df)
check(r["created"] >= 1, f"菜品/食材创建（实际新增菜品 {r['created']}）")
store_a = next(s for s in get_all_stores(active_only=False) if s["name"] == "验证门店A")
check(len(get_dishes_by_store(store_a["id"], active_only=False)) == 2, "门店A 菜品=2")
check(len(get_ingredients_by_store(store_a["id"], active_only=False)) >= 1, "门店A 食材>=1")

# ---------- 3. bind_user_id 绑定归属 ----------
print("\n[3] 门店导入 bind_user_id 绑定到账号")
ok_user = add_user("bindowner", "P@ssw0rd", "channel", store_ids=[], authorized_by=None)
check(ok_user is True, "创建绑定归属账号成功")
uid = next(u["id"] for u in db.get_all_users() if u["username"] == "bindowner")
r = bi.import_stores_csv(stores_df, bind_user_id=uid)
bound = get_user_store_ids(uid)
check(len(bound) == 2, f"bind_user_id 后该账号绑定门店=2（实际 {len(bound)}）")

print("\n==============================")
if fail == 0:
    print("✅ 全部验证通过")
else:
    print(f"❌ 失败 {fail} 项")
print(f"临时库：{_tmp.name}（验证结束，可手动删除）")
