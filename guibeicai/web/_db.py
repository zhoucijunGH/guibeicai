"""桂备菜 - 数据库层（SQLite，分布式就绪：可换 PG）

连接创建统一收敛到 get_conn()，未来切换到 PostgreSQL 时只需在此根据
DATABASE_URL 前缀改写连接实现（上层 raw SQL 需同步适配方言）。当前单机云部署
使用 SQLite，并启用 WAL / busy_timeout / foreign_keys 提升并发安全与健壮性。
"""

import base64
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

import pandas as pd

from guibeicai.web import _crypto
from guibeicai.web import _net

logger = logging.getLogger(__name__)


# ---- 名称归一化：用于菜品/食材去重判定 ----
# 全角 → 半角（ASCII 区间 0x21~0x7E 与全角 0xFF01~0xFF5E 一一对应）
_FULL2HALF = {chr(0xFF01 + i): chr(0x21 + i) for i in range(0x5E)}
# 常见繁体 → 简体（仅覆盖餐饮高频字，避免引入 opencc 等重依赖）
_TRAD2SIMP = {
    "雞": "鸡", "鴨": "鸭", "魚": "鱼", "蝦": "虾", "蟹": "蟹", "豬": "猪", "牛": "牛", "羊": "羊",
    "麵": "面", "飯": "饭", "湯": "汤", "燒": "烧", "烤": "烤", "炒": "炒", "炸": "炸", "燉": "炖",
    "滷": "卤", "醬": "酱", "蒜": "蒜", "蔥": "葱", "薑": "姜", "薺": "荠", "筍": "笋", "菇": "菇",
    "蘿": "萝", "蔔": "卜", "茄": "茄", "豆": "豆", "腐": "腐", "蛋": "蛋", "腸": "肠", "腳": "脚",
    "頭": "头", "內": "内", "臟": "脏", "腩": "腩", "翅": "翅", "骨": "骨", "肉": "肉", "米": "米",
    "粉": "粉", "條": "条", "餃": "饺", "包": "包", "饅": "馒", "餅": "饼", "粥": "粥", "涼": "凉",
    "冰": "冰", "熱": "热", "辣": "辣", "酸": "酸", "甜": "甜", "鹽": "盐", "糖": "糖", "醋": "醋",
    "油": "油", "酒": "酒", "茶": "茶", "貝": "贝", "參": "参", "藥": "药", "補": "补", "鍋": "锅",
    "爐": "炉", "燙": "烫", "滾": "滚", "燜": "焖", "蒸": "蒸", "煮": "煮", "拌": "拌", "滷味": "卤味",
}
# 需剔除的括号对（含中文方括号、书名号等）
_BRACKET_PAIRS = [("(", ")"), ("（", "）"), ("[", "]"), ("【", "】"), ("<", ">"), ("《", "》")]


def normalize_name(name: str) -> str:
    """
    名称归一化，用于菜品/食材的去重判定。

    处理链：全角转半角 → 繁体转简体 → 去除括号及其内容 → 去除所有空白 → 转小写。
    例：「老友粉(大份)」→「老友粉」、「檸檬 鴨」→「柠檬鸭」
    """
    s = str(name or "")
    s = "".join(_FULL2HALF.get(ch, ch) for ch in s)
    s = "".join(_TRAD2SIMP.get(ch, ch) for ch in s)
    # 去除括号及其内容（支持嵌套的最外层剥离，重复处理直到稳定）
    for _ in range(3):
        changed = False
        for lp, rp in _BRACKET_PAIRS:
            while lp in s and rp in s:
                i = s.find(lp)
                j = s.find(rp, i + 1)
                if j == -1:
                    s = s.replace(lp, "", 1)
                    changed = True
                    continue
                s = s[:i] + s[j + 1:]
                changed = True
        if not changed:
            break
    # 去除所有空白字符（含全角空格）
    s = "".join(ch for ch in s if not ch.isspace() and ch != "\u3000")
    return s.strip().lower()


# 允许的重量/体积单位别名映射（导入/LLM/配方录入统一归一化入口）
_UNIT_ALIAS = {
    "kg": "kg", "千克": "kg", "公斤": "kg", "kgs": "kg",
    "g": "g", "克": "g", "grams": "g", "gram": "g",
    "l": "L", "L": "L", "升": "L", "公升": "L", "liters": "L", "liter": "L",
    "ml": "ml", "毫升": "ml", "milliliter": "ml", "milliliters": "ml",
    "个": "个", "只": "只", "片": "片", "份": "份",
}


def canonicalize_unit(quantity: float, unit: str, fallback_count_weight_kg: float = 0.05):
    """
    把食材用量/单位统一归一化为 kg 或 L（程序内部标准量纲）。

    规则：
    - g / 克 → kg：quantity /= 1000，unit_price 保持不变（系统内单价均按 kg/L 计）。
    - ml / 毫升 → L：quantity /= 1000，unit_price 保持不变。
    - kg / L 保持不变。
    - 计数单位（个/只/片/份）默认按 0.05 kg/个换算为 kg，并记录警告；
      若调用方不希望自动换算，可将 fallback_count_weight_kg 设为 None，此时返回原值并 is_count=True。

    返回：{"quantity": float, "unit": str, "changed": bool, "is_count": bool}
    """
    import warnings
    qty = float(quantity or 0)
    u = str(unit or "").strip().lower()
    # 统一全角/特殊符号
    u = "".join(_FULL2HALF.get(ch, ch) for ch in u)
    u = _UNIT_ALIAS.get(u, u)

    changed = False
    is_count = False

    if u in ("kg", "kgs", "千克", "公斤"):
        return {"quantity": qty, "unit": "kg", "changed": False, "is_count": False}
    if u in ("g", "克", "grams", "gram"):
        return {"quantity": qty / 1000.0, "unit": "kg", "changed": True, "is_count": False}
    if u in ("l", "升", "公升", "liters", "liter"):
        return {"quantity": qty, "unit": "L", "changed": False, "is_count": False}
    if u in ("ml", "毫升", "milliliter", "milliliters"):
        return {"quantity": qty / 1000.0, "unit": "L", "changed": True, "is_count": False}
    if u in ("个", "只", "片", "份"):
        is_count = True
        if fallback_count_weight_kg is not None and qty > 0:
            warnings.warn(f"计数单位「{unit}」按默认 {fallback_count_weight_kg} kg/{unit} 换算为 kg")
            return {
                "quantity": qty * float(fallback_count_weight_kg),
                "unit": "kg",
                "changed": True,
                "is_count": True,
            }
        return {"quantity": qty, "unit": unit, "changed": False, "is_count": True}

    # 未知单位：原样返回（让上层决定是否拒绝）
    return {"quantity": qty, "unit": unit, "changed": False, "is_count": False}


# ---- 12-factor：数据库位置可经环境变量覆盖（默认 data/guibeicai.db）----
_DEFAULT_DB = Path(__file__).resolve().parent.parent.parent / "data" / "guibeicai.db"
DB_PATH = Path(os.getenv("SQLITE_PATH", str(_DEFAULT_DB))).resolve()

# ===================== 多租户托管：租户 → 独立库文件 =====================
# 托管模型：**单进程共享实例 + 每租户独立 SQLite 文件**。
#   - 一个部署实例同时服务多家餐饮/渠道客户，每家一份 <tenant>.db，物理隔离不串数据；
#   - 运行时由 _tenant_ctx 决定"当前这次请求/会话连哪个库"，调用方无需感知（get_conn 自动路由）；
#   - 未设置租户（本地开发 / 现有单机部署）时，行为与改造前完全一致 —— 向后兼容。
# 相比"每租户起一个进程"，共享实例不随租户数线性涨内存；相比"单库 + store_id 逻辑隔离"，
# 独立文件让备份/迁移/退租按客户维度一键完成，故障与误操作也不会跨租户扩散。
_TENANT_HOME = Path(
    os.getenv("GUOBEICAI_TENANT_HOME", str(_DEFAULT_DB.parent / "tenants"))
).resolve()

# 租户上下文：Web 侧在每个页面渲染前(require_auth)、API 侧在每个请求认证后设置。
# ContextVar 天然隔离并发（Streamlit 会话 / FastAPI 请求），避免串到别人的库。
_tenant_ctx: ContextVar[str | None] = ContextVar("guibeicai_tenant_id", default=None)

# 租户 id 白名单字符：只允许 小写字母/数字/短横线/下划线，杜绝路径穿越与怪异文件名。
_TENANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")


def normalize_tenant_id(tenant_id: str) -> str:
    """校验并返回规范化的租户 id（非法则抛 ValueError）。

    约束：3–63 位、小写字母/数字/-/_、必须以字母或数字开头。
    """
    tid = (tenant_id or "").strip().lower()
    if not _TENANT_ID_RE.match(tid):
        raise ValueError(
            "租户标识非法：仅允许小写字母/数字/短横线/下划线，3–63 位且以字母或数字开头"
        )
    return tid


def set_tenant(tenant_id: str | None) -> str | None:
    """设置当前上下文的租户（None 表示回到默认单库）。返回实际生效值。"""
    tid = normalize_tenant_id(tenant_id) if tenant_id else None
    _tenant_ctx.set(tid)
    return tid


def get_tenant() -> str | None:
    """读取当前上下文的租户 id（未设置返回 None）。"""
    return _tenant_ctx.get()


def tenant_db_path(tenant_id: str) -> Path:
    """给定租户 id，返回其库文件路径：<tenant_home>/<tenant_id>/guibeicai.db"""
    return _TENANT_HOME / normalize_tenant_id(tenant_id) / "guibeicai.db"


def resolve_db_path() -> Path:
    """按当前上下文解析应连接的库文件路径（get_conn 的唯一依据）。"""
    tid = _tenant_ctx.get()
    if not tid:
        return DB_PATH
    return tenant_db_path(tid)


# 未来换 PG 时：DATABASE_URL 以 postgresql:// 开头即走 PG 实现（此处预留判定）
_DATABASE_URL = os.getenv("DATABASE_URL", "")
DB_BACKEND = "postgres" if _DATABASE_URL.startswith(("postgresql://", "postgres://")) else "sqlite"

# 连接级 PRAGMA（SQLite）：WAL 提升读写并发、busy_timeout 避免锁等待崩溃、
# foreign_keys 保证引用完整性、synchronous=NORMAL 在 WAL 下兼顾安全与性能。
_SQLITE_PRAGMAS = (
    "PRAGMA journal_mode=WAL;",
    "PRAGMA busy_timeout=30000;",
    "PRAGMA foreign_keys=ON;",
    "PRAGMA synchronous=NORMAL;",
)


def _ensure_db_dir():
    resolve_db_path().parent.mkdir(parents=True, exist_ok=True)


def get_conn():
    """获取数据库连接（唯一入口）。

    多租户托管下，连接目标由当前上下文的租户决定（见 set_tenant / resolve_db_path）；
    未设置租户时连接默认单库，行为与单机部署一致。
    当前 SQLite 实现；切换 PG 时改写此函数即可，调用方无需改动。
    """
    _ensure_db_dir()
    conn = sqlite3.connect(str(resolve_db_path()), timeout=30)
    for pragma in _SQLITE_PRAGMAS:
        try:
            conn.execute(pragma)
        except sqlite3.Error as e:  # pragma 失败不应阻断业务
            logger.warning("[db] PRAGMA 执行失败 %s: %s", pragma, e)
    return conn


@contextmanager
def _tx():
    """写事务上下文：正常提交、异常回滚、**始终关闭连接**。

    统一收敛写操作，杜绝「异常路径忘记 close」造成的连接泄漏。
    被泄漏的连接会保留一个未结束的写事务，持续占着 SQLite 的写锁，
    使之后**所有**写操作长时间报 `database is locked`（busy_timeout=30s 也会耗尽），
    直到进程重启 —— 影响面极大，因此写操作应尽量走本上下文。
    """
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def hash_password(pwd: str) -> str:
    """密码哈希：scrypt + 随机盐（零依赖，替代原 SHA-256 无盐）。

    存储格式：scrypt$<n>$<r>$<p>$<salt_b64>$<hash_b64>。
    旧版纯 SHA-256 十六进制哈希由 verify_password 兼容，并在登录成功时自动升级。
    """
    salt = os.urandom(16)
    n, r, p = 16384, 8, 1
    dk = hashlib.scrypt(pwd.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        n, r, p, base64.b64encode(salt).decode(), base64.b64encode(dk).decode()
    )


def verify_password(stored: str, pwd: str) -> bool:
    """校验密码：支持 scrypt 与旧版 SHA-256 两种格式。

    使用 hmac.compare_digest 做恒定时间比较，降低时序侧信道风险。
    """
    if not stored or not pwd:
        return False
    if stored.startswith("scrypt$"):
        try:
            _, n_s, r_s, p_s, salt_b64, hash_b64 = stored.split("$")
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(hash_b64)
            dk = hashlib.scrypt(
                pwd.encode("utf-8"), salt=salt, n=int(n_s), r=int(r_s), p=int(p_s), dklen=32
            )
            return hmac.compare_digest(dk, expected)
        except Exception:
            return False
    # 旧版：纯 SHA-256 十六进制（无盐）
    return hmac.compare_digest(stored, hashlib.sha256(pwd.encode()).hexdigest())


def init_db():
    """初始化所有表"""
    with _tx() as conn:
        c = conn.cursor()

        # ---- 门店表 ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS stores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                province TEXT NOT NULL DEFAULT '',
                city TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                owner_email TEXT DEFAULT '',
                wecom_webhook TEXT DEFAULT '',
                auto_predict INTEGER NOT NULL DEFAULT 1,
                auto_predict_target_offset INTEGER NOT NULL DEFAULT 1,
                auto_predict_time TEXT DEFAULT '06:00',
                last_auto_predict_at TEXT DEFAULT '',
                last_auto_predict_status TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)

        # ---- 用户门店关联表（多对多） ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_stores (
                user_id INTEGER NOT NULL,
                store_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, store_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE CASCADE
            )
        """)

        # ---- 渠道供货范围（门店需求的子集）----
        # 表达「门店需要多种食材、但某渠道商只供应其中几样」：
        # 该门店的食材需求由门店上传/自动生成，渠道商在此勾选它实际供货的种类。
        # 归一化食材名(ingredient_norm)与 ingredients 口径一致（跨门店同名同概念）。
        c.execute("""
            CREATE TABLE IF NOT EXISTS store_supply_scope (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                ingredient_norm TEXT NOT NULL,
                ingredient_name TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(store_id, ingredient_norm),
                FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE CASCADE
            )
        """)

        # ---- 菜品表 ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS dishes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                category TEXT DEFAULT '',
                standard_price REAL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (store_id) REFERENCES stores(id)
            )
        """)

        # ---- 配方表 ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS recipes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dish_id INTEGER NOT NULL,
                ingredient_name TEXT NOT NULL,
                quantity REAL NOT NULL,
                unit TEXT DEFAULT '',
                unit_price REAL DEFAULT 0,
                category TEXT DEFAULT '',
                FOREIGN KEY (dish_id) REFERENCES dishes(id)
            )
        """)

        # ---- 食材主数据表（按门店独立；唯一键 store_id + name） ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS ingredients (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                display_name TEXT DEFAULT '',
                category TEXT DEFAULT '',
                base_unit TEXT DEFAULT 'kg',
                unit_price REAL DEFAULT 0,
                price_updated_at TEXT,
                source TEXT DEFAULT 'llm',
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (store_id) REFERENCES stores(id),
                UNIQUE(store_id, name)
            )
        """)

        # ---- 预测结果表 ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                predict_date TEXT NOT NULL,
                run_time TEXT NOT NULL,
                special_event TEXT DEFAULT '',
                revenue REAL DEFAULT 0,
                cost REAL DEFAULT 0,
                gross_margin REAL DEFAULT 0,
                result_json TEXT DEFAULT '{}',
                FOREIGN KEY (store_id) REFERENCES stores(id)
            )
        """)

        # ---- 实际销量表（带 UNIQUE 约束，支持 upsert） ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS actual_sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                dish_id INTEGER NOT NULL,
                sale_date TEXT NOT NULL,
                actual_qty REAL DEFAULT 0,
                actual_waste REAL DEFAULT 0,
                amount REAL DEFAULT 0,
                notes TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(store_id, dish_id, sale_date),
                FOREIGN KEY (store_id) REFERENCES stores(id),
                FOREIGN KEY (dish_id) REFERENCES dishes(id)
            )
        """)
        # [AMOUNT] 兼容已存在的库：缺失该列时安全补加（POS 拉取的金额字段落位点）
        try:
            c.execute("ALTER TABLE actual_sales ADD COLUMN amount REAL DEFAULT 0")
        except Exception:
            pass  # 列已存在

        # ---- 用户表 ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT '商家用户',
                store_id INTEGER DEFAULT 0,
                openid TEXT DEFAULT NULL,
                phone TEXT DEFAULT NULL,
                force_change_pwd INTEGER NOT NULL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        # [OPENID] 兼容已存在的库：缺失该列时安全补加（唯一性由 _migrate_users_add_openid 的部分唯一索引保证）
        try:
            c.execute("ALTER TABLE users ADD COLUMN openid TEXT DEFAULT NULL")
        except Exception:
            pass  # 列已存在
        # [S1] 首次登录强制改密标记（兼容旧库）
        try:
            c.execute("ALTER TABLE users ADD COLUMN force_change_pwd INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # 列已存在

        # ---- 合同表（仅管理员可见，按合同期限管控账号有效期）----
        c.execute("""
            CREATE TABLE IF NOT EXISTS contracts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                contract_no TEXT DEFAULT '',
                name TEXT DEFAULT '',
                amount REAL DEFAULT 0,
                payment_method TEXT DEFAULT '',
                party_a TEXT DEFAULT '',
                party_b TEXT DEFAULT '',
                sign_date TEXT DEFAULT '',
                effective_date TEXT DEFAULT '',
                expiry_date TEXT DEFAULT '',
                linked_user_id INTEGER DEFAULT NULL,
                attachment_path TEXT DEFAULT '',
                attachment_name TEXT DEFAULT '',
                remark TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)

        # ---- 角色表 ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                description TEXT DEFAULT '',
                permissions TEXT DEFAULT '{}',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)

        # ---- 历史销量表 ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS sales_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                dish_id INTEGER NOT NULL,
                sale_date TEXT NOT NULL,
                qty REAL DEFAULT 0,
                unit_price REAL DEFAULT 0,
                revenue REAL DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(store_id, dish_id, sale_date),
                FOREIGN KEY (store_id) REFERENCES stores(id),
                FOREIGN KEY (dish_id) REFERENCES dishes(id)
            )
        """)

        # ---- 日期影响因素表 ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS date_factors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                factor_date TEXT NOT NULL,
                weather_text TEXT DEFAULT '',
                temperature REAL DEFAULT 0,
                is_holiday INTEGER DEFAULT 0,
                special_event TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(store_id, factor_date),
                FOREIGN KEY (store_id) REFERENCES stores(id)
            )
        """)

        # ---- 回测偏差校正表（误差反馈闭环：回测→校正因子→下次预测应用） ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS bias_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                dish_name TEXT NOT NULL,
                factor REAL NOT NULL DEFAULT 1.0,
                updated_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(store_id, dish_name),
                FOREIGN KEY (store_id) REFERENCES stores(id)
            )
        """)

        # ---- 回测运行记录表 ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS backtest_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER,
                run_time TEXT DEFAULT (datetime('now', 'localtime')),
                holdout_days INTEGER DEFAULT 7,
                min_history_days INTEGER DEFAULT 30,
                summary_json TEXT DEFAULT '{}'
            )
        """)

        # ---- 操作审计表（#15：谁在何时对什么做了什么，支撑安全审计与责任追溯）----
        c.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT DEFAULT (datetime('now', 'localtime')),
                user_id INTEGER DEFAULT 0,
                username TEXT DEFAULT '',
                role TEXT DEFAULT '',
                action TEXT NOT NULL,
                target_type TEXT DEFAULT '',
                target_id TEXT DEFAULT '',
                detail_json TEXT DEFAULT '{}',
                result TEXT DEFAULT 'ok',
                error_msg TEXT DEFAULT '',
                ip TEXT DEFAULT ''
            )
        """)

        # ---- POS/收银数据源对接配置表（#商业化：通用可配置数据源） ----
        c.execute("""
            CREATE TABLE IF NOT EXISTS pos_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                store_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL DEFAULT 'http_json',
                base_url TEXT NOT NULL DEFAULT '',
                auth_type TEXT NOT NULL DEFAULT 'none',
                auth_config TEXT DEFAULT '{}',
                field_mapping TEXT DEFAULT '{}',
                request_config TEXT DEFAULT '{}',
                schedule TEXT DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                last_sync_at TEXT,
                last_sync_status TEXT DEFAULT '',
                last_sync_msg TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now','localtime')),
                FOREIGN KEY (store_id) REFERENCES stores(id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS pos_sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                store_id INTEGER,
                run_at TEXT,
                status TEXT DEFAULT '',
                msg TEXT DEFAULT '',
                FOREIGN KEY (source_id) REFERENCES pos_sources(id) ON DELETE CASCADE
            )
        """)


    # 迁移台账（确保「一次性」迁移只跑一次，避免覆盖管理员在角色管理页的配置）
    _ensure_migration_ledger()

    # 自动迁移
    _migrate_users_add_store_id()
    _migrate_stores_add_province()
    _migrate_stores_add_push_fields()
    _migrate_user_stores_table()
    _migrate_actual_sales_unique()
    _migrate_recipes_unit_text()
    _migrate_roles_add_code_pages()
    # 以下三类「角色页面」迁移属一次性回填，用台账保证只跑一次，
    # 否则每次 init_db 都会把管理员在角色管理页取消的页面强制加回。
    _run_once("roles_add_pos_page", _migrate_roles_add_pos_page)
    _run_once("roles_add_channel_pages", _migrate_roles_add_channel_pages)
    # 渠道商页面权限刷新：移除越权的 user_manage，新增独立的渠道门店账号页与数据初始化页。
    # 用新台账键确保历史库（曾带 user_manage）也能被修正一次。
    _run_once("roles_channel_scoped_pages", _migrate_roles_channel_scoped_pages)
    _migrate_users_role_to_code()
    _migrate_dish_bom_fields()
    _migrate_recipes_ingredient_id()
    _migrate_users_add_openid()
    _migrate_users_add_phone()
    _migrate_stores_add_auto_predict()
    _migrate_predictions_add_pushed()
    _migrate_pos_sources_add_request_config()
    _migrate_stores_add_predict_target()
    _migrate_stores_add_alert_push()
    _ensure_alert_push_log()
    _migrate_actual_sales_reconciled()
    _migrate_users_valid_period()
    _migrate_users_add_authorized_by()
    _migrate_units_to_kg_l()

    # 初始化默认数据：先建角色，再建用户，角色用稳定编码、用户 role 挂编码
    init_roles()
    init_users()


