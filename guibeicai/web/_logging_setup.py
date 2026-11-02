"""桂备菜 - 统一日志配置（12-factor 友好）

幂等 init_logging()：
- 文件 handler：按天轮转（midnight）+ gzip 压缩，保留 LOG_RETENTION_DAYS 天（默认 30）；
- 控制台 handler：保留 stdout 输出（容器/宝塔可收集）；
- 反复调用（Streamlit 页面 rerun）不会重复叠加 handler。

环境变量：
- LOG_DIR：日志目录（默认 <BASE>/logs）
- LOG_LEVEL：日志级别（默认 INFO）
- LOG_RETENTION_DAYS：保留天数（默认 30）
"""
import os
import sys
import gzip
import shutil
import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_INITIALIZED = False

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s - %(message)s"


def _make_gzip_rotator():
    def rotator(source, dest):
        try:
            with open(source, "rb") as f_in, gzip.open(dest, "wb") as f_out:
                shutil.copyfileobj(f_in, f_out)
            os.remove(source)
        except Exception:
            # 压缩失败则退化为直接重命名，避免丢日志
            try:
                if os.path.exists(source):
                    os.replace(source, dest[:-3] if dest.endswith(".gz") else dest)
            except Exception:
                pass
    return rotator


def _gzip_namer(name: str) -> str:
    return name + ".gz"


def init_logging() -> None:
    """幂等初始化根日志：文件(按天轮转+gzip,保留N天) + 控制台。"""
    global _INITIALIZED
    if _INITIALIZED:
        return

    log_dir = Path(os.getenv("LOG_DIR", "")) if os.getenv("LOG_DIR") else (
        Path(__file__).resolve().parent.parent.parent / "logs"
    )
    log_dir.mkdir(parents=True, exist_ok=True)

    level_name = (os.getenv("LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    retention = max(1, int(os.getenv("LOG_RETENTION_DAYS", "30")))

    root = logging.getLogger()
    root.setLevel(level)

    # 防重复：清掉可能存在的默认 basicConfig StreamHandler（仅 unnamed 的）
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = logging.Formatter(_LOG_FORMAT)

    # 文件 handler：按天轮转 + gzip 压缩
    file_handler = TimedRotatingFileHandler(
        filename=str(log_dir / "app.log"),
        when="midnight",
        interval=1,
        backupCount=retention,
        encoding="utf-8",
        utc=False,
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(level)
    file_handler.rotator = _make_gzip_rotator()
    file_handler.namer = _gzip_namer
    root.addHandler(file_handler)

    # 控制台 handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    console_handler.setLevel(level)
    root.addHandler(console_handler)

    _INITIALIZED = True
    logging.getLogger("guibeicai.logging").info(
        "日志已初始化：目录=%s 级别=%s 保留=%d天(压缩)", log_dir, level_name, retention
    )
