"""桂备菜 - 多租户托管：租户注册与开通（provisioning）

## 托管模型

**单进程共享实例 + 每租户独立 SQLite 文件**

| 关注点 | 做法 |
|---|---|
| 数据隔离 | 一家客户 = 一个 `<tenant_id>/guibeicai.db`，物理隔离，天然不串数据 |
| 运行时路由 | `_db.set_tenant()` 写入 ContextVar，`get_conn()` 自动连对应库，**业务代码零改动** |
| 成本 | 共享进程，内存不随租户数线性增长（每租户起进程则需 N 倍内存） |
| 运维 | 备份/迁移/退租按客户目录一键完成；故障不跨租户扩散 |

## 为什么必须有"主控注册表"

登录时我们只知道用户名，**还不知道该连哪个库**——这就是先有鸡还是先有蛋的问题。
所以另设一个**注册表库**（始终是默认库 `data/guibeicai.db`），只存 `username → tenant_id`
的映射；登录先查注册表拿到 tenant_id，再切到租户库做真正的鉴权。

注册表也向后兼容：**查不到的用户名按老逻辑走（无量租户的单机部署）**，现有演示库与
开发流程完全不受影响。

## 用法

    from guibeicai.web import _tenancy
    _tenancy.create_tenant("nanning-laoyou", "南宁老友粉（望州店）", "望州店", "admin", "初始密码")

    tid = _tenancy.resolve_tenant_for_username("admin")   # -> "nanning-laoyou"
"""

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from guibeicai.web import _db

logger = logging.getLogger(__name__)

# 注册表状态取值
STATUS_ACTIVE = "active"
STATUS_SUSPENDED = "suspended"      # 欠费/冻结：禁止登录，数据保留
STATUS_CLOSED = "closed"            # 已退租


# ---------------- 注册表（永远指向默认库，不受租户上下文影响） ----------------

def _registry_conn():
    """连接主控注册表库。

    注意：这里**刻意绕过 get_conn()**，因为注册表必须落在固定位置。
    否则一旦处于某个租户上下文中，注册表表结构会被建进租户库里。
    """
    _db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_db.DB_PATH), timeout=30)
    for pragma in _db._SQLITE_PRAGMAS:
        try:
            conn.execute(pragma)
        except sqlite3.Error as e:
            logger.warning("[tenancy] 注册表 PRAGMA 失败 %s: %s", pragma, e)
    return conn


def ensure_registry():
    """创建注册表表结构（幂等，可重复调用）。

    - tenants：租户主记录（id / 名称 / 状态 / 开通时间）
    - tenant_accounts：登录名 → 租户 的路由索引（一个租户可有多个账号）
    """
    conn = _registry_conn()
    try:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS tenants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                remark TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS tenant_accounts (
                username TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_tenant_accounts_tenant "
                  "ON tenant_accounts(tenant_id)")
        conn.commit()
    finally:
        conn.close()


def tenant_exists(tenant_id: str) -> bool:
    """该租户是否已在注册表中（不做 tenant_id 合法性校验前的归一化）。"""
    tid = _db.normalize_tenant_id(tenant_id)
    conn = _registry_conn()
    try:
        row = conn.execute("SELECT 1 FROM tenants WHERE tenant_id = ?", (tid,)).fetchone()
        return row is not None
    finally:
        conn.close()