def _migrate_users_add_store_id():
    """兼容已有数据库：users 表加 store_id 列"""
    with _tx() as conn:
        c = conn.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "store_id" not in cols:
            c.execute("ALTER TABLE users ADD COLUMN store_id INTEGER DEFAULT 0")


def _migrate_stores_add_province():
    """兼容已有数据库：stores 表加 province 列"""
    with _tx() as conn:
        c = conn.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(stores)").fetchall()]
        if "province" not in cols:
            c.execute("ALTER TABLE stores ADD COLUMN province TEXT NOT NULL DEFAULT ''")


def _migrate_stores_add_push_fields():
    """兼容已有数据库：stores 表加 owner_email / wecom_webhook 推送字段"""
    conn = get_conn()
    try:
        c = conn.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(stores)").fetchall()]
        for col, ddl in (
            ("owner_email", "ALTER TABLE stores ADD COLUMN owner_email TEXT DEFAULT ''"),
            ("wecom_webhook", "ALTER TABLE stores ADD COLUMN wecom_webhook TEXT DEFAULT ''"),
        ):
            if col not in cols:
                try:
                    c.execute(ddl)
                    conn.commit()
                except Exception:
                    conn.rollback()
    finally:
        conn.close()


def _migrate_stores_add_auto_predict():
    """兼容已有数据库：stores 表加 auto_predict / last_auto_predict_at / last_auto_predict_status。"""
    conn = get_conn()
    try:
        c = conn.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(stores)").fetchall()]
        for col, ddl in (
            ("auto_predict", "ALTER TABLE stores ADD COLUMN auto_predict INTEGER NOT NULL DEFAULT 1"),
            ("last_auto_predict_at", "ALTER TABLE stores ADD COLUMN last_auto_predict_at TEXT DEFAULT ''"),
            ("last_auto_predict_status", "ALTER TABLE stores ADD COLUMN last_auto_predict_status TEXT DEFAULT ''"),
        ):
            if col not in cols:
                try:
                    c.execute(ddl)
                    conn.commit()
                except Exception:
                    conn.rollback()
    finally:
        conn.close()


def _migrate_predictions_add_pushed():
    """兼容已有数据库：predictions 表加 pushed 标志（采购单推送去重）。"""
    conn = get_conn()
    try:
        c = conn.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(predictions)").fetchall()]
        if "pushed" not in cols:
            try:
                c.execute("ALTER TABLE predictions ADD COLUMN pushed INTEGER NOT NULL DEFAULT 0")
                conn.commit()
            except Exception:
                conn.rollback()
    finally:
        conn.close()


def _migrate_stores_add_predict_target():
    """兼容已有数据库：stores 表加 auto_predict_target_offset / auto_predict_time。"""
    conn = get_conn()
    try:
        c = conn.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(stores)").fetchall()]
        for col, ddl in (
            ("auto_predict_target_offset", "ALTER TABLE stores ADD COLUMN auto_predict_target_offset INTEGER NOT NULL DEFAULT 1"),
            ("auto_predict_time", "ALTER TABLE stores ADD COLUMN auto_predict_time TEXT DEFAULT '06:00'"),
        ):
            if col not in cols:
                try:
                    c.execute(ddl)
                    conn.commit()
                except Exception:
                    conn.rollback()
    finally:
        conn.close()


def _migrate_stores_add_alert_push():
    """兼容已有数据库：stores 表加 alert_push_enabled（预警自动推送开关）。"""
    conn = get_conn()
    try:
        c = conn.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(stores)").fetchall()]
        if "alert_push_enabled" not in cols:
            try:
                c.execute("ALTER TABLE stores ADD COLUMN alert_push_enabled INTEGER NOT NULL DEFAULT 1")
                conn.commit()
            except Exception:
                conn.rollback()
    finally:
        conn.close()


def _ensure_alert_push_log():
    """确保门店预警推送去重表存在（每日每店仅推送一次）。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "CREATE TABLE IF NOT EXISTS store_alert_push_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "store_id INTEGER NOT NULL, "
            "sent_date TEXT NOT NULL, "
            "sent_at TEXT NOT NULL, "
            "UNIQUE(store_id, sent_date))"
        )


def mark_alert_pushed(store_id: int, sent_date: str):
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO store_alert_push_log (store_id, sent_date, sent_at) "
            "VALUES (?, ?, datetime('now','localtime'))",
            (store_id, sent_date),
        )


def alert_pushed_today(store_id: int, sent_date: str) -> bool:
    with _tx() as conn:
        c = conn.cursor()
        row = c.execute(
            "SELECT 1 FROM store_alert_push_log WHERE store_id = ? AND sent_date = ?",
            (store_id, sent_date),
        ).fetchone()
    return row is not None


def _migrate_pos_sources_add_request_config():
    """兼容已有数据库：pos_sources 表加 request_config（请求侧配置 JSON）。"""
    conn = get_conn()
    try:
        c = conn.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(pos_sources)").fetchall()]
        if "request_config" not in cols:
            try:
                c.execute("ALTER TABLE pos_sources ADD COLUMN request_config TEXT DEFAULT '{}'")
                conn.commit()
            except Exception:
                conn.rollback()
    finally:
        conn.close()


def _migrate_users_add_openid():
    """兼容已有数据库：users 表加 openid 列（移动端推送身份识别）。

    SQLite 不允许给已有数据的表 ADD 带 UNIQUE 的列，故此处先加普通列，
    再用「部分唯一索引」保证 openid 非空时唯一（允许空值）。
    """
    conn = get_conn()
    try:
        c = conn.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "openid" not in cols:
            try:
                c.execute("ALTER TABLE users ADD COLUMN openid TEXT DEFAULT NULL")
                conn.commit()
            except Exception:
                conn.rollback()
        # 确保部分唯一索引存在（与新建库一致）
        try:
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_openid "
                "ON users(openid) WHERE openid IS NOT NULL AND openid != ''"
            )
            conn.commit()
        except Exception:
            conn.rollback()
    finally:
        conn.close()


def _migrate_users_add_phone():
    """兼容已有数据库：users 表加 phone 列（手机号，作为小程序微信绑定的匹配键）。

    SQLite 不允许给已有数据的表 ADD 带 UNIQUE 的列，故先加普通列，
    再用「部分唯一索引」保证手机号非空时唯一（允许空值）。
    """
    conn = get_conn()
    try:
        c = conn.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()]
        if "phone" not in cols:
            try:
                c.execute("ALTER TABLE users ADD COLUMN phone TEXT DEFAULT NULL")
                conn.commit()
            except Exception:
                conn.rollback()
        try:
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_phone "
                "ON users(phone) WHERE phone IS NOT NULL AND phone != ''"
            )
            conn.commit()
        except Exception:
            conn.rollback()
    finally:
        conn.close()


def _migrate_user_stores_table():
    """兼容已有数据库：确保 user_stores 表存在，并从 users.store_id 迁移数据"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("""SELECT name FROM sqlite_master WHERE type='table' AND name='user_stores'""")
        if not c.fetchone():
            c.execute("""
                CREATE TABLE user_stores (
                    user_id INTEGER NOT NULL,
                    store_id INTEGER NOT NULL,
                    PRIMARY KEY (user_id, store_id),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (store_id) REFERENCES stores(id) ON DELETE CASCADE
                )
            """)
            # 从 users 表迁移已有数据
            c.execute("SELECT id, COALESCE(store_id, 0) FROM users WHERE store_id != 0")
            for uid, sid in c.fetchall():
                c.execute("INSERT OR IGNORE INTO user_stores (user_id, store_id) VALUES (?, ?)", (uid, sid))


def _migrate_actual_sales_unique():
    """
    兼容已有数据库：actual_sales 表加 UNIQUE(store_id, dish_id, sale_date) 约束。
    SQLite 不支持 ALTER TABLE ADD CONSTRAINT，所以用重建表方式。

    本迁移已加固为「崩溃安全」+「数据优先」：
    - CREATE TABLE 在 SQLite 中为自动提交，进程在迁移中途被杀死会残留 actual_sales_new 孤儿表；
    - init_db() 顶部的 CREATE TABLE IF NOT EXISTS 又会把被中途删除的旧表重建为空壳（且带唯一索引）；
    因此判定优先级为：
      1) 旧表含数据 -> 旧表是权威源（丢弃不完整的新表，必要时以旧表为源重建）；
      2) 旧表缺失/为空 -> 以 actual_sales_new 为权威（保留其中的数据）；
      3) 二者皆空 -> 从空旧表重建，保证最终带唯一索引。
    绝对不丢弃带数据的表。
    """
    conn = get_conn()
    try:
        c = conn.cursor()

        # 现状探测
        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='actual_sales'")
        old_exists = c.fetchone() is not None
        has_unique = False
        old_count = 0
        if old_exists:
            try:
                c.execute("PRAGMA index_list(actual_sales)")
                has_unique = any(r[2] for r in c.fetchall())  # r[2]=unique 标志
            except Exception:
                has_unique = False
            try:
                old_count = c.execute("SELECT COUNT(*) FROM actual_sales").fetchone()[0]
            except Exception:
                old_count = 0

        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='actual_sales_new'")
        new_exists = c.fetchone() is not None
        new_count = 0
        if new_exists:
            try:
                new_count = c.execute("SELECT COUNT(*) FROM actual_sales_new").fetchone()[0]
            except Exception:
                new_count = 0

        # 1) 旧表含数据 -> 旧表为权威源
        if old_exists and old_count > 0:
            if new_exists:
                c.execute("DROP TABLE IF EXISTS actual_sales_new")  # 丢弃不完整的重建副本
            if has_unique:
                conn.commit()
                return
            cols = [r[1] for r in c.execute("PRAGMA table_info(actual_sales)").fetchall()]
            if 'id' in cols:
                c.execute("""
                    CREATE TABLE actual_sales_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        store_id INTEGER NOT NULL,
                        dish_id INTEGER NOT NULL,
                        sale_date TEXT NOT NULL,
                        actual_qty REAL DEFAULT 0,
                        actual_waste REAL DEFAULT 0,
                        amount REAL DEFAULT 0,
                        notes TEXT DEFAULT '',
                        created_at TEXT DEFAULT (datetime('now', 'localtime')),
                        UNIQUE(store_id, dish_id, sale_date),
                        FOREIGN KEY (store_id) REFERENCES stores(id),
                        FOREIGN KEY (dish_id) REFERENCES dishes(id)
                    )
                """)
                c.execute("SELECT store_id, dish_id, sale_date, actual_qty, actual_waste, notes, created_at FROM actual_sales")
                old_rows = c.fetchall()
                for row in old_rows:
                    try:
                        c.execute("INSERT OR IGNORE INTO actual_sales_new (store_id, dish_id, sale_date, actual_qty, actual_waste, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", row)
                    except Exception as e:
                        logger.warning("[迁移] actual_sales 行迁移失败，已跳过: %s", e)
                c.execute("DROP TABLE actual_sales")
                c.execute("ALTER TABLE actual_sales_new RENAME TO actual_sales")
            conn.commit()
            return

        # 2) 旧表缺失/为空（被中途删除或被 init_db 重建为空壳）-> 以新表为权威
        if new_exists and new_count > 0:
            c.execute("DROP TABLE IF EXISTS actual_sales")
            c.execute("ALTER TABLE actual_sales_new RENAME TO actual_sales")
            conn.commit()
            return
        if new_exists and new_count == 0:
            c.execute("DROP TABLE IF EXISTS actual_sales_new")  # 丢弃空壳新表

        # 3) 旧表为空且无新表 -> 从空旧表重建，保证最终带唯一索引
        if old_exists:
            cols = [r[1] for r in c.execute("PRAGMA table_info(actual_sales)").fetchall()]
            if 'id' in cols and not has_unique:
                c.execute("""
                    CREATE TABLE actual_sales_new (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        store_id INTEGER NOT NULL,
                        dish_id INTEGER NOT NULL,
                        sale_date TEXT NOT NULL,
                        actual_qty REAL DEFAULT 0,
                        actual_waste REAL DEFAULT 0,
                        amount REAL DEFAULT 0,
                        notes TEXT DEFAULT '',
                        created_at TEXT DEFAULT (datetime('now', 'localtime')),
                        UNIQUE(store_id, dish_id, sale_date),
                        FOREIGN KEY (store_id) REFERENCES stores(id),
                        FOREIGN KEY (dish_id) REFERENCES dishes(id)
                    )
                """)
                c.execute("SELECT store_id, dish_id, sale_date, actual_qty, actual_waste, notes, created_at FROM actual_sales")
                old_rows = c.fetchall()
                for row in old_rows:
                    try:
                        c.execute("INSERT OR IGNORE INTO actual_sales_new (store_id, dish_id, sale_date, actual_qty, actual_waste, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", row)
                    except Exception as e:
                        logger.warning("[迁移] actual_sales 行迁移失败，已跳过: %s", e)
                c.execute("DROP TABLE actual_sales")
                c.execute("ALTER TABLE actual_sales_new RENAME TO actual_sales")
        conn.commit()
    finally:
        conn.close()


def _migrate_actual_sales_reconciled():
    """为 actual_sales 增加 reconciled 列（对账标记），默认 0（未对账）。

    POS 对接写入的销量在同步成功即视为已完成对账，落 reconciled=1；
    小程序/手工录入默认 0，待后续对账环节确认后再 feed 预测历史。
    幂等：列已存在则跳过。
    """
    _add_column_if_missing(
        "actual_sales", "reconciled", "reconciled INTEGER NOT NULL DEFAULT 0"
    )


def _migrate_users_valid_period():
    """为 users 增加账号有效期列（按合同期限管控账号可用区间），默认 NULL（无期限）。

    幂等：列已存在则跳过。
    """
    _add_column_if_missing("users", "valid_from", "valid_from TEXT DEFAULT NULL")
    _add_column_if_missing("users", "valid_until", "valid_until TEXT DEFAULT NULL")


def _migrate_users_add_authorized_by():
    """为 users 增加 authorized_by 列（记录「谁创建的这个账号」，用于渠道归属统计）。

    渠道自建的门店账号写入渠道账号 id，管理员自建写入管理员 id（或 NULL）。
    列已存在则跳过；历史库默认为 NULL（未知归属）。
    """
    _add_column_if_missing("users", "authorized_by", "authorized_by INTEGER DEFAULT NULL")


def _migrate_units_to_kg_l():
    """把 recipes / ingredients 中的 g/ml 等历史单位统一归一化为 kg/L。

    规则：数量级按 1000 缩放，单价保持 kg/L 计（与 LLM 提示词、系统 UI 一致）。
    幂等：重复执行对已是 kg/L 的数据无影响。
    """
    with _tx() as conn:
        c = conn.cursor()

        # recipes：g→kg（quantity/1000, unit='kg'），ml→L（quantity/1000, unit='L'）
        c.execute("""
            UPDATE recipes
            SET quantity = quantity / 1000.0, unit = 'kg'
            WHERE unit IN ('g', 'G', '克')
        """)
        c.execute("""
            UPDATE recipes
            SET quantity = quantity / 1000.0, unit = 'L'
            WHERE unit IN ('ml', 'mL', 'ML', '毫升')
        """)
        affected_recipes = c.rowcount

        # ingredients base_unit：g→kg，ml→L；单价不动（已按 kg/L 计）
        c.execute("UPDATE ingredients SET base_unit = 'kg' WHERE base_unit IN ('g', 'G', '克')")
        c.execute("UPDATE ingredients SET base_unit = 'L' WHERE base_unit IN ('ml', 'mL', 'ML', '毫升')")
        affected_ingredients = c.rowcount


    if affected_recipes:
        # 重算所有菜品单份成本，使 dishes.unit_cost 与新的 kg/L 配方保持一致
        recompute_all_costs()


def user_validity_status(user: dict) -> tuple:
    """判断账号是否在有效期内。

    :return: (valid: bool, reason: str)
        - valid=True  → reason=""
        - valid=False → reason="账号已过期" / "账号尚未生效"
    有效期以本地日期（YYYY-MM-DD）比较；valid_from / valid_until 为 NULL 表示不限制。
    """
    if not user:
        return False, "未登录"
    today = datetime.date.today().isoformat()
    vf = (user.get("valid_from") or "").strip()
    vu = (user.get("valid_until") or "").strip()
    if vf and today < vf:
        return False, "账号尚未生效"
    if vu and today > vu:
        return False, "账号已过期"
    return True, ""


# ==================== 用户-门店关联 CRUD ====================

def get_user_store_ids(user_id: int) -> list:
    """获取用户绑定的所有门店ID"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT store_id FROM user_stores WHERE user_id = ? ORDER BY store_id", (user_id,))
        rows = [r[0] for r in c.fetchall()]
    return rows


