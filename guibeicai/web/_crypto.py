"""桂备菜 - 凭据加密工具（S3 安全加固）

用于 POS/收银数据源的认证凭据（api_key / token / basic 密码等）在数据库中的
加密存储与读取脱敏，避免明文落库、避免 API/页面回显明文。

实现：使用 cryptography.fernet（AES-128-CBC + HMAC-SHA256，标准库，经审计）。
密钥来源优先级：
  1. 环境变量 GUOBEICAI_SECRET_KEY（Fernet 密钥，base64，32 字节）—— 生产推荐，
     建议由外部密钥管理（如 K8s Secret / Vault）注入，不落盘。
  2. 密钥文件 data/.pos_secret_key（首次运行自动生成并持久化，权限 0600）——
     开发/单机部署零配置可用，重启后仍可解密历史凭据。
生产环境若未配置 GUOBEICAI_SECRET_KEY，会生成密钥文件并给出安全告警
（与数据库同盘，仍优于明文，但建议改用外部密钥注入）。
"""

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

ENC_PREFIX = "fernet::"
MASK_SENTINEL = "********"  # 脱敏占位符，同时作为「保留原值」的标记

# POS auth_config 中的敏感字段（这些字段在存储前加密、在回显时脱敏）
POS_SENSITIVE_KEYS = {"api_key", "token", "pass", "user"}


def _key_file_path() -> Path:
    db_env = os.getenv("SQLITE_PATH")
    if db_env:
        return Path(db_env).resolve().parent / ".pos_secret_key"
    return Path("data") / ".pos_secret_key"


def _load_key() -> bytes:
    env = (os.getenv("GUOBEICAI_SECRET_KEY") or "").strip()
    if env:
        try:
            Fernet(env.encode() if isinstance(env, str) else env)
            return env.encode() if isinstance(env, str) else env
        except Exception:
            logger.warning("GUOBEICAI_SECRET_KEY 不是合法 Fernet 密钥，回退到密钥文件/自动生成")

    key_file = _key_file_path()
    if key_file.exists():
        try:
            return key_file.read_text().strip().encode()
        except Exception as e:
            logger.warning("读取密钥文件失败 %s: %s", key_file, e)

    # 生成并持久化（开发/生产均可用；生产建议改用外部密钥注入）
    key = Fernet.generate_key()
    try:
        key_file.parent.mkdir(parents=True, exist_ok=True)
        key_file.write_text(key.decode())
        try:
            os.chmod(key_file, 0o600)
        except Exception:
            pass
        is_prod = os.getenv("GUOBEICAI_ENV", os.getenv("ENV", "")).lower() in ("production", "prod", "1")
        if is_prod:
            logger.warning(
                "安全告警：生产环境未配置 GUOBEICAI_SECRET_KEY，已自动生成密钥文件 %s。"
                "建议改用外部密钥管理注入，避免密钥与数据库同盘的风险。", key_file
            )
        else:
            logger.info("已生成 POS 凭据加密密钥文件 %s（请妥善备份，丢失将导致已存凭据不可解密）。", key_file)
    except Exception as e:
        logger.warning("密钥文件写入失败，使用本次进程内存密钥（重启后已存凭据不可解密）: %s", e)
    return key


_FERNET_CACHE = None


def _get_fernet() -> Fernet:
    global _FERNET_CACHE
    if _FERNET_CACHE is None:
        _FERNET_CACHE = Fernet(_load_key())
    return _FERNET_CACHE


def encrypt_secret(plaintext) -> str:
    """加密字符串。已是密文（带前缀）则原样返回（幂等）。空值返回空串。"""
    if plaintext is None:
        return ""
    pt = str(plaintext)
    if pt.startswith(ENC_PREFIX):
        return pt
    token = _get_fernet().encrypt(pt.encode("utf-8")).decode("ascii")
    return ENC_PREFIX + token


def decrypt_secret(value) -> str:
    """解密字符串。非密文（旧明文/None）原样返回，保证兼容迁移。"""
    if not value:
        return ""
    s = str(value)
    if s.startswith(ENC_PREFIX):
        try:
            return _get_fernet().decrypt(s[len(ENC_PREFIX):].encode("ascii")).decode("utf-8")
        except (InvalidToken, Exception) as e:
            logger.warning("凭据解密失败（可能密钥已变更或数据损坏）: %s", e)
            return ""
    return s


def mask_secret(value, visible: int = 4) -> str:
    """脱敏展示：仅保留末 visible 位，其余打码。"""
    if not value:
        return ""
    s = str(value)
    if len(s) <= visible:
        return "*" * len(s)
    return "*" * (len(s) - visible) + s[-visible:]
