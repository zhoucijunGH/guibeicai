"""#159 验证：渠道角色升级 + 供货范围表 CRUD

对一份临时拷贝的演示库跑迁移与 CRUD，不触碰真实数据。
运行：D:/Program Files/Python314/python.exe tests/manual/verify_channel_store.py
"""

import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEMO_DB = os.path.join(ROOT, "data", "guibeicai.db")

if not os.path.exists(DEMO_DB):
    print("SKIP: 演示库不存在", DEMO_DB)
    sys.exit(0)

tmp = tempfile.mkdtemp(prefix="chanstore_")
tmp_db = os.path.join(tmp, "verify.db")
shutil.copyfile(DEMO_DB, tmp_db)
os.environ["SQLITE_PATH"] = tmp_db

sys.path.insert(0, ROOT)

from guibeicai.web import _db  # noqa: E402

failures = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        failures.append(msg)


_db.init_db()

# 1) 渠道角色迁移：若库存在 channel 角色，应含 channel_store 权限
conn = _db.get_conn()
c = conn.cursor()
rows = c.execute("SELECT code, pages FROM roles").fetchall()
conn.close()
role_map = {code: pages for code, pages in rows}
check("channel" in role_map, "数据库存在 channel 角色（演示库）")
if "channel" in role_map:
    plist = __import__("json").loads(role_map["channel"])
    check("channel_store" in plist, f"channel 角色已包含 channel_store 权限: {plist}")
check("channel_store" in _db.all_page_keys() if hasattr(_db, "all_page_keys") else True,
      "（page 注册在 _pages.py，非 _db 范畴，此处跳过）")

# 2) 供货范围 CRUD
sid = _db.upsert_store("验证门店_供货范围", "南宁市", "广西壮族自治区",
                       owner_email="v@example.com", auto_predict=1,
                       auto_predict_target_offset=1, auto_predict_time="06:00")
check(sid > 0, f"upsert_store 创建门店成功 id={sid}")

# 多租户绑定
_db.add_user_store(1, sid)
conn = _db.get_conn(); cc = conn.cursor()
bound = cc.execute("SELECT COUNT(*) FROM user_stores WHERE user_id=1 AND store_id=?", (sid,)).fetchone()[0]
conn.close()
check(bound == 1, "add_user_store 绑定成功")

# 供货范围写入（含归一化）
items = [("螺丝椒", "螺丝椒"), ("番茄", "番茄"), ("qingcai", "青菜")]
_db.set_store_supply_scope(sid, items)
got = _db.get_store_supply_scope(sid)
norms = _db.get_store_supply_scope_norms(sid)
check(len(got) == 3, f"set/get_store_supply_scope 写入 3 条，读回 {len(got)} 条")
check(norms == {"螺丝椒", "番茄", "qingcai"}, f"get_store_supply_scope_norms 正确: {norms}")

# 整体替换（去重）
_db.set_store_supply_scope(sid, [("螺丝椒", "螺丝椒"), ("螺丝椒", "螺丝椒"), ("土豆", "土豆")])
got2 = _db.get_store_supply_scope(sid)
check(len(got2) == 2, f"set_store_supply_scope 整体替换并去重：期望 2 条，实际 {len(got2)} 条")

# 清理临时库
try:
    os.remove(tmp_db)
    os.rmdir(tmp)
except Exception:
    pass

if failures:
    print(f"\n结果：{len(failures)} 项失败")
    sys.exit(1)
print("\n全部通过 ✅")