def get_accessible_stores(user: dict, active_only: bool = True) -> list:
    """多租户安全：返回当前用户可访问的门店列表。

    - 管理员：返回全部门店（可跨部门/租户运营）。
    - 非管理员：仅返回其绑定的门店（user_stores），杜绝越权查看其他租户数据。
    用于所有「门店下拉框」的统一构造，避免各页面遗漏 allowed_ids 过滤导致隔离泄漏。
    """
    stores = get_all_stores(active_only=active_only)
    if is_admin_role(user.get("role", "")):
        return stores
    allowed_ids = set(get_user_store_ids(user.get("id", 0)))
    return [s for s in stores if s["id"] in allowed_ids]


def set_user_store_ids(user_id: int, store_ids: list):
    """设置用户绑定的门店（全量替换）"""
    store_ids = sorted(set(int(x) for x in store_ids if int(x) != 0))
    with _tx() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM user_stores WHERE user_id = ?", (user_id,))
        for sid in store_ids:
            c.execute("INSERT OR IGNORE INTO user_stores (user_id, store_id) VALUES (?, ?)", (user_id, sid))
        # 同步主门店到 users.store_id（兼容字段）
        primary = store_ids[0] if store_ids else 0
        c.execute("UPDATE users SET store_id = ? WHERE id = ?", (primary, user_id))


def add_user_store(user_id: int, store_id: int):
    """把一个门店追加绑定到某用户（用于渠道商自建门店后自动归属）。"""
    if not user_id or not store_id:
        return
    with _tx() as conn:
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO user_stores (user_id, store_id) VALUES (?, ?)", (int(user_id), int(store_id)))
        # 同步主门店到 users.store_id（兼容字段）
        c.execute("UPDATE users SET store_id = ? WHERE id = ? AND (store_id IS NULL OR store_id = 0)",
                  (int(store_id), int(user_id)))


# ==================== 渠道供货范围 CRUD（门店需求的子集） ====================

def get_store_supply_scope(store_id: int) -> list:
    """返回该门店的渠道供货范围（食材列表）。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, store_id, ingredient_norm, ingredient_name FROM store_supply_scope "
            "WHERE store_id = ? ORDER BY ingredient_norm", (store_id,))
        rows = c.fetchall()
    return [{"id": r[0], "store_id": r[1], "ingredient_norm": r[2], "ingredient_name": r[3]} for r in rows]


def get_store_supply_scope_norms(store_id: int) -> set:
    """返回该门店供货范围的归一化食材名集合（供渠道工作台计算「已覆盖/未覆盖」）。"""
    return {s["ingredient_norm"] for s in get_store_supply_scope(store_id)}


def set_store_supply_scope(store_id: int, items: list):
    """整体替换某门店的供货范围。

    items: [(ingredient_norm, ingredient_name), ...]，按归一化名去重。
    """
    with _tx() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM store_supply_scope WHERE store_id = ?", (store_id,))
        seen = set()
        for norm, name in items:
            n = normalize_name(norm)
            if not n or n in seen:
                continue
            seen.add(n)
            c.execute(
                "INSERT OR IGNORE INTO store_supply_scope (store_id, ingredient_norm, ingredient_name) VALUES (?, ?, ?)",
                (store_id, n, str(name or norm).strip()),
            )


# ==================== 门店 CRUD（完整增删改查） ====================

def upsert_store(name: str, city: str, province: str = "", owner_email: str = "", wecom_webhook: str = "",
                 auto_predict: int = 1, auto_predict_target_offset: int = 1, auto_predict_time: str = "06:00",
                 alert_push_enabled: int = 1) -> int:
    """新增门店：同省同市同名才算重复，否则新建"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM stores WHERE name = ? AND province = ? AND city = ?", (name, province, city))
        row = c.fetchone()
        if row:
            sid = row[0]
            c.execute("UPDATE stores SET status = 'active', owner_email = ?, wecom_webhook = ?, auto_predict = ?, "
                      "auto_predict_target_offset = ?, auto_predict_time = ?, alert_push_enabled = ? WHERE id = ?",
                      (owner_email, wecom_webhook, int(auto_predict), int(auto_predict_target_offset),
                       auto_predict_time or "06:00", int(alert_push_enabled), sid))
        else:
            c.execute(
                "INSERT INTO stores (name, province, city, status, owner_email, wecom_webhook, auto_predict, "
                "auto_predict_target_offset, auto_predict_time, alert_push_enabled) VALUES (?, ?, ?, 'active', ?, ?, ?, ?, ?, ?)",
                (name, province, city, owner_email, wecom_webhook, int(auto_predict),
                 int(auto_predict_target_offset), auto_predict_time or "06:00", int(alert_push_enabled)),
            )
            sid = c.lastrowid
    return sid


def get_all_stores(active_only: bool = True):
    with _tx() as conn:
        c = conn.cursor()
        cols = "id, name, province, city, status, owner_email, wecom_webhook, auto_predict, " \
               "auto_predict_target_offset, auto_predict_time, last_auto_predict_at, last_auto_predict_status, alert_push_enabled, created_at"
        if active_only:
            c.execute(f"SELECT {cols} FROM stores WHERE status = 'active' ORDER BY id")
        else:
            c.execute(f"SELECT {cols} FROM stores ORDER BY id")
        rows = c.fetchall()
    return [{"id": r[0], "name": r[1], "province": r[2], "city": r[3], "status": r[4],
             "owner_email": r[5], "wecom_webhook": r[6], "auto_predict": r[7],
             "auto_predict_target_offset": r[8], "auto_predict_time": r[9],
             "last_auto_predict_at": r[10], "last_auto_predict_status": r[11], "alert_push_enabled": r[12], "created_at": r[13]} for r in rows]


def get_store_by_id(store_id: int):
    if not store_id:
        return None
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, name, province, city, status, owner_email, wecom_webhook, auto_predict, "
            "auto_predict_target_offset, auto_predict_time, last_auto_predict_at, last_auto_predict_status, alert_push_enabled, created_at "
            "FROM stores WHERE id = ?",
            (store_id,),
        )
        row = c.fetchone()
    if row:
        return {"id": row[0], "name": row[1], "province": row[2], "city": row[3], "status": row[4],
                "owner_email": row[5], "wecom_webhook": row[6], "auto_predict": row[7],
                "auto_predict_target_offset": row[8], "auto_predict_time": row[9],
                "last_auto_predict_at": row[10], "last_auto_predict_status": row[11],
                "alert_push_enabled": row[12], "created_at": row[13]}
    return None


def get_prediction_stats_by_stores(store_ids: list) -> dict:
    """批量统计门店的预测活跃度（渠道视角用）。

    返回 {store_id: {"last_run": str|None, "last_date": str|None, "count_7d": int}}
    - last_run: 最近一次预测运行时间
    - last_date: 最近一次预测的目标日期
    - count_7d: 近 7 天（含今天）生成的预测次数，用于判断门店是否活跃
    无预测记录的门店不会出现在结果中（调用方用 .get(sid, 默认) 处理）。
    """
    ids = [int(x) for x in (store_ids or []) if int(x)]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            f"SELECT store_id, MAX(run_time), MAX(predict_date), "
            f"SUM(CASE WHEN date(run_time) >= date('now','-7 days') THEN 1 ELSE 0 END) "
            f"FROM predictions WHERE store_id IN ({placeholders}) GROUP BY store_id",
            ids,
        )
        rows = c.fetchall()
    return {
        int(r[0]): {"last_run": r[1], "last_date": r[2], "count_7d": int(r[3] or 0)}
        for r in rows
    }


def get_store_push_targets(store_id: int) -> dict:
    """汇集门店采购单推送的目标：老板邮箱、企微 webhook、已绑定微信的 openid 列表。

    返回 {"emails": [...], "openids": [...], "wecom_webhook": "..."}
    - emails: 门店 owner_email（若填写）
    - openids: 绑定到该门店、且已绑 openid 的用户（移动端订阅消息推送对象）
    - wecom_webhook: 门店配置的企微群机器人地址
    """
    store = get_store_by_id(store_id) or {}
    emails = [e.strip() for e in (store.get("owner_email") or "").split(",") if e.strip()]
    wecom = (store.get("wecom_webhook") or "").strip()

    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT u.openid FROM users u JOIN user_stores us ON us.user_id = u.id "
            "WHERE us.store_id = ? AND u.openid IS NOT NULL AND u.openid != ''",
            (store_id,),
        )
        openids = [r[0] for r in c.fetchall()]
    return {"emails": emails, "openids": openids, "wecom_webhook": wecom}


def update_store(store_id: int, name: str, city: str, province: str, owner_email: str = None,
                 wecom_webhook: str = None, auto_predict: int = None,
                 auto_predict_target_offset: int = None, auto_predict_time: str = None,
                 alert_push_enabled: int = None):
    """修改门店信息：同区域不能有其他同名门店"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id FROM stores WHERE name = ? AND province = ? AND city = ? AND id != ?",
            (name, province, city, store_id)
        )
        row = c.fetchone()
        if row:
            raise ValueError(f"该区域已存在门店：【{province} {city} {name}】")
        c.execute("UPDATE stores SET name = ?, city = ?, province = ? WHERE id = ?", (name, city, province, store_id))
        if owner_email is not None:
            c.execute("UPDATE stores SET owner_email = ? WHERE id = ?", (owner_email, store_id))
        if wecom_webhook is not None:
            c.execute("UPDATE stores SET wecom_webhook = ? WHERE id = ?", (wecom_webhook, store_id))
        if auto_predict is not None:
            c.execute("UPDATE stores SET auto_predict = ? WHERE id = ?", (int(auto_predict), store_id))
        if auto_predict_target_offset is not None:
            c.execute("UPDATE stores SET auto_predict_target_offset = ? WHERE id = ?",
                      (int(auto_predict_target_offset), store_id))
        if auto_predict_time is not None:
            c.execute("UPDATE stores SET auto_predict_time = ? WHERE id = ?", (auto_predict_time or "06:00", store_id))
        if alert_push_enabled is not None:
            c.execute("UPDATE stores SET alert_push_enabled = ? WHERE id = ?", (int(alert_push_enabled), store_id))


def get_auto_predict_stores():
    """返回参与自动预测的门店（活跃 + auto_predict=1）。返回 dict 列表，含 id/name/city/target/time。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, name, city, auto_predict_target_offset, auto_predict_time, last_auto_predict_at "
            "FROM stores WHERE status = 'active' AND auto_predict = 1 ORDER BY id"
        )
        rows = c.fetchall()
    return [{"id": r[0], "name": r[1], "city": r[2],
             "auto_predict_target_offset": r[3], "auto_predict_time": r[4],
             "last_auto_predict_at": r[5]} for r in rows]


def get_alert_push_stores():
    """返回开启预警自动推送的门店（活跃 + alert_push_enabled=1）。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, name, city FROM stores WHERE status = 'active' AND alert_push_enabled = 1 ORDER BY id"
        )
        rows = c.fetchall()
    return [{"id": r[0], "name": r[1], "city": r[2]} for r in rows]


def set_store_auto_predict(store_id: int, auto_predict: int):
    """设置门店是否参与自动预测（1=参与，0=仅手工）。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("UPDATE stores SET auto_predict = ? WHERE id = ?", (int(auto_predict), store_id))


def update_store_predict_status(store_id: int, at: str, status: str):
    """更新门店上次自动预测时间与状态（ok/failed/skipped）。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("UPDATE stores SET last_auto_predict_at = ?, last_auto_predict_status = ? WHERE id = ?",
                  (at, status, store_id))


def parse_pos_schedule(schedule: str):
    """解析 POS 同步计划为 (hour, minute)。

    支持 '23:30' / '每天 23:30' / 'daily 23:30' / '23:30:00' / '23：30'（全角冒号）。
    返回 (h, m) 元组；空或非法返回 None（调用方回落到全局默认时间）。
    """
    if not schedule or not str(schedule).strip():
        return None
    m = re.search(r'(\d{1,2})\s*[:：]\s*(\d{1,2})', str(schedule))
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return (h, mi)
    return None


def parse_auto_predict_time(time_str: str):
    """解析自动预测时间为 (hour, minute)。

    支持 '06:00' / '6:00' / '6：00'（全角冒号）等。空或非法返回 (6, 0)。
    """
    if not time_str or not str(time_str).strip():
        return (6, 0)
    m = re.search(r'(\d{1,2})\s*[:：]\s*(\d{1,2})', str(time_str))
    if not m:
        return (6, 0)
    h, mi = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return (h, mi)
    return (6, 0)


# 删除门店时需级联清理的子表（按外键依赖，叶 → 根排列）。
# 说明：除 user_stores / pos_source_logs 外，其余子表外键均【无】ON DELETE CASCADE，
# 若不显式清理，在 PRAGMA foreign_keys=ON 下 DELETE stores 会直接报
# "FOREIGN KEY constraint failed"（即门店删不掉）。
_STORE_CHILD_TABLES = (
    "actual_sales", "sales_history", "predictions", "date_factors",
    "backtest_runs", "bias_corrections", "store_supply_scope",
    "pos_sync_log", "pos_sources", "store_alert_push_log",
    "dishes", "ingredients", "user_stores",
)


def get_store_delete_impact(store_id: int) -> dict:
    """统计删除某门店将级联清理的数据量（供页面二次确认展示）。"""
    with _tx() as conn:
        c = conn.cursor()
        impact = {}
        for t in _STORE_CHILD_TABLES:
            impact[t] = c.execute(f"SELECT COUNT(*) FROM {t} WHERE store_id = ?", (store_id,)).fetchone()[0]
        impact["recipes"] = c.execute(
            "SELECT COUNT(*) FROM recipes WHERE dish_id IN (SELECT id FROM dishes WHERE store_id = ?) "
            "OR ingredient_id IN (SELECT id FROM ingredients WHERE store_id = ?)",
            (store_id, store_id),
        ).fetchone()[0]
    return impact


def delete_store(store_id: int):
    """删除门店，并级联清理其全部子数据（菜品/食材/配方/预测/销量/POS/预警等）。

    注意：除 user_stores / pos_source_logs 外，子表外键无 ON DELETE CASCADE，
    必须显式按依赖顺序删除，否则会被外键约束拒绝。
    """
    with _tx() as conn:
        c = conn.cursor()
        # 1) recipes 无 store_id，先按本店菜品/食材清掉（最底层，避免 FK 拦截）
        c.execute(
            "DELETE FROM recipes WHERE dish_id IN (SELECT id FROM dishes WHERE store_id = ?) "
            "OR ingredient_id IN (SELECT id FROM ingredients WHERE store_id = ?)",
            (store_id, store_id),
        )
        # 2) 其余子表按 store_id 直接清理（元组已按叶→根排序：dishes 在 sales_history/actual_sales 之后）
        for t in _STORE_CHILD_TABLES:
            c.execute(f"DELETE FROM {t} WHERE store_id = ?", (store_id,))
        # 3) users.store_id 是"主门店"兼容列：置 0 而非删账号
        c.execute("UPDATE users SET store_id = 0 WHERE store_id = ?", (store_id,))
        # 4) 最后删门店本身
        c.execute("DELETE FROM stores WHERE id = ?", (store_id,))


def update_store_status(store_id: int, status: str):
    with _tx() as conn:
        c = conn.cursor()
        c.execute("UPDATE stores SET status = ? WHERE id = ?", (status, store_id))


# ==================== 菜品 CRUD（完整增删改查） ====================

def upsert_dish(store_id: int, name: str, category: str = "", standard_price: float = 0) -> int:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM dishes WHERE store_id = ? AND name = ?", (store_id, name))
        row = c.fetchone()
        if row:
            did = row[0]
            c.execute("UPDATE dishes SET category = ?, standard_price = ?, status = 'active' WHERE id = ?", (category, standard_price, did))
        else:
            c.execute("INSERT INTO dishes (store_id, name, category, standard_price, status) VALUES (?, ?, ?, ?, 'active')", (store_id, name, category, standard_price))
            did = c.lastrowid
    return did


def upsert_dish_ex(store_id: int, name: str, category: str = "", standard_price: float = 0) -> tuple:
    """
    按 (门店, 归一化名) 去重入库。

    返回 (dish_id, is_new)：
    - is_new=True  → 新入库，需触发大模型生成配方
    - is_new=False → 已存在，仅更新单价与启用状态，不动配方
    """
    norm = normalize_name(name)
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM dishes WHERE store_id = ? AND norm_name = ?", (store_id, norm))
        row = c.fetchone()
        if row:
            did = row[0]
            c.execute("UPDATE dishes SET standard_price = ?, status = 'active' WHERE id = ?", (float(standard_price or 0), did))
            return did, False
        c.execute(
            "INSERT INTO dishes (store_id, name, norm_name, category, standard_price, status, bom_status) "
            "VALUES (?, ?, ?, ?, ?, 'active', 'pending')",
            (store_id, str(name).strip(), norm, category or "", float(standard_price or 0)),
        )
        return c.lastrowid, True


def get_dishes_by_store(store_id: int, active_only: bool = True):
    with _tx() as conn:
        c = conn.cursor()
        if active_only:
            c.execute("SELECT id, name, category, standard_price, status, unit_cost, bom_status FROM dishes WHERE store_id = ? AND status = 'active' ORDER BY id", (store_id,))
        else:
            c.execute("SELECT id, name, category, standard_price, status, unit_cost, bom_status FROM dishes WHERE store_id = ? ORDER BY id", (store_id,))
        rows = c.fetchall()
    return [{"id": r[0], "name": r[1], "category": r[2], "standard_price": r[3], "status": r[4],
             "unit_cost": r[5] or 0, "bom_status": r[6] or "pending"} for r in rows]


def get_dish_by_id(dish_id: int, conn=None):
    own = conn is None
    if own:
        conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT id, store_id, name, category, standard_price, status FROM dishes WHERE id = ?", (dish_id,))
        row = c.fetchone()
    finally:
        if own:
            conn.close()
    if row:
        return {"id": row[0], "store_id": row[1], "name": row[2], "category": row[3], "standard_price": row[4], "status": row[5]}
    return None


def update_dish(dish_id: int, name: str, category: str = "", standard_price: float = 0):
    """修改菜品"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("UPDATE dishes SET name = ?, category = ?, standard_price = ? WHERE id = ?", (name, category, standard_price, dish_id))


