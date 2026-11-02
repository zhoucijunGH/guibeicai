"""临时库验证：小程序增强相关的 DB 改动。
- get_actual_sales_reconciled_by_store_date 返回 {dish_id: reconciled}
- get_all_users 透出 openid（供用户管理页微信绑定列展示）
不触碰真实演示库（SQLITE_PATH 指向临时文件）。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ["SQLITE_PATH"] = tmp

from guibeicai.web import _db as db

db.init_db()
conn = db.get_conn()
c = conn.cursor()

# 建门店 + 菜品
c.execute("INSERT INTO stores(name, city, status) VALUES (?, ?, ?)", ("测试店", "X", "active"))
sid = c.lastrowid
c.execute("INSERT INTO dishes(store_id, name) VALUES (?, ?)", (sid, "红烧肉"))
did1 = c.lastrowid
c.execute("INSERT INTO dishes(store_id, name) VALUES (?, ?)", (sid, "清蒸鱼"))
did2 = c.lastrowid
# 两条实际销量：一条 POS 自动(reconciled=1)，一条手工(reconciled=0)
c.execute(
    "INSERT INTO actual_sales(store_id, dish_id, sale_date, actual_qty, reconciled) VALUES (?, ?, ?, ?, ?)",
    (sid, did1, "2026-09-11", 10, 1),
)
c.execute(
    "INSERT INTO actual_sales(store_id, dish_id, sale_date, actual_qty, reconciled) VALUES (?, ?, ?, ?, ?)",
    (sid, did2, "2026-09-11", 5, 0),
)
conn.commit()
conn.close()

# --- 验证 A：数据来源标记 ---
rec = db.get_actual_sales_reconciled_by_store_date(sid, "2026-09-11")
assert rec.get(did1) == 1, f"POS 自动应=1, got {rec.get(did1)}"
assert rec.get(did2) == 0, f"手工应=0, got {rec.get(did2)}"
print("✅ A 数据来源标记：POS自动=1 / 手工=0 正确")

# --- 验证 B：get_all_users 透出 openid ---
conn = db.get_conn()
c = conn.cursor()
c.execute("INSERT INTO users(username, password_hash, role, openid) VALUES (?, ?, ?, ?)",
          ("boss", "x", "merchant", "wxabc123456"))
conn.commit()
conn.close()

users = db.get_all_users()
u = [x for x in users if x["username"] == "boss"][0]
assert u.get("openid") == "wxabc123456", f"openid 未透出: {u.get('openid')}"
print("✅ B get_all_users 透出 openid 正确")

print("🎉 全部验证通过")
os.remove(tmp)
