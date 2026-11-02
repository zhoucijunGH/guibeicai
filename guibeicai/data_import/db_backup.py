"""桂备菜 - 数据库热备（月备，保留 3 个月）

- 在线热备：先 wal_checkpoint 落盘，再用 sqlite3 的 connection.backup() 生成
  一致性快照（不阻塞读写），gzip 压缩落盘。
- 产物按月命名：backups/db/guibeicai_db_YYYYMM.sqlite.gz（同月多次运行覆盖，天然「每月 1 份」）。
- 保留策略：仅留最近 keep_months 个自然月文件（默认 3）；另按 mtime > 90 天兜底清理。
- 默认仅备 SQLite 库（最敏感且不可重建）。config 打包见 include_config 开关（含 .env 密钥，谨慎）。

供 _scheduler 月备 cron 与 scripts/backup_db.py 独立脚本复用。
"""
import os
import gzip
import shutil
import sqlite3
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("guibeicai.db_backup")


def _db_path() -> Path:
    from guibeicai.web import _db
    return _db.DB_PATH


def _backups_dir() -> Path:
    base = Path(__file__).resolve().parent.parent.parent  # guibeicai/
    d = base / "backups" / "db"
    d.mkdir(parents=True, exist_ok=True)
    return d


def backup_database(include_config: bool = False) -> str:
    """在线热备数据库（可选打包 config），返回产物路径。失败抛异常。"""
    src = _db_path()
    if not src.exists():
        raise FileNotFoundError(f"数据库文件不存在：{src}")

    out_dir = _backups_dir()
    stamp = datetime.now().strftime("%Y%m")  # 按月，覆盖式
    gz_path = out_dir / f"guibeicai_db_{stamp}.sqlite.gz"

    tmp_sqlite = out_dir / f".tmp_guibeicai_db_{stamp}.sqlite"
    try:
        # 1) 在线热备到临时 sqlite（先 checkpoint 让 WAL 落盘，再 backup）
        src_con = sqlite3.connect(str(src))
        try:
            src_con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            dst_con = sqlite3.connect(str(tmp_sqlite))
            try:
                src_con.backup(dst_con)
            finally:
                dst_con.close()
        finally:
            src_con.close()

        # 2) gzip 压缩
        with open(tmp_sqlite, "rb") as f_in, gzip.open(gz_path, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out)

        # 3) 可选：打包 config/（含 .env 密钥，权限收紧）
        if include_config:
            cfg_dir = src.parent.parent / "config"
            if cfg_dir.exists():
                tar_path = out_dir / f"guibeicai_config_{stamp}.tar.gz"
                import tarfile
                with tarfile.open(tar_path, "w:gz") as tar:
                    for p in cfg_dir.iterdir():
                        if p.is_file():
                            tar.add(str(p), arcname=p.name)
                try:
                    os.chmod(tar_path, 0o600)
                except Exception:
                    pass

        # 4) 收紧备份文件权限
        try:
            os.chmod(gz_path, 0o600)
        except Exception:
            pass

        logger.info("[备份] 数据库已热备：%s", gz_path)
        return str(gz_path)
    finally:
        if tmp_sqlite.exists():
            try:
                os.remove(tmp_sqlite)
            except Exception:
                pass


def prune_backups(keep_months: int = 3, max_age_days: int = 90) -> int:
    """清理过期备份：保留最近 keep_months 个自然月 + 删除 mtime 超 max_age_days 的文件。返回删除数。"""
    out_dir = _backups_dir()
    now = datetime.now()
    # 允许的月份前缀，如 ["202609","202608","202607"]
    keep_prefixes = []
    y, m = now.year, now.month
    for _ in range(max(1, keep_months)):
        keep_prefixes.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1

    removed = 0
    cutoff = now.timestamp() - max_age_days * 86400
    for f in out_dir.glob("guibeicai_db_*.sqlite.gz"):
        name = f.name
        month_prefix = name[len("guibeicai_db_"):len("guibeicai_db_") + 6]  # YYYYMM
        keep = month_prefix in keep_prefixes
        # 兜底：mtime 超龄也删（即便落在保留月内，异常情况）
        if not keep or f.stat().st_mtime < cutoff:
            try:
                f.unlink()
                removed += 1
                logger.info("[备份清理] 删除过期备份：%s", f)
            except Exception as e:
                logger.warning("[备份清理] 删除失败 %s: %s", f, e)
    return removed


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    p = backup_database()
    n = prune_backups()
    print(f"备份完成：{p}，清理过期 {n} 个")