def delete_dish(dish_id: int):
    """删除菜品，并级联清理其配方 / 实际销量 / 历史销量。

    说明：actual_sales / sales_history 均有 dish_id 外键且无 ON DELETE CASCADE，
    不显式清理会在 PRAGMA foreign_keys=ON 下被拒绝。
    """
    with _tx() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM recipes WHERE dish_id = ?", (dish_id,))
        c.execute("DELETE FROM actual_sales WHERE dish_id = ?", (dish_id,))
        c.execute("DELETE FROM sales_history WHERE dish_id = ?", (dish_id,))
        c.execute("DELETE FROM dishes WHERE id = ?", (dish_id,))


def update_dish_status(dish_id: int, status: str):
    with _tx() as conn:
        c = conn.cursor()
        c.execute("UPDATE dishes SET status = ? WHERE id = ?", (status, dish_id))


# ==================== 配方 CRUD ====================

def replace_recipe_for_dish(dish_id: int, items: list, source: str = "import", conn=None):
    """
    整体替换某菜品的配方（先删后插）。

    items: [{ingredient_name, quantity, unit?, unit_price?, category?}]
    每个食材会同步归位到 ingredients 主数据（默认不覆盖已有单价）。

    conn: 可选共享写连接。传入时本函数复用该连接且不关闭，便于批量落库时
          整条写链路使用单一连接，避免并发锁（database is locked）。
    """
    own = conn is None
    if own:
        conn = get_conn()
    try:
        dish = get_dish_by_id(dish_id, conn=conn)
        if not dish:
            raise ValueError("菜品不存在")

        c = conn.cursor()
        c.execute("DELETE FROM recipes WHERE dish_id = ?", (dish_id,))
        for item in items:
            raw_name = str(item.get("ingredient_name", "")).strip()
            if not raw_name:
                continue
            # 统一归一化为 kg / L；计数单位（个/只/片/份）按默认 0.05 kg 换算
            norm = canonicalize_unit(
                float(item.get("quantity", 0) or 0),
                item.get("unit", "") or "",
                fallback_count_weight_kg=0.05,
            )
            ing_id = upsert_ingredient(
                store_id=dish["store_id"], name=raw_name, category=item.get("category", ""),
                base_unit=norm["unit"] or "kg", unit_price=item.get("unit_price", 0),
                source=source, overwrite_price=False, conn=conn,
            )
            c.execute(
                "INSERT INTO recipes (dish_id, ingredient_name, quantity, unit, unit_price, category, ingredient_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (dish_id, raw_name, norm["quantity"],
                 norm["unit"] or "kg", float(item.get("unit_price", 0) or 0),
                 item.get("category", "") or "", ing_id)
            )
        conn.commit()
        c.execute("UPDATE dishes SET bom_status = 'generated', bom_source = ?, "
                  "bom_generated_at = datetime('now','localtime') WHERE id = ?", (source, dish_id))
        conn.commit()
        recompute_dish_cost(dish_id, conn=conn)
    finally:
        if own:
            conn.close()


def mark_bom_status(dish_id: int, status: str, source: str = None):
    """标记菜品配方生成状态：pending / generated / manual"""
    with _tx() as conn:
        c = conn.cursor()
        if source:
            c.execute("UPDATE dishes SET bom_status = ?, bom_source = ?, "
                      "bom_generated_at = datetime('now','localtime') WHERE id = ?", (status, source, dish_id))
        else:
            c.execute("UPDATE dishes SET bom_status = ? WHERE id = ?", (status, dish_id))


def get_dishes_pending_bom(store_id: int = None, limit: int = None) -> list:
    """取待生成配方的菜品（bom_status='pending' 且当前无配方行）。"""
    with _tx() as conn:
        c = conn.cursor()
        sql = """
            SELECT d.id, d.store_id, d.name, d.category, d.standard_price
            FROM dishes d
            WHERE d.status = 'active' AND d.bom_status = 'pending'
              AND NOT EXISTS (SELECT 1 FROM recipes r WHERE r.dish_id = d.id)
        """
        params = []
        if store_id:
            sql += " AND d.store_id = ?"
            params.append(store_id)
        sql += " ORDER BY d.store_id, d.id"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        c.execute(sql, params)
        rows = c.fetchall()
    return [{"id": r[0], "store_id": r[1], "name": r[2], "category": r[3], "standard_price": r[4]} for r in rows]


def get_dishes_need_regen(store_id: int = None, limit: int = None) -> list:
    """
    取「需要重新生成配方」的菜品：bom_status='pending' 的活跃菜品。

    存量迁移后所有菜品均为 pending，因此这里会返回全部存量菜品——
    用于一次性全量重刷（用户决策：存量配方可能有误，全部重新生成）。
    """
    with _tx() as conn:
        c = conn.cursor()
        sql = """
            SELECT d.id, d.store_id, d.name, d.category, d.standard_price
            FROM dishes d
            WHERE d.status = 'active' AND d.bom_status = 'pending'
        """
        params = []
        if store_id:
            sql += " AND d.store_id = ?"
            params.append(store_id)
        sql += " ORDER BY d.store_id, d.id"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        c.execute(sql, params)
        rows = c.fetchall()
    return [{"id": r[0], "store_id": r[1], "name": r[2], "category": r[3], "standard_price": r[4]} for r in rows]


def get_active_recipes_by_store(store_id: int) -> dict:
    """
    返回 {菜名: [配方行...]}，用于食材聚合。

    权威单价优先取食材主数据 ingredients.unit_price，
    仅在未归位到主数据时才回退 recipes.unit_price。
    """
    with _tx() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT d.name, r.ingredient_name, r.quantity, r.unit, r.unit_price, r.category,
                   r.ingredient_id, i.unit_price, i.base_unit, i.category
            FROM recipes r
            JOIN dishes d ON r.dish_id = d.id
            LEFT JOIN ingredients i ON r.ingredient_id = i.id
            WHERE d.store_id = ? AND d.status = 'active'
        """, (store_id,))
        rows = c.fetchall()
    result = {}
    for r in rows:
        dish_name = r[0]
        if dish_name not in result:
            result[dish_name] = []
        eff_price = r[7] if r[7] is not None else (r[4] or 0)
        result[dish_name].append({
            "ingredient_name": r[1],
            "quantity": r[2],
            "unit": r[3] or (r[8] or ""),
            "unit_price": float(eff_price or 0),
            "category": r[5] or (r[9] or ""),
            "ingredient_id": r[6],
        })
    return result


# ==================== 食材主数据 CRUD（按门店独立） ====================

def upsert_ingredient(
    store_id: int,
    name: str,
    category: str = "",
    base_unit: str = "kg",
    unit_price: float = 0,
    source: str = "llm",
    overwrite_price: bool = False,
    conn: object = None,
) -> int:
    """
    新增或获取食材（按 门店+归一化名 去重）。

    overwrite_price=False（默认）时，**绝不覆盖已有单价**——
    老板在「食材管理」改过的价，优先级高于大模型估价与导入值。
    """
    norm = normalize_name(name)
    if not norm:
        raise ValueError("食材名称不能为空")

    own_conn = conn is None
    if own_conn:
        conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT id, unit_price, source FROM ingredients WHERE store_id = ? AND name = ?", (store_id, norm))
        row = c.fetchone()
        if row:
            ing_id, exist_price, exist_source = row[0], row[1], (row[2] or "")
            # 价格覆盖策略：
            #  - overwrite_price=True        → 无条件覆盖（页面显式改价）
            #  - 现有为 manual（老板手改/手建）→ 绝不覆盖，人工价优先级最高
            #  - 新值为 llm 且现有为 import/llm → 覆盖（存量导入值可能有误，用新估价刷新）
            if not overwrite_price:
                overwrite_price = (source == "llm" and exist_source in ("import", "llm"))
            # 若存量食材 base_unit 仍停留在 g/ml，同步归一化为 kg/L（单价保持 kg/L 计，不动）
            norm_unit = canonicalize_unit(1.0, base_unit, fallback_count_weight_kg=0.05)["unit"] or "kg"
            if overwrite_price:
                c.execute(
                    "UPDATE ingredients SET unit_price = ?, base_unit = ?, price_updated_at = datetime('now','localtime'), "
                    "source = ?, status = 'active' WHERE id = ?",
                    (float(unit_price or 0), norm_unit, source, ing_id),
                )
            else:
                c.execute(
                    "UPDATE ingredients SET base_unit = ?, status = 'active' WHERE id = ?",
                    (norm_unit, ing_id),
                )
            conn.commit()
            return ing_id

        norm_unit = canonicalize_unit(1.0, base_unit, fallback_count_weight_kg=0.05)["unit"]
        c.execute(
            "INSERT INTO ingredients (store_id, name, display_name, category, base_unit, unit_price, "
            " price_updated_at, source, status) VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'), ?, 'active')",
            (store_id, norm, str(name).strip(), category or "", norm_unit or "kg", float(unit_price or 0), source),
        )
        ing_id = c.lastrowid
        conn.commit()
        return ing_id
    finally:
        if own_conn:
            conn.close()


def get_ingredients_by_store(store_id: int, active_only: bool = True):
    with _tx() as conn:
        c = conn.cursor()
        if active_only:
            c.execute(
                "SELECT id, store_id, name, display_name, category, base_unit, unit_price, "
                "price_updated_at, source, status, created_at, updated_at "
                "FROM ingredients WHERE store_id = ? AND status = 'active' ORDER BY id",
                (store_id,),
            )
        else:
            c.execute(
                "SELECT id, store_id, name, display_name, category, base_unit, unit_price, "
                "price_updated_at, source, status, created_at, updated_at "
                "FROM ingredients WHERE store_id = ? ORDER BY id",
                (store_id,),
            )
        rows = c.fetchall()
    keys = ["id", "store_id", "name", "display_name", "category", "base_unit", "unit_price",
            "price_updated_at", "source", "status", "created_at", "updated_at"]
    return [dict(zip(keys, r)) for r in rows]


def get_ingredient_by_id(ing_id: int):
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, store_id, name, display_name, category, base_unit, unit_price, "
            "price_updated_at, source, status, created_at, updated_at FROM ingredients WHERE id = ?",
            (ing_id,),
        )
        row = c.fetchone()
    if not row:
        return None
    keys = ["id", "store_id", "name", "display_name", "category", "base_unit", "unit_price",
            "price_updated_at", "source", "status", "created_at", "updated_at"]
    return dict(zip(keys, row))


def add_ingredient(store_id: int, name: str, category: str = "", base_unit: str = "kg",
                   unit_price: float = 0, source: str = "manual") -> int:
    """显式新增食材（重名时抛错，由页面层提示）。"""
    norm = normalize_name(name)
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT id FROM ingredients WHERE store_id = ? AND name = ?", (store_id, norm))
        if c.fetchone():
            raise ValueError(f"食材「{name}」已存在")
        norm_unit = canonicalize_unit(1.0, base_unit, fallback_count_weight_kg=0.05)["unit"]
        c.execute(
            "INSERT INTO ingredients (store_id, name, display_name, category, base_unit, unit_price, "
            " price_updated_at, source, status) VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'), ?, 'active')",
            (store_id, norm, str(name).strip(), category or "", norm_unit or "kg", float(unit_price or 0), source),
        )
        iid = c.lastrowid
        conn.commit()
        return iid
    finally:
        conn.close()


def update_ingredient(ing_id: int, name: str = None, category: str = None,
                      base_unit: str = None, unit_price: float = None):
    """修改食材。改价后需调用 recompute_costs_for_ingredient 联动重算菜品成本。"""
    sets, vals = [], []
    if name is not None:
        sets.append("name = ?")
        vals.append(normalize_name(name))
        sets.append("display_name = ?")
        vals.append(str(name).strip())
    if category is not None:
        sets.append("category = ?")
        vals.append(category)
    if base_unit is not None:
        sets.append("base_unit = ?")
        vals.append(base_unit)
    if unit_price is not None:
        sets.append("unit_price = ?")
        vals.append(float(unit_price))
        sets.append("price_updated_at = datetime('now','localtime')")
    if not sets:
        return
    sets.append("updated_at = datetime('now','localtime')")
    vals.append(ing_id)
    with _tx() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE ingredients SET {', '.join(sets)} WHERE id = ?", vals)


def update_ingredient_price(ing_id: int, unit_price: float):
    update_ingredient(ing_id, unit_price=float(unit_price))


def delete_ingredient(ing_id: int):
    """删除食材，并同步摘除所有配方中对它的引用（配方行一并删除）。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM recipes WHERE ingredient_id = ?", (ing_id,))
        c.execute("DELETE FROM ingredients WHERE id = ?", (ing_id,))


def update_ingredient_status(ing_id: int, status: str):
    with _tx() as conn:
        c = conn.cursor()
        c.execute("UPDATE ingredients SET status = ?, updated_at = datetime('now','localtime') WHERE id = ?",
                  (status, ing_id))


def get_ingredient_usage(ing_id: int) -> list:
    """返回引用该食材的菜品列表（用于食材管理页展示影响面）。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT d.id, d.name, r.quantity, r.unit
            FROM recipes r JOIN dishes d ON r.dish_id = d.id
            WHERE r.ingredient_id = ? ORDER BY d.name
        """, (ing_id,))
        rows = c.fetchall()
    return [{"dish_id": r[0], "dish_name": r[1], "quantity": r[2], "unit": r[3]} for r in rows]


def get_ingredient_usage_map(store_id: int) -> dict:
    """一次查询返回某门店【所有】食材的引用明细，供列表页批量展示。

    返回 {ingredient_id: [{dish_id, dish_name, quantity, unit}, ...]}。
    用途：替代「列表里逐个食材调用 get_ingredient_usage」的 N+1 查询。
    """
    with _tx() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT r.ingredient_id, d.id, d.name, r.quantity, r.unit
            FROM recipes r JOIN dishes d ON r.dish_id = d.id
            WHERE d.store_id = ? AND r.ingredient_id IS NOT NULL
            ORDER BY d.name
        """, (store_id,))
        rows = c.fetchall()
    out: dict = {}
    for r in rows:
        out.setdefault(r[0], []).append(
            {"dish_id": r[1], "dish_name": r[2], "quantity": r[3], "unit": r[4]}
        )
    return out


def get_distinct_ingredient_names() -> list:
    """返回全库去重后的食材名列表（用于渠道供货范围配置的多选）。

    每条为 {"norm": 归一化名, "name": 展示名}，按归一化名去重，展示名取首个出现的原始名。
    """
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT name, display_name FROM ingredients WHERE status = 'active' ORDER BY id")
        rows = c.fetchall()
    seen = {}
    out = []
    for norm, disp in rows:
        n = normalize_name(norm)
        if not n or n in seen:
            continue
        seen[n] = True
        out.append({"norm": n, "name": (disp or norm or "").strip() or n})
    out.sort(key=lambda x: x["name"])
    return out


# ==================== 配方（BOM）明细 CRUD ====================

def get_recipes_by_dish(dish_id: int, conn=None) -> list:
    """返回菜品配方明细，含权威单价（优先取食材主数据）。"""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT r.id, r.dish_id, r.ingredient_name, r.quantity, r.unit, r.unit_price,
                   r.category, r.ingredient_id, i.unit_price, i.base_unit, i.category
            FROM recipes r
            LEFT JOIN ingredients i ON r.ingredient_id = i.id
            WHERE r.dish_id = ? ORDER BY r.id
        """, (dish_id,))
        rows = c.fetchall()
    finally:
        if own:
            conn.close()
    out = []
    for r in rows:
        eff_price = r[8] if r[8] is not None else (r[5] or 0)
        out.append({
            "id": r[0], "dish_id": r[1], "ingredient_name": r[2], "quantity": r[3],
            "unit": r[4] or (r[9] or ""), "unit_price": r[5] or 0, "category": r[6] or (r[10] or ""),
            "ingredient_id": r[7], "effective_price": float(eff_price or 0),
            "amount": round(float(r[3] or 0) * float(eff_price or 0), 4),
        })
    return out


