"""桂备菜 - 数据库独立备份脚本（供系统 cron 调用）

用法：
  python scripts/backup_db.py                 # 月备 DB（gzip），自动清理过期
  python scripts/backup_db.py --with-config   # 额外打包 config/（含 .env 密钥，权限 600）

crontab 示例（每月 1 日 03:00）：
  0 3 1 * * cd /path/to/guibeicai && python scripts/backup_db.py >> logs/backup.log 2>&1
"""
import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")

from guibeicai.data_import.db_backup import backup_database, prune_backups  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="桂备菜数据库热备")
    parser.add_argument("--with-config", action="store_true", help="额外打包 config/（含 .env 密钥）")
    args = parser.parse_args()
    path = backup_database(include_config=args.with_config)
    removed = prune_backups()
    logging.getLogger("backup_db").info("完成：%s，清理过期 %d 个", path, removed)


if __name__ == "__main__":
    main()
