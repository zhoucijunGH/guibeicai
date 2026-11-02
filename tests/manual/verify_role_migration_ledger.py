"""验证角色页面迁移「一次性」台账：管理员在角色管理页取消某页面后，
后续 init_db() 不应再把页面强制加回。

使用临时 SQLite（SQLITE_PATH 指向临时文件），不触碰真实演示库。
"""
import os
import sys
import tempfile

_tmp = tempfile.mktemp(suffix=".db")
os.environ["SQLITE_PATH"] = _tmp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from guibeicai.web import _db

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)

# ---- 第一次 init_db：台账为空，三类页面迁移应各跑一次（补齐历史库） ----
_db.init_db()

chan = _db.get_role_by_code("channel")
mer = _db.get_role_by_code("merchant")
# 注：渠道角色按设计【不】含 user_manage（账号管理仅管理员可用），见 2026-09-09 决策。
check("首次 init_db 渠道不含 user_manage（符合设计）", "user_manage" not in (chan["pages"] or []))
check("首次 init_db 商家含 pos_integration（回填）", "pos_integration" in (mer["pages"] or []))

# 管理员在角色管理页取消渠道的 channel_store（模拟取消勾选并保存）
TARGET = "channel_store"
check("渠道初始含 channel_store", TARGET in (chan["pages"] or []))
new_chan_pages = [p for p in (chan["pages"] or []) if p != TARGET]
_db.update_role(chan["id"], code=chan["code"], name=chan["name"],
                description=chan.get("description") or "", pages=new_chan_pages)
chan2 = _db.get_role_by_code("channel")
check(f"保存后渠道不含 {TARGET}", TARGET not in (chan2["pages"] or []))

# ---- 关键：再次 init_db（模拟用户切换到其他页面/下次加载） ----
_db.init_db()
chan3 = _db.get_role_by_code("channel")
check(f"再次 init_db 后渠道仍不含 {TARGET}（未被迁移加回）", TARGET not in (chan3["pages"] or []))

# ---- 台账应记录已实装的一次性迁移（roles_add_channel_user_manage 从未实装，不再断言） ----
conn = _db.get_conn()
applied = {r[0] for r in conn.execute("SELECT name FROM _migrations").fetchall()}
conn.close()
check("台账记录了 roles_add_pos_page", "roles_add_pos_page" in applied)
check("台账记录了 roles_add_channel_pages", "roles_add_channel_pages" in applied)

# 清理
try:
    os.remove(_tmp)
except OSError:
    pass

print("\nPASS:", len(PASS), "FAIL:", len(FAIL))
for p in PASS:
    print("  ✓", p)
for f in FAIL:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