def get_recipes_map_by_store(store_id: int) -> dict:
    """一次查询返回某门店【所有】菜品的配方明细：{dish_id: [recipe, ...]}。

    字段与 get_recipes_by_dish 完全一致（含权威单价 effective_price / amount）。
    用途：菜品管理页 N+1 优化（原实现每道菜查两次、整页 O(菜品数) 次查询）。
    """
    with _tx() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT r.id, r.dish_id, r.ingredient_name, r.quantity, r.unit, r.unit_price,
                   r.category, r.ingredient_id, i.unit_price, i.base_unit, i.category
            FROM recipes r
            JOIN dishes d ON r.dish_id = d.id
            LEFT JOIN ingredients i ON r.ingredient_id = i.id
            WHERE d.store_id = ? ORDER BY r.dish_id, r.id
        """, (store_id,))
        rows = c.fetchall()
    out: dict = {}
    for r in rows:
        eff_price = r[8] if r[8] is not None else (r[5] or 0)
        out.setdefault(r[1], []).append({
            "id": r[0], "dish_id": r[1], "ingredient_name": r[2], "quantity": r[3],
            "unit": r[4] or (r[9] or ""), "unit_price": r[5] or 0, "category": r[6] or (r[10] or ""),
            "ingredient_id": r[7], "effective_price": float(eff_price or 0),
            "amount": round(float(r[3] or 0) * float(eff_price or 0), 4),
        })
    return out


def add_recipe_item(dish_id: int, ingredient_name: str, quantity: float, unit: str = "kg",
                    unit_price: float = 0, category: str = "") -> int:
    """为菜品新增一条食材用量。同步归位到食材主数据（不覆盖已有价）。"""
    dish = get_dish_by_id(dish_id)
    if not dish:
        raise ValueError("菜品不存在")
    norm = canonicalize_unit(float(quantity), unit, fallback_count_weight_kg=0.05)
    ing_id = upsert_ingredient(
        store_id=dish["store_id"], name=ingredient_name, category=category,
        base_unit=norm["unit"], unit_price=unit_price, source="manual", overwrite_price=False,
    )
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO recipes (dish_id, ingredient_name, quantity, unit, unit_price, category, ingredient_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (dish_id, str(ingredient_name).strip(), norm["quantity"], norm["unit"], float(unit_price or 0), category or "", ing_id),
        )
        rid = c.lastrowid
    recompute_dish_cost(dish_id)
    return rid


def update_recipe_item(recipe_id: int, ingredient_name: str = None, quantity: float = None,
                       unit: str = None, category: str = None):
    """修改配方明细行（用量/单位/品类/食材名）。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT dish_id FROM recipes WHERE id = ?", (recipe_id,))
        row = c.fetchone()
        if not row:
            return
        dish_id = row[0]

        sets, vals = [], []
        # 若同时改用量/单位，先归一化
        if quantity is not None and unit is not None:
            norm = canonicalize_unit(float(quantity), unit, fallback_count_weight_kg=0.05)
            quantity, unit = norm["quantity"], norm["unit"]
        elif quantity is not None:
            # 仅改用量：按当前单位归一化一次（防御旧存量 g 被重新保存）
            c.execute("SELECT unit FROM recipes WHERE id = ?", (recipe_id,))
            cur_unit = (c.fetchone() or [None])[0]
            norm = canonicalize_unit(float(quantity), cur_unit or "kg", fallback_count_weight_kg=0.05)
            quantity = norm["quantity"]
            unit = norm["unit"] if norm["changed"] else cur_unit
        elif unit is not None:
            # 仅改单位：按原数量重新换算（g→kg 则 quantity/1000）
            c.execute("SELECT quantity FROM recipes WHERE id = ?", (recipe_id,))
            cur_qty = (c.fetchone() or [0])[0]
            norm = canonicalize_unit(float(cur_qty), unit, fallback_count_weight_kg=0.05)
            quantity, unit = norm["quantity"], norm["unit"]

        if ingredient_name is not None:
            dish = get_dish_by_id(dish_id)
            ing_id = upsert_ingredient(
                store_id=dish["store_id"], name=ingredient_name, category=category or "",
                base_unit=unit or "kg", source="manual", overwrite_price=False,
            )
            sets += ["ingredient_name = ?", "ingredient_id = ?"]
            vals += [str(ingredient_name).strip(), ing_id]
        if quantity is not None:
            sets.append("quantity = ?")
            vals.append(float(quantity))
        if unit is not None:
            sets.append("unit = ?")
            vals.append(unit)
        if category is not None:
            sets.append("category = ?")
            vals.append(category)
        if not sets:
            return
        vals.append(recipe_id)
        c.execute(f"UPDATE recipes SET {', '.join(sets)} WHERE id = ?", vals)
    recompute_dish_cost(dish_id)


def delete_recipe_item(recipe_id: int):
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT dish_id FROM recipes WHERE id = ?", (recipe_id,))
        row = c.fetchone()
        c.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    if row:
        recompute_dish_cost(row[0])


def recompute_dish_cost(dish_id: int, conn=None) -> float:
    """重算单个菜品的单份食材成本 = Σ(用量 × 权威单价)，并回写 dishes.unit_cost。"""
    items = get_recipes_by_dish(dish_id, conn=conn)
    cost = round(sum(float(i["amount"] or 0) for i in items), 4)
    own = conn is None
    if own:
        conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("UPDATE dishes SET unit_cost = ? WHERE id = ?", (cost, dish_id))
        conn.commit()
    finally:
        if own:
            conn.close()
    return cost


def recompute_costs_for_ingredient(ing_id: int) -> int:
    """食材改价后，重算所有引用它的菜品成本，返回受影响菜品数。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT DISTINCT dish_id FROM recipes WHERE ingredient_id = ?", (ing_id,))
        dish_ids = [r[0] for r in c.fetchall()]
    for did in dish_ids:
        recompute_dish_cost(did)
    return len(dish_ids)


def recompute_all_costs(store_id: int = None, conn=None) -> int:
    """全量重算菜品成本（store_id 为空则全库）。"""
    own = conn is None
    if own:
        conn = get_conn()
    try:
        c = conn.cursor()
        if store_id:
            c.execute("SELECT id FROM dishes WHERE store_id = ?", (store_id,))
        else:
            c.execute("SELECT id FROM dishes")
        dish_ids = [r[0] for r in c.fetchall()]
    finally:
        if own:
            conn.close()
    for did in dish_ids:
        recompute_dish_cost(did)
    return len(dish_ids)


# ==================== 预测结果 CRUD ====================

def save_prediction(record: dict):
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO predictions (store_id, predict_date, run_time, special_event, revenue, cost, gross_margin, result_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.get("store_id", 0),
                record.get("predict_date", ""),
                record.get("run_time", __import__('datetime').datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                record.get("special_event", ""),
                record.get("revenue", 0),
                record.get("cost", 0),
                record.get("gross_margin", 0),
                json.dumps(record.get("result", {}), ensure_ascii=False),
            ),
        )


def load_all_history(store_ids: list = None, days: int = None, limit: int = None) -> list:
    """读取预测历史（倒序）。

    [F4 性能加固] 支持在 DB 层按门店、时间窗、条数上限过滤，避免全表加载后在
    Python 层过滤。默认（无参）行为保持兼容：返回全部记录。
    """
    with _tx() as conn:
        c = conn.cursor()
        sql = ("SELECT store_id, predict_date, run_time, special_event, revenue, cost, "
               "gross_margin, result_json FROM predictions")
        conds, params = [], []
        if store_ids:
            placeholders = ",".join("?" * len(store_ids))
            conds.append(f"store_id IN ({placeholders})")
            params.extend(store_ids)
        if days and days > 0:
            conds.append("predict_date >= date('now', ?)")
            params.append(f"-{int(days)} days")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY id DESC"
        if limit and limit > 0:
            sql += f" LIMIT {int(limit)}"
        c.execute(sql, params)
        rows = c.fetchall()
    result = []
    for r in rows:
        result.append({
            "store_id": r[0],
            "predict_date": r[1],
            "run_time": r[2],
            "special_event": r[3],
            "revenue": r[4],
            "cost": r[5],
            "gross_margin": r[6],
            "result_json": r[7],
            "result": json.loads(r[7]) if r[7] else {},
        })
    return result


def check_prediction_exists(store_id: int, predict_date: str) -> bool:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM predictions WHERE store_id = ? AND predict_date = ?", (store_id, predict_date))
        count = c.fetchone()[0]
    return count > 0


def get_prediction_pushed(store_id: int, predict_date: str) -> bool:
    """查询该预测记录是否已推送采购单（去重用）。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT COALESCE(pushed, 0) FROM predictions WHERE store_id = ? AND predict_date = ?", (store_id, predict_date))
        row = c.fetchone()
    return bool(row and row[0])


def mark_prediction_pushed(store_id: int, predict_date: str):
    """标记该预测记录的采购单已推送。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("UPDATE predictions SET pushed = 1 WHERE store_id = ? AND predict_date = ?", (store_id, predict_date))


def get_prediction_by_store_date(store_id: int, predict_date: str) -> dict | None:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("""SELECT revenue, cost, gross_margin, result_json
                     FROM predictions WHERE store_id = ? AND predict_date = ?
                     ORDER BY id DESC LIMIT 1""", (store_id, predict_date))
        row = c.fetchone()
    if row:
        return {
            "revenue": row[0], "cost": row[1], "gross_margin": row[2],
            "result": json.loads(row[3]) if row[3] else {}
        }
    return None


# ==================== 历史销量 CRUD ====================

def upsert_sales_history(store_id: int, dish_id: int, sale_date: str, qty: float, unit_price: float):
    with _tx() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO sales_history (store_id, dish_id, sale_date, qty, unit_price, revenue)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(store_id, dish_id, sale_date)
            DO UPDATE SET qty = excluded.qty, unit_price = excluded.unit_price, revenue = excluded.revenue
        """, (store_id, dish_id, sale_date, qty, unit_price, qty * unit_price))


def get_default_history_days() -> int:
    """预测/拉取的默认历史窗口（天）。可通过 GUOBEICAI_HISTORY_DAYS 调整。"""
    try:
        return max(30, int(os.getenv("GUOBEICAI_HISTORY_DAYS", "180")))
    except Exception:
        return 180


def backfill_history_from_actual(store_id: int = None, sale_date: str = None,
                                 only_reconciled: bool = True) -> dict:
    """将 actual_sales 回灌到 sales_history（预测训练用的历史销量）。

    幂等：基于 sales_history 的 UNIQUE(store_id, dish_id, sale_date) 做覆盖写，
    重复调用不丢数据、不产生重复。用于「实际销量完成对账后持续 feed 预测历史」。

    :param store_id: 仅回灌该门店（None=全部门店）
    :param sale_date: 仅回灌该日期（None=不限日期）
    :param only_reconciled: True=仅回灌已对账(reconciled=1)的记录（POS 对接默认已对账）
    :return: {"rows": 扫描行数, "upserted": 写入行数}
    """
    with _tx() as conn:
        c = conn.cursor()
        sql = ("SELECT store_id, dish_id, sale_date, actual_qty, amount "
               "FROM actual_sales WHERE 1=1")
        params = []
        if store_id:
            sql += " AND store_id = ?"
            params.append(store_id)
        if sale_date:
            sql += " AND sale_date = ?"
            params.append(sale_date)
        if only_reconciled:
            sql += " AND reconciled = 1"
        c.execute(sql, params)
        rows = c.fetchall()
        upserted = 0
        for store_id_v, dish_id_v, sdate, qty, amount in rows:
            unit_price = (amount / qty) if (amount and qty) else 0.0
            try:
                upsert_sales_history(store_id_v, dish_id_v, sdate, qty, unit_price)
                upserted += 1
            except Exception as e:
                logger.warning("[回灌] sales_history 写入失败 store=%s dish=%s date=%s: %s",
                               store_id_v, dish_id_v, sdate, e)
    return {"rows": len(rows), "upserted": upserted}


def get_sales_history_df(store_id: int = None, days: int = None) -> pd.DataFrame:
    """读取历史销量（按门店/全局），可选时间窗（近 days 天）。

    [B2 性能加固] days>0 时在 SQL 层按 sale_date 截取近期窗口，
    避免单店全量历史一次性载入（历史越长、菜品越多，内存与耗时越高）。
    预测只需近期窗口，故调度/预测路径应传入 days。
    """
    with _tx() as conn:
        base = """
            SELECT sh.store_id, s.name as store_name, sh.dish_id, d.name as dish_name,
                   sh.sale_date, sh.qty, sh.unit_price, sh.revenue
            FROM sales_history sh
            JOIN stores s ON sh.store_id = s.id
            JOIN dishes d ON sh.dish_id = d.id
        """
        if store_id:
            if days and days > 0:
                query = base + " WHERE sh.store_id = ? AND sh.sale_date >= date('now', ?) ORDER BY sh.sale_date"
                df = pd.read_sql_query(query, conn, params=(store_id, f"-{int(days)} days"))
            else:
                query = base + " WHERE sh.store_id = ? ORDER BY sh.sale_date"
                df = pd.read_sql_query(query, conn, params=(store_id,))
        else:
            if days and days > 0:
                query = base + " WHERE sh.sale_date >= date('now', ?) ORDER BY sh.sale_date"
                df = pd.read_sql_query(query, conn, params=(f"-{int(days)} days",))
            else:
                query = base + " ORDER BY sh.sale_date"
                df = pd.read_sql_query(query, conn)
    return df


# ==================== 日期影响因素 CRUD ====================

def upsert_date_factor(store_id: int, factor_date: str, weather_text: str = "", temperature: float = 0, is_holiday: int = 0, special_event: str = ""):
    with _tx() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO date_factors (store_id, factor_date, weather_text, temperature, is_holiday, special_event)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(store_id, factor_date)
            DO UPDATE SET weather_text = excluded.weather_text, temperature = excluded.temperature,
                          is_holiday = excluded.is_holiday, special_event = excluded.special_event
        """, (store_id, factor_date, weather_text, temperature, is_holiday, special_event))


def get_date_factor(store_id: int, factor_date: str) -> dict | None:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT weather_text, temperature, is_holiday, special_event FROM date_factors WHERE store_id = ? AND factor_date = ?", (store_id, factor_date))
        row = c.fetchone()
    if row:
        return {"weather_text": row[0], "temperature": row[1], "is_holiday": row[2], "special_event": row[3]}
    return None


def get_date_factors_for_store(store_id: int) -> dict:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT factor_date, weather_text, temperature, is_holiday, special_event FROM date_factors WHERE store_id = ?", (store_id,))
        rows = c.fetchall()
    result = {}
    for r in rows:
        result[r[0]] = {"weather_text": r[1], "temperature": r[2], "is_holiday": r[3], "special_event": r[4]}
    return result


# ==================== 实际销量 CRUD（增强版） ====================

def upsert_actual_sales(store_id: int, dish_id: int, sale_date: str, actual_qty: float, actual_waste: float = 0, notes: str = None, actual_amount: float = 0, reconciled: int = 0):
    """
    新增或更新实际销量（同一门店+菜品+日期覆盖写）。
    使用 INSERT OR REPLACE 基于 UNIQUE(store_id, dish_id, sale_date) 约束。
    actual_amount：销售额（POS 对接时可落位）；缺省 0，与未携带金额的手工/小程序录入兼容。
    冲突更新时：若本次未提供金额（excluded.amount 为 0/空），保留已有金额，避免手工补录误清空 POS 金额。
    reconciled：对账标记。POS 对接写入成功即置 1；手工/小程序录入默认 0，待对账后 feed 预测历史。
    """
    with _tx() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO actual_sales (store_id, dish_id, sale_date, actual_qty, actual_waste, notes, amount, reconciled)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(store_id, dish_id, sale_date)
            DO UPDATE SET actual_qty = excluded.actual_qty, actual_waste = excluded.actual_waste,
                          notes = excluded.notes,
                          reconciled = CASE WHEN excluded.reconciled = 1 THEN 1 ELSE actual_sales.reconciled END,
                          amount = CASE WHEN excluded.amount IS NOT NULL AND excluded.amount <> 0
                                        THEN excluded.amount ELSE actual_sales.amount END
        """, (store_id, dish_id, sale_date, actual_qty, actual_waste, notes, actual_amount, int(reconciled or 0)))


def get_actual_sales_by_store_date(store_id: int, sale_date: str) -> dict:
    """
    查某门店某日期的所有实际销量，返回 {dish_id: actual_qty} 字典。
    用于销量录入页面回填默认值。
    """
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT dish_id, actual_qty FROM actual_sales WHERE store_id = ? AND sale_date = ?", (store_id, sale_date))
        rows = c.fetchall()
    return {r[0]: r[1] for r in rows}


def get_actual_sales_full_by_store_date(store_id: int, sale_date: str) -> dict:
    """
    查某门店某日期的实际销量与损耗，返回 {dish_id: {"qty": qty, "waste": waste}} 字典。
    用于每日销量/损耗录入页面回填默认值。
    """
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT dish_id, actual_qty, actual_waste FROM actual_sales WHERE store_id = ? AND sale_date = ?", (store_id, sale_date))
        rows = c.fetchall()
    return {r[0]: {"qty": r[1] or 0, "waste": r[2] or 0} for r in rows}


