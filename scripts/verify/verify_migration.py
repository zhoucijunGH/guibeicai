"""迁移崩溃安全验证（在临时副本上，不污染生产库）。"""
import os
import sys
import shutil
import tempfile
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PROD = str(ROOT / "data" / "guibeicai.db")
_tmp = os.path.join(tempfile.gettempdir(), "guibeicai_mig_verify.db")
for suf in ("", "-wal", "-shm"):
    if os.path.exists(_tmp + suf):
        os.remove(_tmp + suf)
shutil.copy(PROD, _tmp)
os.environ["SQLITE_PATH"] = _tmp
sys.path.insert(0, str(ROOT))

from guibeicai.web._db import init_db

checks = []
def ok(name, cond, detail=""):
    checks.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else ""))


def state():
    conn = sqlite3.connect(_tmp)
    c = conn.cursor()
    tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    au = c.execute("SELECT COUNT(*) FROM actual_sales").fetchone()[0] if "actual_sales" in tabs else None
    # 用 PRAGMA index_list 判唯一索引（自增唯一索引的 sql 在 sqlite_master 中为 NULL，不能用 LIKE 查）
    has_unique = False
    if "actual_sales" in tabs:
        try:
            c.execute("PRAGMA index_list(actual_sales)")
            has_unique = any(r[2] for r in c.fetchall())
        except Exception:
            has_unique = False
    conn.close()
    return tabs, au, has_unique


# 场景1：干净库首次迁移
init_db()
tabs, au, uniq = state()
ok("场景1 迁移后 actual_sales 存在", "actual_sales" in tabs)
ok("场景1 迁移后无 actual_sales_new 孤儿表", "actual_sales_new" not in tabs)
ok("场景1 actual_sales 带唯一索引", uniq is True, f"has_unique={uniq}")
ok("场景1 数据行数保持 3877", au == 3877, f"实际={au}")

# 场景2：模拟进程在写入阶段被杀死（actual_sales 旧表完整 + 孤儿 actual_sales_new 部分数据）
conn = sqlite3.connect(_tmp)
c = conn.cursor()
c.execute("""CREATE TABLE actual_sales_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT, store_id INTEGER NOT NULL, dish_id INTEGER NOT NULL,
    sale_date TEXT NOT NULL, actual_qty REAL DEFAULT 0, actual_waste REAL DEFAULT 0,
    notes TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(store_id, dish_id, sale_date))""")
c.execute("INSERT INTO actual_sales_new (store_id, dish_id, sale_date, actual_qty) VALUES (999, 888, '2020-01-01', 1)")
conn.commit(); conn.close()
init_db()
tabs, au, uniq = state()
ok("场景2 二次 init_db 不再崩溃", "actual_sales" in tabs)
ok("场景2 孤儿 actual_sales_new 已清理", "actual_sales_new" not in tabs)
ok("场景2 业务数据保持 3877（无脏数据混入）", au == 3877, f"实际={au}")

# 场景3：模拟进程在 DROP 旧表之后、RENAME 之前被杀死（旧表已删，新表为唯一权威数据）
conn = sqlite3.connect(_tmp)
c = conn.cursor()
# 先正常结构：把实际数据搬进 new，再删旧表
c.execute("CREATE TABLE actual_sales_new AS SELECT * FROM actual_sales")
c.execute("DROP TABLE actual_sales")
conn.commit(); conn.close()
init_db()
tabs, au, uniq = state()
ok("场景3 崩溃恢复后 actual_sales 存在", "actual_sales" in tabs)
ok("场景3 数据被保留（无丢失）", au is not None and au == 3877, f"实际={au}")
ok("场景3 无孤儿表", "actual_sales_new" not in tabs)

print("=" * 60)
fails = [c for c in checks if not c[1]]
print(f"TOTAL={len(checks)} PASS={len(checks)-len(fails)} FAIL={len(fails)}")
for suf in ("", "-wal", "-shm"):
    if os.path.exists(_tmp + suf):
        os.remove(_tmp + suf)
sys.exit(1 if fails else 0)