def list_tenants() -> list:
    """列出全部租户（含库文件路径与文件大小，便于运维核对）。"""
    ensure_registry()
    conn = _registry_conn()
    try:
        rows = conn.execute(
            "SELECT tenant_id, name, status, remark, created_at FROM tenants "
            "ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    result = []
    for tid, name, status, remark, created_at in rows:
        db_file = _db.tenant_db_path(tid)
        size_mb = round(db_file.stat().st_size / 1024 / 1024, 2) if db_file.exists() else None
        result.append({
            "tenant_id": tid,
            "name": name,
            "status": status,
            "remark": remark,
            "created_at": created_at,
            "db_path": str(db_file),
            "db_size_mb": size_mb,
        })
    return result


def resolve_tenant_for_username(username: str) -> str | None:
    """登录路由：用户名 → 租户 id。

    返回 None 表示该账号不属于任何租户 —— 按老的单机/管理员路径处理（向后兼容）。
    已冻结(suspended)/已退租(closed)的租户返回 None 之外的值为特殊标记 ""，调用方据此拒绝登录。
    """
    if not username:
        return None
    ensure_registry()
    conn = _registry_conn()
    try:
        row = conn.execute(
            "SELECT a.tenant_id, t.status FROM tenant_accounts a "
            "JOIN tenants t ON t.tenant_id = a.tenant_id WHERE a.username = ?",
            (str(username).strip(),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    tid, status = row
    # 冻结/退租：返回空串，语义为"属于某租户但当前不可用"
    return "" if status != STATUS_ACTIVE else tid


def set_tenant_status(tenant_id: str, status: str) -> bool:
    """变更租户状态（active / suspended / closed）。拒绝非法状态值。"""
    if status not in (STATUS_ACTIVE, STATUS_SUSPENDED, STATUS_CLOSED):
        raise ValueError("状态必须是 active / suspended / closed")
    tid = _db.normalize_tenant_id(tenant_id)
    if not tenant_exists(tid):
        raise ValueError(f"租户不存在：{tid}")
    conn = _registry_conn()
    try:
        conn.execute("UPDATE tenants SET status = ? WHERE tenant_id = ?", (status, tid))
        conn.commit()
    finally:
        conn.close()
    logger.info("[tenancy] 租户 %s 状态改为 %s", tid, status)
    return True


# ---------------- 开通（provisioning） ----------------

def create_tenant(tenant_id: str, tenant_name: str, store_name: str,
                  admin_username: str, admin_password: str,
                  entry_role: str = "admin",
                  city: str = "", province: str = "",
                  valid_until: str = None, remark: str = "") -> dict:
    """开通一个新租户：建库 → 建 schema → 建门店 → 建入口账号 → 写入注册表。

    这是"10 分钟内给新客户开出一个可用环境"的那一步（GTM 计划第五章 W2 的技术解卡项）。

    幂等性：租户 id 已存在则直接抛错，不会覆盖已有客户数据（防止误操作清库）。

    Args:
        tenant_id:     租户唯一标识（小写字母/数字/-/_，3–63 位），建议用客户拼音缩写，如 nanning-laoyou
        tenant_name:   客户名称（对外展示）
        store_name:    首店名称
        admin_username: 该租户入口账号登录名
        admin_password: 初始密码（建议强制首次登录改密）
        entry_role:    入口账号角色**编码**（admin / channel / merchant），按签约场景选：
                          - 直签门店(独立/连锁)：用 "admin"（租户管理员，可自建门店账号）或 "merchant"（单店老板）
                          - 渠道签约：用 "channel"（渠道商，可登记门店并自助建门店账号）
                          - 平台演示/代管：用 "admin"
        valid_until:   账号有效期 YYYY-MM-DD（对齐合同期限，None 表示长期）
        remark:        备注

    Returns:
        dict: {tenant_id, db_path, store_id, admin_username, entry_role, status}
    """
    tid = _db.normalize_tenant_id(tenant_id)
    ensure_registry()
    if tenant_exists(tid):
        raise ValueError(f"租户已存在，拒绝重建（防止覆盖客户数据）：{tid}")

    _fsck = None
    try:
        # 关键：进入租户上下文后，后续所有 _db 调用（含 init_db）都作用于该租户的库
        _db.set_tenant(tid)
        try:
            _db.init_db()                       # 建全部表结构 + 迁移 + 种子角色
            store_id = _db.upsert_store(name=store_name, city=city, province=province)
            # entry_role 必须是角色编码（admin/channel/merchant）：users.role 存编码，
            # can_access_page 按编码匹配；传名称会导致该账号匹配不到任何角色、进不了页面。
            if _db.get_role_by_code(entry_role) is None:
                raise ValueError(f"非法角色编码：{entry_role}（可选：admin / channel / merchant）")
            ok = _db.add_user(
                username=admin_username,
                password=admin_password,
                role=entry_role,
                store_ids=[store_id],
                valid_until=valid_until,
            )
            if not ok:
                raise ValueError(f"创建入口账号失败（用户名可能重复）：{admin_username}")
        finally:
            _db.set_tenant(None)                # 无论成败都要退出租户上下文

        # 写入注册表（成功后再登记，避免租户库建一半却对外可登录）
        conn = _registry_conn()
        try:
            conn.execute(
                "INSERT INTO tenants (tenant_id, name, status, remark) VALUES (?, ?, ?, ?)",
                (tid, tenant_name, STATUS_ACTIVE, remark),
            )
            conn.execute(
                "INSERT OR IGNORE INTO tenant_accounts (username, tenant_id) VALUES (?, ?)",
                (str(admin_username).strip(), tid),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("[tenancy] 开通租户失败：%s", tid)
        raise

    db_file = _db.tenant_db_path(tid)
    logger.info("[tenancy] 已开通租户 %s（%s）store_id=%s", tid, tenant_name, store_id)
    return {
        "tenant_id": tid,
        "name": tenant_name,
        "db_path": str(db_file),
        "store_id": store_id,
        "admin_username": admin_username,
        "entry_role": entry_role,
        "valid_until": valid_until,
        "status": STATUS_ACTIVE,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def register_extra_account(tenant_id: str, username: str) -> None:
    """把租户内已存在的账号补登记进登录路由表（该账号需已在租户库中建好）。

    幂等（INSERT OR IGNORE）。tenant 尚未登记时静默跳过——create_tenant 在 add_user
    自动登记阶段可能先建库、后登记 tenants 主记录，属合法时序，不应报错；
    随后 create_tenant 会补登 tenants 与 tenant_accounts。
    """
    tid = _db.normalize_tenant_id(tenant_id)
    conn = _registry_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO tenant_accounts (username, tenant_id) VALUES (?, ?)",
            (str(username).strip(), tid),
        )
        conn.commit()
    finally:
        conn.close()


def delete_tenant(tenant_id: str, drop_files: bool = False) -> dict:
    """退租处理：默认只冻结并摘除登录路由（**保留数据**），drop_files=True 才物理删库。

    默认保留数据是刻意的：删库不可恢复，且退租客户常有"回来对账/要数据"的需求。
    """
    tid = _db.normalize_tenant_id(tenant_id)
    if not tenant_exists(tid):
        raise ValueError(f"租户不存在：{tid}")

    set_tenant_status(tid, STATUS_CLOSED)
    conn = _registry_conn()
    try:
        conn.execute("DELETE FROM tenant_accounts WHERE tenant_id = ?", (tid,))
        conn.commit()
    finally:
        conn.close()

    removed = False
    if drop_files:
        db_file = _db.tenant_db_path(tid)
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(db_file) + suffix)
            if p.exists():
                p.unlink()
                removed = True
        parent = db_file.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()

    logger.info("[tenancy] 已关闭租户 %s（drop_files=%s）", tid, drop_files)
    return {"tenant_id": tid, "status": STATUS_CLOSED, "files_removed": removed}