def get_actual_sales_reconciled_by_store_date(store_id: int, sale_date: str) -> dict:
    """返回 {dish_id: reconciled}，标记实际销量的数据来源（1=POS自动/已对账, 0=手工）。

    供「预测对比」页面区分每条实际销量的来源，不改动 actual_sales 本身。
    """
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT dish_id, reconciled FROM actual_sales WHERE store_id = ? AND sale_date = ?",
                  (store_id, sale_date))
        rows = c.fetchall()
    return {r[0]: int(r[1] or 0) for r in rows}


def get_latest_actual_qty(store_id: int, dish_id: int) -> float:
    """
    查某菜品最近一次实际销量（取最新 sale_date）。
    用于"无预测数据时取历史最新销量"作为默认值。
    """
    with _tx() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT actual_qty FROM actual_sales
            WHERE store_id = ? AND dish_id = ?
            ORDER BY sale_date DESC, id DESC LIMIT 1
        """, (store_id, dish_id))
        row = c.fetchone()
    return row[0] if row else 0.0


def get_actual_sales_range(store_id: int, start_date: str, end_date: str) -> list:
    """
    查某门店在日期区间内的实际销量与损耗明细。
    返回 [{dish_id, sale_date, actual_qty, actual_waste}, ...]
    """
    with _tx() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT dish_id, sale_date, actual_qty, actual_waste
            FROM actual_sales
            WHERE store_id = ? AND sale_date >= ? AND sale_date <= ?
        """, (store_id, start_date, end_date))
        rows = c.fetchall()
    return [{"dish_id": r[0], "sale_date": r[1], "actual_qty": r[2], "actual_waste": r[3]} for r in rows]


def get_sales_avg_by_dish(store_id: int) -> dict:
    """返回该门店每个菜品的历史平均日销量 {dish_id: avg_qty}。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT dish_id, AVG(qty) FROM sales_history WHERE store_id = ? GROUP BY dish_id", (store_id,))
        rows = c.fetchall()
    return {r[0]: float(r[1]) for r in rows}


def get_upcoming_holidays(store_id: int, from_date: str, to_date: str) -> list:
    """返回日期区间内标记为节假日的 factor_date 列表。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT factor_date FROM date_factors WHERE store_id = ? AND factor_date >= ? AND factor_date <= ? AND is_holiday = 1 ORDER BY factor_date",
            (store_id, from_date, to_date),
        )
        rows = c.fetchall()
    return [r[0] for r in rows]


def load_actual_sales(store_id: int = None, sale_date: str = None) -> list:
    """加载实际销量（兼容旧调用方式）"""
    with _tx() as conn:
        c = conn.cursor()
        if store_id and sale_date:
            c.execute("SELECT dish_id, actual_qty, actual_waste, notes FROM actual_sales WHERE store_id = ? AND sale_date = ?", (store_id, sale_date))
        elif store_id:
            c.execute("SELECT dish_id, sale_date, actual_qty, actual_waste, notes FROM actual_sales WHERE store_id = ?", (store_id,))
        else:
            c.execute("SELECT dish_id, sale_date, actual_qty, actual_waste, notes FROM actual_sales")
        rows = c.fetchall()
    if store_id and sale_date:
        return [{"dish_id": r[0], "actual_qty": r[1], "actual_waste": r[2], "notes": r[3]} for r in rows]
    else:
        return [{"dish_id": r[0], "sale_date": r[1], "actual_qty": r[2], "actual_waste": r[3], "notes": r[4]} for r in rows]


# ==================== POS/收银数据源对接（#商业化：通用可配置数据源） ====================

def _row_to_pos_source(r, decrypt_auth: bool = False) -> dict:
    keys = ["id", "store_id", "name", "source_type", "base_url", "auth_type",
            "auth_config", "field_mapping", "request_config", "schedule", "enabled",
            "last_sync_at", "last_sync_status", "last_sync_msg", "created_at"]
    d = dict(zip(keys, r))
    # field_mapping 始终解析为 dict
    if isinstance(d.get("field_mapping"), str):
        try:
            d["field_mapping"] = json.loads(d["field_mapping"] or "{}")
        except Exception:
            d["field_mapping"] = {}
    # request_config 解析为 dict（请求侧报文配置）
    if isinstance(d.get("request_config"), str):
        try:
            d["request_config"] = json.loads(d["request_config"] or "{}")
        except Exception:
            d["request_config"] = {}
    # auth_config：默认保留为密文字符串（不向外暴露明文）；decrypt_auth=True 时解密为 dict（内部拉取用）
    raw = d.get("auth_config")
    if decrypt_auth:
        d["auth_config"] = _decrypt_auth_to_dict(raw)
    # 非解密路径：保持 raw（密文或旧明文），调用方不应当 dict 直接使用
    d["enabled"] = bool(d.get("enabled", 1))
    return d


def _decrypt_auth_to_dict(raw) -> dict:
    """把 pos_sources.auth_config 列（密文字符串/旧明文/None）解析为 dict。"""
    if not raw:
        return {}
    s = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
    if s.startswith(_crypto.ENC_PREFIX):
        try:
            s = _crypto.decrypt_secret(s)
        except Exception:
            return {}
    try:
        return json.loads(s or "{}") if isinstance(s, str) else s
    except Exception:
        return {}


def _merge_auth_keep_existing(existing: dict, incoming: dict) -> dict:
    """更新凭证时「留空 / 掩码占位符」表示不修改，保留原值；新值则覆盖。

    仅在 auth_config 已加密存储的前提下使用：页面/API 回显的是 MASK_SENTINEL，
    用户未改则原样回传，此处据此保留已存密文对应的明文。
    """
    merged = dict(incoming)
    for k, v in incoming.items():
        if k in _crypto.POS_SENSITIVE_KEYS and (v is None or v == "" or v == _crypto.MASK_SENTINEL):
            merged[k] = existing.get(k, v)
    return merged


def mask_pos_source(source: dict) -> dict:
    """脱敏：对 auth_config 中的敏感字段以 MASK_SENTINEL 占位（服务器侧解密后打码，不外泄明文）。"""
    s = dict(source)
    d = _decrypt_auth_to_dict(source.get("auth_config"))
    for k in _crypto.POS_SENSITIVE_KEYS:
        if k in d and d[k]:
            d[k] = _crypto.MASK_SENTINEL
    s["auth_config"] = d
    return s


def upsert_pos_source(store_id, name, source_type="http_json", base_url="", auth_type="none",
                      auth_config=None, field_mapping=None, request_config=None, schedule="", enabled=1,
                      source_id=None) -> int:
    """新增或更新某门店的 POS/收银数据源配置。source_id 给定则更新。

    [S3 安全加固] auth_config 敏感字段加密存储；更新时「留空/掩码」保留原值。
    request_config：请求侧报文配置（方法/参数/请求体/自定义头），JSON dict。
    """
    incoming = auth_config or {}
    field_mapping = field_mapping or {}
    request_config = request_config or {}
    # [S5] SSRF 防护：保存即拒绝危险地址（内网/回环/保留网段）
    if base_url:
        ok, reason = _net.validate_pos_url(base_url)
        if not ok:
            raise ValueError(f"接口地址不安全，已拒绝保存：{reason}")
    # 更新且传入了 auth_config：对敏感字段做「留空/掩码则保留原值」处理
    if source_id:
        existing = _decrypt_auth_to_dict(_get_raw_auth_config(source_id))
        incoming = _merge_auth_keep_existing(existing, incoming)
    # 加密后存储（整段 auth_config 以密文落库）
    enc_auth = _crypto.encrypt_secret(json.dumps(incoming, ensure_ascii=False))
    with _tx() as conn:
        c = conn.cursor()
        if source_id:
            c.execute(
                "UPDATE pos_sources SET name=?, source_type=?, base_url=?, auth_type=?, "
                "auth_config=?, field_mapping=?, request_config=?, schedule=?, enabled=? WHERE id=?",
                (name, source_type, base_url, auth_type, enc_auth,
                 json.dumps(field_mapping, ensure_ascii=False),
                 json.dumps(request_config, ensure_ascii=False), schedule, int(enabled), source_id),
            )
            sid = source_id
        else:
            c.execute(
                "INSERT INTO pos_sources (store_id, name, source_type, base_url, auth_type, "
                "auth_config, field_mapping, request_config, schedule, enabled) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (store_id, name, source_type, base_url, auth_type,
                 enc_auth, json.dumps(field_mapping, ensure_ascii=False),
                 json.dumps(request_config, ensure_ascii=False),
                 schedule, int(enabled)),
            )
            sid = c.lastrowid
    return sid


def _get_raw_auth_config(source_id: int):
    """读取 pos_sources.auth_config 原始列（密文/明文），用于更新时合并。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT auth_config FROM pos_sources WHERE id=?", (source_id,))
        row = c.fetchone()
    return row[0] if row else None


def get_pos_source_decrypted(source_id: int) -> dict | None:
    """内部拉取/测试用：返回 auth_config 已解密的源配置。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id, store_id, name, source_type, base_url, auth_type, auth_config, "
                  "field_mapping, request_config, schedule, enabled, last_sync_at, last_sync_status, last_sync_msg, created_at "
                  "FROM pos_sources WHERE id=?", (source_id,))
        row = c.fetchone()
    return _row_to_pos_source(row, decrypt_auth=True) if row else None


def get_all_pos_sources_decrypted() -> list:
    """内部调度拉取用：返回 auth_config 已解密的源配置列表。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id, store_id, name, source_type, base_url, auth_type, auth_config, "
                  "field_mapping, request_config, schedule, enabled, last_sync_at, last_sync_status, last_sync_msg, created_at "
                  "FROM pos_sources ORDER BY store_id, id")
        rows = c.fetchall()
    return [_row_to_pos_source(r, decrypt_auth=True) for r in rows]


def get_pos_sources_by_store(store_id: int) -> list:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id, store_id, name, source_type, base_url, auth_type, auth_config, "
                  "field_mapping, request_config, schedule, enabled, last_sync_at, last_sync_status, last_sync_msg, created_at "
                  "FROM pos_sources WHERE store_id=? ORDER BY id", (store_id,))
        rows = c.fetchall()
    return [_row_to_pos_source(r) for r in rows]


def get_all_pos_sources() -> list:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id, store_id, name, source_type, base_url, auth_type, auth_config, "
                  "field_mapping, request_config, schedule, enabled, last_sync_at, last_sync_status, last_sync_msg, created_at "
                  "FROM pos_sources ORDER BY store_id, id")
        rows = c.fetchall()
    return [_row_to_pos_source(r) for r in rows]


def get_pos_source(source_id: int) -> dict | None:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id, store_id, name, source_type, base_url, auth_type, auth_config, "
                  "field_mapping, request_config, schedule, enabled, last_sync_at, last_sync_status, last_sync_msg, created_at "
                  "FROM pos_sources WHERE id=?", (source_id,))
        row = c.fetchone()
    return _row_to_pos_source(row) if row else None


def delete_pos_source(source_id: int):
    with _tx() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM pos_sources WHERE id=?", (source_id,))


def set_pos_sync_result(source_id: int, status: str, msg: str, store_id: int = None):
    """记录同步结果并写入同步日志。"""
    with _tx() as conn:
        c = conn.cursor()
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute("UPDATE pos_sources SET last_sync_at=?, last_sync_status=?, last_sync_msg=? WHERE id=?",
                  (now, status, msg[:500], source_id))
        c.execute(
            "INSERT INTO pos_sync_log (source_id, store_id, run_at, status, msg) VALUES (?,?,?,?,?)",
            (source_id, store_id, now, status, msg[:500]),
        )


def get_pos_sync_log(source_id: int = None, limit: int = 20) -> list:
    with _tx() as conn:
        c = conn.cursor()
        if source_id:
            c.execute("SELECT id, source_id, store_id, run_at, status, msg FROM pos_sync_log "
                      "WHERE source_id=? ORDER BY id DESC LIMIT ?", (source_id, limit))
        else:
            c.execute("SELECT id, source_id, store_id, run_at, status, msg FROM pos_sync_log "
                      "ORDER BY id DESC LIMIT ?", (limit,))
        rows = c.fetchall()
    return [{"id": r[0], "source_id": r[1], "store_id": r[2], "run_at": r[3],
            "status": r[4], "msg": r[5]} for r in rows]


# ==================== 回测体系（#17 准确度评估 / #14 误差反馈闭环） ====================

def save_bias_correction(store_id: int, dish_name: str, factor: float):
    """写入/更新某门店某菜品的偏差校正因子（回测闭环产物）。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            """INSERT INTO bias_corrections (store_id, dish_name, factor, updated_at)
               VALUES (?, ?, ?, datetime('now', 'localtime'))
               ON CONFLICT(store_id, dish_name)
               DO UPDATE SET factor=excluded.factor, updated_at=excluded.updated_at""",
            (store_id, dish_name, float(factor)),
        )


def get_bias_corrections(store_id: int) -> dict:
    """返回 {dish_name: factor}，供预测流程应用误差校正。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT dish_name, factor FROM bias_corrections WHERE store_id = ?", (store_id,))
        rows = c.fetchall()
    return {r[0]: r[1] for r in rows}


def save_backtest_run(store_id, holdout_days: int, min_history_days: int, summary_json: dict):
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO backtest_runs (store_id, holdout_days, min_history_days, summary_json) VALUES (?, ?, ?, ?)",
            (store_id, int(holdout_days), int(min_history_days), json.dumps(summary_json, ensure_ascii=False)),
        )


def load_backtest_runs(limit: int = 20) -> list:
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, store_id, run_time, holdout_days, min_history_days, summary_json "
            "FROM backtest_runs ORDER BY id DESC LIMIT ?",
            (int(limit),),
        )
        rows = c.fetchall()
    return [
        {"id": r[0], "store_id": r[1], "run_time": r[2], "holdout_days": r[3],
         "min_history_days": r[4], "summary": json.loads(r[5]) if r[5] else {}}
        for r in rows
    ]


def load_comparison(store_id: int, sale_date: str) -> list:
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT p.result_json
            FROM predictions p
            WHERE p.store_id = ? AND p.predict_date = ?
            ORDER BY p.id DESC LIMIT 1
        """, (store_id, sale_date))
        row = c.fetchone()
        if not row:
            return []

        result = json.loads(row[0]) if row[0] else {}
        purchase_details = result.get("purchase_details", [])
        dish_predictions = result.get("dish_predictions", {})

        c.execute("SELECT dish_id, actual_qty, actual_waste, notes FROM actual_sales WHERE store_id = ? AND sale_date = ?", (store_id, sale_date))
        actual_rows = c.fetchall()
        actual_map = {r[0]: {"qty": r[1], "waste": r[2], "notes": r[3]} for r in actual_rows}

        c.execute("SELECT id, name, category FROM dishes WHERE store_id = ?", (store_id,))
        dish_rows = c.fetchall()
        dish_map = {r[0]: {"name": r[1], "category": r[2]} for r in dish_rows}


        comparison = []
        for dish_id, dish_info in dish_map.items():
            dish_name = dish_info["name"]
            predicted_qty = dish_predictions.get(dish_name, 0)
            actual = actual_map.get(dish_id, {})
            actual_qty = actual.get("qty", None)
            if actual_qty is not None:
                qty_bias_abs = actual_qty - predicted_qty
                qty_bias_pct = (qty_bias_abs / predicted_qty * 100) if predicted_qty > 0 else 0
            else:
                qty_bias_abs = None
                qty_bias_pct = None
            dish_cost = sum(p.get("总成本(元)", 0) for p in purchase_details if p.get("关联菜品") == dish_name)
            comparison.append({
                "dish_id": dish_id,
                "dish_name": dish_name,
                "category": dish_info.get("category", ""),
                "predicted_qty": predicted_qty,
                "actual_qty": actual_qty,
                "qty_bias_abs": qty_bias_abs,
                "qty_bias_pct": qty_bias_pct,
                "predicted_waste": 0,
                "actual_waste": actual.get("waste", 0),
                "predicted_revenue": 0,
                "predicted_cost": dish_cost,
                "predicted_margin": 0,
                "notes": actual.get("notes", ""),
            })
        return comparison
    finally:
        conn.close()


