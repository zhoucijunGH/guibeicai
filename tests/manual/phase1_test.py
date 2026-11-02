"""Phase 1 自测：轻架构重构 + 预测逻辑补全前置
- #6 统一鉴权：_auth 模块可导入、require_auth 存在
- #7 SQLite 加固：get_conn 应用 WAL/busy_timeout/foreign_keys；DB 路径可经 env 覆盖；DB_BACKEND 判定
- #10 调度单实例守卫 + 12-factor：文件锁获取/过期清理逻辑；调度时间读 env
- #11 无状态化准备：配置项 12-factor 化、模块可导入
- 全量语法编译：guibeicai/**/*.py 无语法错误（覆盖 8 页面鉴权重构）
"""
import os
import sys
import tempfile
import py_compile

# 必须在导入 _db 前设置，使其 DB_PATH 指向临时库（隔离真实数据）
_TMP = tempfile.mkdtemp(prefix="phase1_")
os.environ["SQLITE_PATH"] = os.path.join(_TMP, "test.db")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL = 0, 0
def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"[PASS] {name} {extra}")
    else:
        FAIL += 1; print(f"[FAIL] {name} {extra}")

print("=== Phase 1 自测 ===")

# ---------- 全量语法编译（覆盖所有编辑过的文件）----------
import glob
py_files = glob.glob(os.path.join("guibeicai", "**", "*.py"), recursive=True)
compile_err = []
for f in py_files:
    try:
        py_compile.compile(f, doraise=True)
    except py_compile.PyCompileError as e:
        compile_err.append((f, str(e)))
if compile_err:
    for f, e in compile_err:
        print(f"  COMPILE ERROR {f}: {e[:200]}")
else:
    print(f"  （已编译 {len(py_files)} 个 .py 文件，无语法错误）")
check("全量语法编译通过", len(compile_err) == 0, f"(共 {len(py_files)} 个文件, 错误 {len(compile_err)})")

# ---------- #6 统一鉴权 ----------
try:
    from guibeicai.web._auth import require_auth, SESSION_TIMEOUT_SECONDS
    import inspect
    sig = inspect.signature(require_auth)
    check("#6 _auth 模块可导入", True)
    check("#6 require_auth 支持 required_role 参数", "required_role" in sig.parameters, f"({sig})")
    check("#6 会话超时默认 8h", SESSION_TIMEOUT_SECONDS == 28800, f"({SESSION_TIMEOUT_SECONDS}s)")
except Exception as e:
    check("#6 _auth 模块可导入", False, f"-> {type(e).__name__}: {e}")

# ---------- #7 SQLite 加固 + 抽象层 ----------
try:
    from guibeicai.web import _db
    check("#7 DB_BACKEND 默认 sqlite", _db.DB_BACKEND == "sqlite", f"({_db.DB_BACKEND})")
    check("#7 DB_PATH 指向临时库(已隔离)", str(_db.DB_PATH).startswith(_TMP), f"({_db.DB_PATH})")
    conn = _db.get_conn()
    cur = conn.cursor()
    jm = cur.execute("PRAGMA journal_mode").fetchone()[0]
    bt = cur.execute("PRAGMA busy_timeout").fetchone()[0]
    fk = cur.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.close()
    check("#7 journal_mode=WAL", jm.lower() == "wal", f"({jm})")
    check("#7 busy_timeout=30000", bt == 30000, f"({bt})")
    check("#7 foreign_keys=ON", fk == 1, f"({fk})")
    # PG 切换判定
    os.environ["DATABASE_URL"] = "postgresql://u:p@h:5432/db"
    import importlib
    importlib.reload(_db)
    check("#7 DATABASE_URL 前缀触发 PG backend", _db.DB_BACKEND == "postgres", f"({_db.DB_BACKEND})")
    del os.environ["DATABASE_URL"]
    importlib.reload(_db)
except Exception as e:
    check("#7 SQLite 加固", False, f"-> {type(e).__name__}: {e}")

# ---------- #10 调度单实例守卫 + 12-factor ----------
try:
    from guibeicai.web import _scheduler as sch
    import os as _os
    lock = os.path.join(_TMP, "scheduler.lock")
    sch._SCHEDULER_LOCK = type(sch._SCHEDULER_LOCK)(lock)
    # 首次获取
    r1 = sch._acquire_scheduler_lock()
    check("#10 首次获取锁成功", r1 is True)
    # 存活 pid 二次获取应失败
    r2 = sch._acquire_scheduler_lock()
    check("#10 存活进程二次获取被拒", r2 is False)
    # 模拟过期锁（死 pid）
    with open(lock, "w", encoding="utf-8") as f:
        f.write("999999")
    r3 = sch._acquire_scheduler_lock()
    check("#10 过期锁自动清理并接管", r3 is True)
    sch._release_scheduler_lock()
    # 释放后锁应被移除
    check("#10 退出释放锁", not os.path.exists(lock) or open(lock).read().strip() != str(_os.getpid()))
    # 12-factor 调度时间
    from guibeicai.config.settings import settings
    check("#10 调度小时可读(env 默认 6)", settings.scheduler_hour == int(os.getenv("SCHEDULER_HOUR", "6")), f"({settings.scheduler_hour})")
    check("#10 调度分钟可读(env 默认 0)", settings.scheduler_minute == int(os.getenv("SCHEDULER_MINUTE", "0")), f"({settings.scheduler_minute})")
except Exception as e:
    check("#10 调度守卫", False, f"-> {type(e).__name__}: {e}")

print(f"\n=== 结果: PASS={PASS} FAIL={FAIL} ===")
sys.exit(0 if FAIL == 0 else 1)