def _migrate_recipes_unit_text():
    """兼容已有数据库：recipes.unit 列类型应为 TEXT（原先误声明为 REAL）。

    已加固为「崩溃安全」+「数据优先」：判定优先级与 _migrate_actual_sales_unique 一致——
    旧表含数据则为权威源；旧表缺失/为空则以 recipes_new 为权威；二者皆空则从空旧表重建。
    """
    conn = get_conn()
    try:
        c = conn.cursor()

        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='recipes'")
        old_exists = c.fetchone() is not None
        old_unit_type = None
        old_count = 0
        if old_exists:
            cols = {r[1]: r[2] for r in c.execute("PRAGMA table_info(recipes)").fetchall()}
            old_unit_type = cols.get("unit")
            try:
                old_count = c.execute("SELECT COUNT(*) FROM recipes").fetchone()[0]
            except Exception:
                old_count = 0

        c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='recipes_new'")
        new_exists = c.fetchone() is not None
        new_count = 0
        if new_exists:
            try:
                new_count = c.execute("SELECT COUNT(*) FROM recipes_new").fetchone()[0]
            except Exception:
                new_count = 0

        # 1) 旧表含数据 -> 旧表为权威源
        if old_exists and old_count > 0:
            if new_exists:
                c.execute("DROP TABLE IF EXISTS recipes_new")
            if old_unit_type != "REAL":
                conn.commit()
                return
            # 旧表 unit=REAL -> 以旧表为源重建为 TEXT
            c.execute("""
                CREATE TABLE recipes_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dish_id INTEGER NOT NULL,
                    ingredient_name TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    unit TEXT DEFAULT '',
                    unit_price REAL DEFAULT 0,
                    category TEXT DEFAULT '',
                    FOREIGN KEY (dish_id) REFERENCES dishes(id)
                )
            """)
            c.execute("SELECT id, dish_id, ingredient_name, quantity, unit, unit_price, category FROM recipes")
            for row in c.fetchall():
                c.execute(
                    "INSERT INTO recipes_new (id, dish_id, ingredient_name, quantity, unit, unit_price, category) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
            c.execute("DROP TABLE recipes")
            c.execute("ALTER TABLE recipes_new RENAME TO recipes")
            conn.commit()
            return

        # 2) 旧表缺失/为空 -> 以新表为权威
        if new_exists and new_count > 0:
            c.execute("DROP TABLE IF EXISTS recipes")
            c.execute("ALTER TABLE recipes_new RENAME TO recipes")
            conn.commit()
            return
        if new_exists and new_count == 0:
            c.execute("DROP TABLE IF EXISTS recipes_new")

        # 3) 旧表为空且无新表 -> 从空旧表重建（保证 unit 为 TEXT）
        if old_exists and old_unit_type == "REAL":
            logger.info("[迁移] recipes.unit 为 REAL，重建为 TEXT")
            c.execute("""
                CREATE TABLE recipes_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dish_id INTEGER NOT NULL,
                    ingredient_name TEXT NOT NULL,
                    quantity REAL NOT NULL,
                    unit TEXT DEFAULT '',
                    unit_price REAL DEFAULT 0,
                    category TEXT DEFAULT '',
                    FOREIGN KEY (dish_id) REFERENCES dishes(id)
                )
            """)
            c.execute("SELECT id, dish_id, ingredient_name, quantity, unit, unit_price, category FROM recipes")
            for row in c.fetchall():
                c.execute(
                    "INSERT INTO recipes_new (id, dish_id, ingredient_name, quantity, unit, unit_price, category) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    row,
                )
            c.execute("DROP TABLE recipes")
            c.execute("ALTER TABLE recipes_new RENAME TO recipes")
        conn.commit()
    finally:
        conn.close()


# ==================== 用户管理 CRUD（完整增删改查） ====================

def init_users():
    """初始化默认账号（role 存角色编码，与 init_roles 保持一致）。

    安全加固（S1 / C6）：
    - 默认账户一律带 force_change_pwd=1，首次登录强制改密；
    - `GUOBEICAI_DISABLE_SEED=1` 可完全禁止自动种账户；
    - **生产环境（GUOBEICAI_ENV=production/prod/1）默认不种任何账户**
      （避免上线即存在 admin/admin、test/test 这类裸奔账户）；
      如确需在类生产环境种号，请显式设置 `GUOBEICAI_ALLOW_SEED=1`，
      或使用管理脚本手动创建管理员。
    """
    if os.getenv("GUOBEICAI_DISABLE_SEED", "").lower() in ("1", "true", "yes", "on"):
        logger.warning(
            "[安全] 已禁用默认账户自动创建（GUOBEICAI_DISABLE_SEED=1）。"
            "生产环境请使用管理脚本手动创建管理员账户。"
        )
        return
    _env = os.getenv("GUOBEICAI_ENV", os.getenv("ENV", "development")).strip().lower()
    _is_prod = _env in ("production", "prod", "1")
    _allow_seed = os.getenv("GUOBEICAI_ALLOW_SEED", "").lower() in ("1", "true", "yes", "on")
    if _is_prod and not _allow_seed:
        logger.warning(
            "[安全] 生产环境（GUOBEICAI_ENV=%s）默认不创建任何种子账户（admin/test）。"
            "请用管理脚本创建管理员；如确需种子账户，请显式设置 GUOBEICAI_ALLOW_SEED=1。", _env
        )
        return
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM users WHERE username = 'admin'")
        if c.fetchone()[0] == 0:
            c.execute(
                "INSERT INTO users (username, password_hash, role, store_id, force_change_pwd) VALUES (?, ?, ?, 0, 1)",
                ("admin", hash_password("admin"), "admin"),
            )
            logger.warning("[安全] 已创建默认管理员账户 admin/admin —— 请于首次登录后立即修改密码！")
        c.execute("SELECT COUNT(*) FROM users WHERE username = 'test'")
        if c.fetchone()[0] == 0:
            c.execute(
                "INSERT INTO users (username, password_hash, role, store_id, force_change_pwd) VALUES (?, ?, ?, 0, 1)",
                ("test", hash_password("test"), "merchant"),
            )
            logger.warning("[安全] 已创建默认测试账户 test/test —— 请于首次登录后立即修改密码！")


def authenticate_user(username: str, password: str) -> dict | None:
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(
            "SELECT id, username, password_hash, role, COALESCE(store_id, 0), "
            "COALESCE(force_change_pwd, 0), valid_from, valid_until FROM users WHERE username = ?",
            (username,),
        )
        row = c.fetchone()
        if not row:
            return None
        stored_hash = row[2]
        if verify_password(stored_hash, password):
            user = {
                "id": row[0],
                "username": row[1],
                "role": row[3],
                "store_id": row[4],
                "need_change_pwd": bool(row[5]),
                "valid_from": row[6],
                "valid_until": row[7],
            }
            # 旧版 SHA-256 哈希：登录成功时自动升级为 scrypt（一次性、原地）
            if not stored_hash.startswith("scrypt$"):
                try:
                    c.execute(
                        "UPDATE users SET password_hash = ? WHERE id = ?",
                        (hash_password(password), row[0]),
                    )
                    conn.commit()
                except Exception as e:
                    logger.warning("密码哈希升级失败 user=%s: %s", username, e)
            return user
        return None
    finally:
        conn.close()


def get_user_by_id(user_id: int) -> dict | None:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id, username, role, COALESCE(store_id, 0), valid_from, valid_until FROM users WHERE id = ?", (user_id,))
        row = c.fetchone()
    if row:
        return {"id": row[0], "username": row[1], "role": row[2], "store_id": row[3],
                "valid_from": row[4], "valid_until": row[5]}
    return None


# ==================== 微信 openID 绑定（移动端推送身份识别） ====================

def get_user_by_openid(openid: str) -> dict | None:
    """按 openid 查已绑定用户（users.openid 唯一）。返回含 openid 的用户字典。"""
    if not openid:
        return None
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, username, role, COALESCE(store_id, 0), openid, COALESCE(force_change_pwd, 0), valid_from, valid_until FROM users WHERE openid = ?",
            (openid,),
        )
        row = c.fetchone()
    if row:
        return {
            "id": row[0], "username": row[1], "role": row[2], "store_id": row[3],
            "openid": row[4], "need_change_pwd": bool(row[5]),
            "valid_from": row[6], "valid_until": row[7],
        }
    return None


def bind_openid(user_id: int, openid: str) -> bool:
    """将微信 openid 绑定到用户。同一 openid 不可同时绑多人（UNIQUE 约束）。"""
    if not openid:
        return False
    conn = get_conn()
    try:
        c = conn.cursor()
        try:
            c.execute("UPDATE users SET openid = ? WHERE id = ?", (openid, user_id))
            conn.commit()
            ok = c.rowcount > 0
        except Exception as e:
            conn.rollback()
            logger.warning("绑定 openid 失败 user_id=%s: %s", user_id, e)
            ok = False
        return ok
    finally:
        conn.close()


def unbind_openid(user_id: int) -> None:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("UPDATE users SET openid = NULL WHERE id = ?", (user_id,))


def get_user_by_phone(phone: str) -> dict | None:
    """按手机号查用户（users.phone 部分唯一，非空时唯一）。

    手机号是小程序「微信绑定」的用户侧匹配键：用户在手机号授权/手输后，
    后端用本函数定位到对应 WEB 账号，再把本次微信 openid 绑上去。
    """
    if not phone:
        return None
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, username, role, COALESCE(store_id, 0), openid, COALESCE(force_change_pwd, 0), "
            "valid_from, valid_until, phone FROM users WHERE phone = ?",
            (phone,),
        )
        row = c.fetchone()
    if row:
        return {
            "id": row[0], "username": row[1], "role": row[2], "store_id": row[3],
            "openid": row[4], "need_change_pwd": bool(row[5]),
            "valid_from": row[6], "valid_until": row[7], "phone": row[8],
        }
    return None


def bind_openid_by_phone(phone: str, openid: str) -> dict:
    """按手机号把微信 openid 绑到对应用户（小程序「手机号绑定」入口）。

    返回 {"ok": bool, "reason": str|None, "user": dict|None}
    reason 取值：bad_input / no_user（手机号未开通）/ openid_taken（该微信已绑他人）
                / inactive（账号未生效或已过期）/ bind_failed。
    成功时 user 已带最新 openid，可直接用于签发令牌。
    """
    if not phone or not openid:
        return {"ok": False, "reason": "bad_input", "user": None}
    user = get_user_by_phone(phone)
    if not user:
        return {"ok": False, "reason": "no_user", "user": None}
    exist = get_user_by_openid(openid)
    if exist and exist["id"] != user["id"]:
        return {"ok": False, "reason": "openid_taken", "user": None}
    ok_valid, _ = user_validity_status(user)
    if not ok_valid:
        return {"ok": False, "reason": "inactive", "user": None}
    if not bind_openid(user["id"], openid):
        return {"ok": False, "reason": "bind_failed", "user": None}
    user["openid"] = openid
    return {"ok": True, "reason": None, "user": user}


def get_user_openid(user_id: int) -> str | None:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT openid FROM users WHERE id = ?", (user_id,))
        row = c.fetchone()
    return row[0] if row and row[0] else None


def get_all_users():
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id, username, role, COALESCE(store_id, 0), created_at, "
                   "valid_from, valid_until, authorized_by, openid, phone FROM users ORDER BY id")
        rows = c.fetchall()
    result = []
    for r in rows:
        uid = r[0]
        store_ids = get_user_store_ids(uid)
        result.append({
            "id": uid,
            "username": r[1],
            "role": r[2],
            "store_id": r[3],
            "store_ids": store_ids,
            "created_at": r[4],
            "valid_from": r[5],
            "valid_until": r[6],
            "authorized_by": r[7],
            "openid": r[8],
            "phone": r[9],
        })
    return result


def get_visible_store_accounts(user: dict) -> list:
    """返回当前用户可管理的「商家用户(门店)」账号列表（多租户隔离）。

    - 管理员 / 租户管理员：返回全部商家用户账号；
    - 渠道商：仅返回「绑定门店全部属于自身可访问门店」的商家用户账号，
      看不到管理员 / 渠道账号，也看不到绑定越权门店的账号。
    供「渠道门店账号」页（及后续收敛「用户管理」的商家视图）复用，测试也可直接调用。
    """
    accessible_ids = {s["id"] for s in get_accessible_stores(user, active_only=True)}
    is_admin_user = is_admin_role(user.get("role", ""))
    out = []
    for u in get_all_users():
        if u.get("role") != "merchant":
            continue
        sids = set(u.get("store_ids") or [])
        if not sids:
            if is_admin_user:
                out.append(u)
            continue
        if sids.issubset(accessible_ids):
            out.append(u)
    return out


def add_user(username: str, password: str, role: str = "商家用户", store_ids: list = None,
             valid_from: str = None, valid_until: str = None, authorized_by: int = None,
             openid: str = None, phone: str = None) -> bool:
    """
    新增用户，store_ids 为门店ID列表（支持多门店）。
    新建用户默认 force_change_pwd=1，首次登录强制改密（S1）。
    valid_from / valid_until：账号有效期（YYYY-MM-DD，NULL 表示无期限），按合同期限管控。
    authorized_by：创建者账号 id（用于渠道归属统计；管理员自建可为 None 或自身 id）。
    openid：微信 OpenID（可选，内部字段）。一般留空，由用户在小程序手机号绑定时自动写入。
    phone：手机号（可选）。非空时须全局唯一；作为小程序微信绑定的匹配键，
           用户在手机号授权/手输后由后端按此列定位账号并绑定 openid。
    """
    try:
        if openid and get_user_by_openid(openid):
            return False
        if phone and get_user_by_phone(phone):
            return False
        # 用户行 + 门店绑定放在同一事务提交：任一步失败整体回滚，
        # 既不留下「半成品账号」，也避免异常路径泄漏连接（曾导致后续写操作 database is locked）。
        norm_ids = sorted(set(int(x) for x in (store_ids or []) if int(x) != 0))
        primary_store = norm_ids[0] if norm_ids else 0
        with _tx() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO users (username, password_hash, role, store_id, force_change_pwd, valid_from, valid_until, authorized_by, openid, phone) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?)",
                (username, hash_password(password), role, primary_store, valid_from or None, valid_until or None, authorized_by, openid or None, phone or None),
            )
            user_id = c.lastrowid
            for sid in norm_ids:
                c.execute("INSERT OR IGNORE INTO user_stores (user_id, store_id) VALUES (?, ?)", (user_id, sid))
        # 多租户登录路由：若在某个租户库内建账号，自动把该账号登记进主控注册表
        # (tenant_accounts, username→tenant_id)。否则门店/渠道账号登录时查不到所属租户，
        # 会被路由到默认库而鉴权失败。默认库(运营方)建账号时 get_tenant() 为 None，跳过。
        _tid = get_tenant()
        if _tid:
            try:
                from guibeicai.web import _tenancy
                _tenancy.register_extra_account(_tid, username)
            except Exception:
                logger.warning("[tenancy] 账号 %s 登记登录路由失败（不影响建号本身）", username)
        return True
    except Exception as e:
        logger.warning("创建用户失败 username=%s role=%s: %s", username, role, e)
        return False


def update_user(user_id: int, username: str = None, role: str = None, store_id: int = None,
                valid_from: str = None, valid_until: str = None, phone: str = None):
    """修改用户（支持改用户名、角色、绑定门店、账号有效期、手机号）

    各字段传 None 表示不改动该字段；有效期/手机号传 ""（空串）表示清除为无期限/无手机号。
    手机号唯一性由调用方（页面）预校验后调用，DB 层部分唯一索引兜底。
    """
    with _tx() as conn:
        c = conn.cursor()
        if username is not None:
            c.execute("UPDATE users SET username = ? WHERE id = ?", (username, user_id))
        if role is not None:
            c.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
        if store_id is not None:
            c.execute("UPDATE users SET store_id = ? WHERE id = ?", (store_id, user_id))
        if valid_from is not None:
            c.execute("UPDATE users SET valid_from = ? WHERE id = ?", (valid_from or None, user_id))
        if valid_until is not None:
            c.execute("UPDATE users SET valid_until = ? WHERE id = ?", (valid_until or None, user_id))
        if phone is not None:
            c.execute("UPDATE users SET phone = ? WHERE id = ?", (phone or None, user_id))


def update_user_password(user_id: int, new_password: str, force_change_pwd: int = 0) -> None:
    """修改密码（scrypt 重哈希）。force_change_pwd=1 表示下次登录仍需强制改密
    （管理员后台重置他人密码时使用，避免明文密码被复用）。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            "UPDATE users SET password_hash = ?, force_change_pwd = ? WHERE id = ?",
            (hash_password(new_password), int(force_change_pwd), user_id),
        )


def delete_user(user_id: int) -> None:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM user_stores WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM users WHERE id = ?", (user_id,))


# ==================== 合同管理 CRUD（仅管理员） ====================

def add_contract(contract_no: str, name: str, amount: float = 0, payment_method: str = "",
                 party_a: str = "", party_b: str = "", sign_date: str = "",
                 effective_date: str = "", expiry_date: str = "",
                 linked_user_id: int = None, remark: str = "") -> int:
    """新增合同，返回新合同 id。"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute(
            """INSERT INTO contracts
               (contract_no, name, amount, payment_method, party_a, party_b, sign_date,
                effective_date, expiry_date, linked_user_id, remark)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (contract_no or "", name or "", float(amount or 0), payment_method or "",
             party_a or "", party_b or "", sign_date or "", effective_date or "",
             expiry_date or "", linked_user_id, remark or ""),
        )
        cid = c.lastrowid
    return cid


def update_contract(cid: int, **fields) -> None:
    """更新合同（字段名同表列：contract_no/name/amount/payment_method/party_a/party_b/
    sign_date/effective_date/expiry_date/linked_user_id/remark）。"""
    allowed = {"contract_no", "name", "amount", "payment_method", "party_a", "party_b",
               "sign_date", "effective_date", "expiry_date", "linked_user_id", "remark"}
    sets, params = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            params.append(float(v) if k == "amount" else (v or None if k == "linked_user_id" else (v or "")))
    if not sets:
        return
    sets.append("updated_at = datetime('now', 'localtime')")
    params.append(cid)
    with _tx() as conn:
        c = conn.cursor()
        c.execute(f"UPDATE contracts SET {', '.join(sets)} WHERE id = ?", params)


def set_contract_attachment(cid: int, path: str, name: str) -> None:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("UPDATE contracts SET attachment_path = ?, attachment_name = ? WHERE id = ?",
                  (path or "", name or "", cid))


def get_contract(cid: int) -> dict | None:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM contracts WHERE id = ?", (cid,))
        row = c.fetchone()
    return _row_to_contract(row) if row else None


def get_all_contracts() -> list:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM contracts ORDER BY id DESC")
        rows = c.fetchall()
    return [_row_to_contract(r) for r in rows]


def delete_contract(cid: int) -> None:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM contracts WHERE id = ?", (cid,))


def sync_contract_to_user(cid: int) -> dict:
    """将合同有效期同步到关联账号：linked_user 的 valid_from/valid_until = 合同生效/到期日。

    :return: {"ok": bool, "msg": str}
    """
    ct = get_contract(cid)
    if not ct:
        return {"ok": False, "msg": "合同不存在"}
    uid = ct.get("linked_user_id")
    if not uid:
        return {"ok": False, "msg": "合同未关联账号"}
    u = get_user_by_id(uid)
    if not u:
        return {"ok": False, "msg": "关联账号不存在"}
    update_user(uid, valid_from=ct.get("effective_date") or "", valid_until=ct.get("expiry_date") or "")
    return {"ok": True, "msg": f"已同步有效期到账号 {u['username']}"}


def _row_to_contract(row) -> dict:
    cols = ["id", "contract_no", "name", "amount", "payment_method", "party_a", "party_b",
            "sign_date", "effective_date", "expiry_date", "linked_user_id",
            "attachment_path", "attachment_name", "remark", "created_at", "updated_at"]
    return {cols[i]: row[i] for i in range(len(cols))}


# ==================== 角色管理 CRUD（完整增删改查） ====================

# 默认商家角色可访问的页面键（角色管理页可随时调整，无需改代码）
# 注：门店管理/菜品管理页已按商家绑定门店做数据隔离（非授权门店仅展示不可操作），
# 故商家默认即可访问自己的门店与菜品，管理员保留全部控制权。
DEFAULT_MERCHANT_PAGES = [
    "dashboard", "data_upload", "predict_config", "predict_overview",
    "ingredient", "compare", "backtest", "waste", "alert",
    "store_manage", "dish_manage", "ingredient_manage", "pos_integration",
]


def _migrate_roles_add_code_pages():
    """兼容已有数据库：roles 表增加 code（逻辑编码）与 pages（可访问页面键列表）。"""
    conn = get_conn()
    try:
        c = conn.cursor()
        cols = [r[1] for r in c.execute("PRAGMA table_info(roles)").fetchall()]
        if "code" not in cols:
            c.execute("ALTER TABLE roles ADD COLUMN code TEXT DEFAULT ''")
        if "pages" not in cols:
            c.execute("ALTER TABLE roles ADD COLUMN pages TEXT DEFAULT '[]'")
        conn.commit()
        # 回填 code：已知中文角色名 -> 稳定编码
        name_to_code = {"系统管理员": "admin", "商家用户": "merchant"}
        for nm, cd in name_to_code.items():
            c.execute("UPDATE roles SET code=? WHERE name=? AND (code IS NULL OR code='')", (cd, nm))
        # 其余角色无 code 时按 id 生成
        for r in c.execute("SELECT id, code FROM roles"):
            if not r[1]:
                c.execute("UPDATE roles SET code=? WHERE id=?", (f"role_{r[0]}", r[0]))
        # 回填 pages（兼容空串 / '[]' / NULL 多种历史情况）
        for r in c.execute("SELECT id, code, pages FROM roles"):
            rid, code, pages = r[0], r[1], (r[2] or "")
            try:
                _parsed = json.loads(pages) if pages else []
            except Exception:
                _parsed = []
            if not _parsed:
                p = '["*"]' if code == "admin" else json.dumps(DEFAULT_MERCHANT_PAGES, ensure_ascii=False)
                c.execute("UPDATE roles SET pages=? WHERE id=?", (p, rid))
        try:
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_roles_code ON roles(code)")
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()


def _migrate_users_role_to_code():
    """兼容已有数据库：users.role 由角色名改为角色编码（稳定，用于逻辑判断）。"""
    with _tx() as conn:
        c = conn.cursor()
        # 仅当 users.role 当前是角色名（存在于 roles.name）时才映射为 code
        c.execute("""UPDATE users SET role = (SELECT code FROM roles WHERE roles.name = users.role)
                     WHERE role IN (SELECT name FROM roles)""")


def _migrate_roles_add_pos_page():
    """兼容已有数据库：给商家角色追加 POS 对接页权限（不影响管理员 *）。"""
    with _tx() as conn:
        c = conn.cursor()
        for rid, code, pages in c.execute("SELECT id, code, pages FROM roles"):
            try:
                plist = json.loads(pages) if pages else []
            except Exception:
                plist = []
            if code == "admin" or "*" in plist:
                continue
            if "pos_integration" not in plist:
                plist.append("pos_integration")
                c.execute("UPDATE roles SET pages=? WHERE id=?", (json.dumps(plist, ensure_ascii=False), rid))


def _migrate_roles_add_channel_pages():
    """兼容已有数据库：给渠道商角色升级为「可写」——追加渠道门店与供货页权限。

    历史渠道商角色为只读（pages=['dashboard','channel_overview']），本次重构后
    渠道商需要录入门店并配置供货范围，故追加 'channel_store' 页面权限。
    """
    needed = ["dashboard", "channel_overview", "channel_store"]
    with _tx() as conn:
        c = conn.cursor()
        for rid, code, pages in c.execute("SELECT id, code, pages FROM roles"):
            if code != "channel":
                continue
            try:
                plist = json.loads(pages) if pages else []
            except Exception:
                plist = []
            if any(p not in plist for p in needed):
                for p in needed:
                    if p not in plist:
                        plist.append(p)
                c.execute("UPDATE roles SET pages=? WHERE id=?",
                          (json.dumps(plist, ensure_ascii=False), rid))


def _migrate_roles_channel_scoped_pages():
    """刷新渠道商角色页面权限（一次性迁移，用台账保证只跑一次）：

    - 移除 user_manage（「用户管理」整页权限，避免渠道越权改管理员密码/删账号）；
    - 新增独立「渠道门店账号」页(channel_store_account) 与「数据初始化」页(data_init)，
      使渠道能自助管理旗下门店账号、导入本渠道门店清单，且门店数据隔离在自身范围内。
    幂等：仅追加缺失页面、移除越权页面。
    """
    ensure = ["dashboard", "channel_overview", "channel_store",
              "channel_store_account", "data_init"]
    remove = ["user_manage"]
    with _tx() as conn:
        c = conn.cursor()
        for rid, code, pages in c.execute("SELECT id, code, pages FROM roles"):
            if code != "channel":
                continue
            try:
                plist = json.loads(pages) if pages else []
            except Exception:
                plist = []
            if "*" in plist:
                continue
            changed = False
            for rk in remove:
                if rk in plist:
                    plist.remove(rk)
                    changed = True
            for ak in ensure:
                if ak not in plist:
                    plist.append(ak)
                    changed = True
            if changed:
                c.execute("UPDATE roles SET pages=? WHERE id=?",
                          (json.dumps(plist, ensure_ascii=False), rid))


# ==================== 迁移台账（一次性回填治理） ====================
# 「角色页面」类迁移属于历史库补齐（把旧库的角色页面集补到默认集），
# 若每次 init_db 都重跑，会强制把管理员在界面上取消的页面又加回来，覆盖管理员的配置。
# 用 _migrations 台账记录已执行的「一次性」迁移，保证只跑一次。

def _ensure_migration_ledger():
    """创建迁移台账表（与当前所连库一致，租户库各自记账）。"""
    with _tx() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations ("
            "name TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now','localtime')))"
        )


def _migration_applied(name: str) -> bool:
    with _tx() as conn:
        row = conn.execute("SELECT 1 FROM _migrations WHERE name=?", (name,)).fetchone()
    return row is not None


def _mark_migration(name: str):
    with _tx() as conn:
        conn.execute("INSERT OR IGNORE INTO _migrations(name) VALUES (?)", (name,))


def _run_once(name: str, fn):
    """仅执行一次迁移：已记入台账则跳过，否则执行并标记。"""
    if _migration_applied(name):
        return
    fn()
    _mark_migration(name)


def _add_column_if_missing(table: str, column: str, ddl: str):
    """幂等地为表补列（SQLite 无 ADD COLUMN IF NOT EXISTS，需手动探测）。"""
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute(f"PRAGMA table_info({table})")
        cols = {r[1] for r in c.fetchall()}
        if column not in cols:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                conn.commit()
                logger.info("[迁移] %s 补列 %s 完成", table, column)
            except Exception as e:
                logger.warning("[迁移] %s 补列 %s 失败: %s", table, column, e)
    finally:
        conn.close()


def _migrate_dish_bom_fields():
    """
    为 dishes 补 BOM（配方）相关字段。

    - norm_name：归一化名称，用于去重判定
    - bom_status：pending（未生成）/ generated（已生成）/ manual（手工维护）
    - bom_source：llm / import / manual
    - bom_generated_at：生成时间
    - unit_cost：单份食材成本（冗余，便于列表展示与排序）

    注意：存量菜品统一置为 pending，使其进入「大模型重新生成配方」队列。
    """
    _add_column_if_missing("dishes", "norm_name", "norm_name TEXT DEFAULT ''")
    _add_column_if_missing("dishes", "bom_status", "bom_status TEXT NOT NULL DEFAULT 'pending'")
    _add_column_if_missing("dishes", "bom_source", "bom_source TEXT DEFAULT ''")
    _add_column_if_missing("dishes", "bom_generated_at", "bom_generated_at TEXT")
    _add_column_if_missing("dishes", "unit_cost", "unit_cost REAL DEFAULT 0")

    with _tx() as conn:
        c = conn.cursor()
        # 回填归一化名称（仅处理空值，避免覆盖已有）
        c.execute("SELECT id, name, norm_name FROM dishes")
        rows = c.fetchall()
        for did, name, norm in rows:
            nn = normalize_name(name)
            if not norm or norm != nn:
                c.execute("UPDATE dishes SET norm_name = ? WHERE id = ?", (nn, did))
        # 存量一律重新生成：重置为 pending，保留原来源便于追溯
        c.execute("UPDATE dishes SET bom_status = 'pending' WHERE bom_status IS NULL OR bom_status = ''")
        c.execute("UPDATE dishes SET bom_source = COALESCE(NULLIF(bom_source, ''), 'import')")


def _migrate_recipes_ingredient_id():
    """
    为 recipes 补 ingredient_id 引用列，并回填到 ingredients 主数据。

    幂等且可重入：重复执行只会补齐缺失的引用，不会重复建食材。
    """
    _add_column_if_missing("recipes", "ingredient_id", "ingredient_id INTEGER")

    # 全程复用单个连接：避免持有事务时再开连接导致 database is locked，同时避免逐行连接的性能问题
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("""
            SELECT r.id, d.store_id, r.ingredient_name, r.unit, r.unit_price, r.category, r.ingredient_id
            FROM recipes r JOIN dishes d ON r.dish_id = d.id
        """)
        rows = c.fetchall()

        # 1) 按 (门店, 归一化名) 去重，批量建食材（仅建缺失的，绝不覆盖已有价）
        ing_keys = {}
        for rid, store_id, raw_name, unit, price, category, existing_id in rows:
            raw = str(raw_name or "").strip()
            if not raw:
                continue
            key = (store_id, normalize_name(raw))
            ing_keys.setdefault(key, (raw, unit or "kg", float(price or 0), category or ""))

        # 已存在的 (门店, 名) 不重复插入，只插缺失项
        c.execute("SELECT store_id, name FROM ingredients")
        existing_keys = {(r[0], r[1]) for r in c.fetchall()}
        new_keys = {k: v for k, v in ing_keys.items() if k not in existing_keys}

        created = 0
        if new_keys:
            c.executemany(
                "INSERT OR IGNORE INTO ingredients (store_id, name, display_name, category, base_unit, unit_price,"
                " price_updated_at, source, status) VALUES (?, ?, ?, ?, ?, ?, datetime('now','localtime'), 'import', 'active')",
                [(sid, nm, disp, cat, bu, pr) for (sid, nm), (disp, bu, pr, cat) in new_keys.items()],
            )
            conn.commit()
            created = len(new_keys)

        # 2) 取回 (门店, 名) → id 映射
        c.execute("SELECT id, store_id, name FROM ingredients")
        id_map = {(r[1], r[2]): r[0] for r in c.fetchall()}

        # 3) 仅回填 ingredient_id 仍为空的配方行（已关联的跳过，避免无谓写库）
        pairs = []
        for rid, store_id, raw_name, unit, price, category, existing_id in rows:
            if existing_id is not None:
                continue
            raw = str(raw_name or "").strip()
            if not raw:
                continue
            iid = id_map.get((store_id, normalize_name(raw)))
            if iid:
                pairs.append((iid, rid))
        updated = 0
        if pairs:
            c.executemany("UPDATE recipes SET ingredient_id = ? WHERE id = ?", pairs)
            conn.commit()
            updated = len(pairs)

        # 仅在真正发生变更时输出 INFO；无变更则降级为 DEBUG，避免每次启动刷屏
        if created or updated:
            logger.info(
                "[迁移] recipes.ingredient_id 回填完成：新建食材 %d 种，回填配方 %d 行",
                created, updated,
            )
        else:
            logger.debug(
                "[迁移] recipes.ingredient_id 已是最新，无需回填（食材 %d 种，配方 %d 行已关联）",
                len(ing_keys), len(rows),
            )
    finally:
        conn.close()


def init_roles():
    """
    初始化默认角色。只在 roles 表为空时执行一次。
    角色用稳定编码（code）做逻辑判断，name 仅展示可改。
    """
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM roles")
        if c.fetchone()[0] == 0:
            c.execute(
                "INSERT INTO roles (code, name, description, pages) VALUES (?, ?, ?, ?)",
                ("admin", "系统管理员", "系统管理员", '["*"]'),
            )
            c.execute(
                "INSERT INTO roles (code, name, description, pages) VALUES (?, ?, ?, ?)",
                ("merchant", "商家用户", "商家用户", json.dumps(DEFAULT_MERCHANT_PAGES, ensure_ascii=False)),
            )
            c.execute(
                "INSERT INTO roles (code, name, description, pages) VALUES (?, ?, ?, ?)",
                ("channel", "渠道商", "渠道商：录入旗下门店并配置食材供货范围、管理旗下门店账号（可写）",
                 json.dumps(["dashboard", "channel_overview", "channel_store",
                             "channel_store_account", "data_init"], ensure_ascii=False)),
            )


def get_role_by_code(code: str) -> dict | None:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id, code, name, description, pages FROM roles WHERE code = ?", (code,))
        row = c.fetchone()
    if row:
        return {"id": row[0], "code": row[1], "name": row[2], "description": row[3],
                "pages": json.loads(row[4]) if row[4] else []}
    return None


def get_role_by_name(name: str) -> dict | None:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id, code, name, description, pages FROM roles WHERE name = ?", (name,))
        row = c.fetchone()
    if row:
        return {"id": row[0], "code": row[1], "name": row[2], "description": row[3],
                "pages": json.loads(row[4]) if row[4] else []}
    return None


def is_admin_role(role_code: str) -> bool:
    """判断某角色是否为管理员：编码为 admin（稳定，不随展示名变化）。"""
    return role_code == "admin"


def can_access_page(role_code: str, page_key: str) -> bool:
    """判断角色是否能访问某页面键：pages 含 '*' 表示全部页面。

    兼容别名：原「采购单生成」(purchase) 已合并进「食材采购」(ingredient)，
    保留 purchase→ingredient 的权限继承，避免历史角色失权。
    """
    if page_key == "ingredient" and role_code:
        # 历史拥有 purchase 权限的角色，视为同时拥有合并后的 ingredient 权限
        if _role_has_page(role_code, "purchase"):
            return True
    role = get_role_by_code(role_code)
    if not role:
        return False
    pages = role.get("pages") or []
    if "*" in pages:
        return True
    return page_key in pages


def _role_has_page(role_code: str, page_key: str) -> bool:
    """内部：判断某角色 pages 是否含指定页面键（不处理 '*' 短路，仅供别名兼容）。"""
    role = get_role_by_code(role_code)
    if not role:
        return False
    pages = role.get("pages") or []
    return page_key in pages


def get_all_roles():
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id, code, name, description, pages, created_at FROM roles ORDER BY id")
        rows = c.fetchall()
    return [{"id": r[0], "code": r[1], "name": r[2], "description": r[3],
             "pages": json.loads(r[4]) if r[4] else [], "created_at": r[5]} for r in rows]


def get_role_by_id(role_id: int) -> dict | None:
    with _tx() as conn:
        c = conn.cursor()
        c.execute("SELECT id, code, name, description, pages FROM roles WHERE id = ?", (role_id,))
        row = c.fetchone()
    if row:
        return {"id": row[0], "code": row[1], "name": row[2], "description": row[3],
                "pages": json.loads(row[4]) if row[4] else []}
    return None


def add_role(code: str, name: str, description: str = "", pages: list = None) -> bool:
    """新增角色（code 必填且唯一，用于逻辑判断）。"""
    try:
        with _tx() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO roles (code, name, description, pages) VALUES (?, ?, ?, ?)",
                (code, name, description, json.dumps(pages or [], ensure_ascii=False)),
            )
        return True
    except Exception:
        return False


def update_role(role_id: int, code: str = None, name: str = None, description: str = None, pages: list = None) -> bool:
    """修改角色；code/name 唯一性冲突返回 False；code 变更时同步 users.role。"""
    conn = get_conn()
    try:
        c = conn.cursor()

        if code is not None:
            c.execute("SELECT id, code FROM roles WHERE id = ?", (role_id,))
            row = c.fetchone()
            old_code = row[1] if row else None
            c.execute("SELECT id FROM roles WHERE code = ? AND id != ?", (code, role_id))
            if c.fetchone():
                return False
            c.execute("UPDATE roles SET code = ? WHERE id = ?", (code, role_id))
            if old_code and old_code != code:
                c.execute("UPDATE users SET role = ? WHERE role = ?", (code, old_code))

        if name is not None:
            c.execute("UPDATE roles SET name = ? WHERE id = ?", (name, role_id))
        if description is not None:
            c.execute("UPDATE roles SET description = ? WHERE id = ?", (description, role_id))
        if pages is not None:
            c.execute("UPDATE roles SET pages = ? WHERE id = ?", (json.dumps(pages, ensure_ascii=False), role_id))
        conn.commit()
        return True
    finally:
        conn.close()


def delete_role(role_id: int) -> bool:
    conn = get_conn()
    try:
        c = conn.cursor()
        c.execute("SELECT code FROM roles WHERE id = ?", (role_id,))
        row = c.fetchone()
        if not row:
            return False
        role_code = row[0]
        c.execute("SELECT COUNT(*) FROM users WHERE role = ?", (role_code,))
        if c.fetchone()[0] > 0:
            return False
        c.execute("DELETE FROM roles WHERE id = ?", (role_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def get_stores_for_user(active_only=False, user=None):
    """按用户权限返回门店列表"""
    all_stores = get_all_stores(active_only=active_only)
    if not user:
        return all_stores
    role = user.get("role", "")
    if is_admin_role(role):
        return all_stores
    allowed_ids = set(get_user_store_ids(user.get("id", 0)))
    if not allowed_ids:
        return []
    return [s for s in all_stores if s["id"] in allowed_ids]

def get_latest_sales_history_qty(store_id: int, dish_id: int) -> float:
    """从 sales_history 取某菜品最近一次销量"""
    with _tx() as conn:
        c = conn.cursor()
        c.execute("""
            SELECT qty FROM sales_history
            WHERE store_id = ? AND dish_id = ?
            ORDER BY sale_date DESC, id DESC LIMIT 1
        """, (store_id, dish_id))
        row = c.fetchone()
    return float(row[0]) if row else 0.0

