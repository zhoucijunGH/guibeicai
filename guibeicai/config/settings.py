"""
桂备菜 - 统一配置管理
所有模块通过此文件获取配置，避免散落的 yaml 读取
"""

import os
from pathlib import Path
from typing import Dict, Any

import yaml
from dotenv import load_dotenv

# 加载 .env 文件（显式指定路径，确保从任意工作目录启动都能加载 guibeicai/.env）
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)
else:
    load_dotenv()  # 回退：从当前工作目录向上查找

# 项目根目录
BASE_DIR = Path(__file__).parent.parent.parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
LOGS_DIR = BASE_DIR / "logs"

# 确保目录存在
for d in [DATA_DIR, OUTPUT_DIR, LOGS_DIR]:
    d.mkdir(exist_ok=True)


class Settings:
    """全局配置单例"""

    def __init__(self):
        self._config = self._load_yaml("config/config.yaml")
        self._stores = self._load_yaml("config/stores.yaml")

    def _load_yaml(self, rel_path: str) -> Dict:
        path = BASE_DIR / rel_path
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    # ---- 系统配置 ----
    @property
    def timezone(self) -> str:
        return self._config.get("scheduler", {}).get("timezone", "Asia/Shanghai")

    @property
    def session_timeout_seconds(self) -> int:
        """登录会话超时（秒），12-factor：可由 SESSION_TIMEOUT_SECONDS 覆盖"""
        return int(os.getenv("SESSION_TIMEOUT_SECONDS", "28800"))

    @property
    def scheduler_hour(self) -> int:
        """每日定时预测执行小时（0-23），12-factor：可由 SCHEDULER_HOUR 覆盖"""
        return int(os.getenv("SCHEDULER_HOUR", "6"))

    @property
    def scheduler_minute(self) -> int:
        """每日定时预测执行分钟（0-59），12-factor：可由 SCHEDULER_MINUTE 覆盖"""
        return int(os.getenv("SCHEDULER_MINUTE", "0"))

    @property
    def max_consecutive_failures(self) -> int:
        return self._config.get("scheduler", {}).get("max_consecutive_failures", 3)

    @property
    def safety_factors(self) -> Dict:
        return self._config.get("purchase", {}).get(
            "safety_factor", {"perishable": 1.15, "non_perishable": 1.25}
        )

    # ---- API Keys（从环境变量） ----
    @property
    def siliconflow_api_key(self) -> str:
        """LLM 大模型密钥（实际生效变量名，_llm.py 读取此变量）。"""
        return os.getenv("SILICONFLOW_API_KEY", "")

    # ---- 密钥托管辅助（#15） ----
    def validate_secrets(self) -> list:
        """校验关键密钥是否已配置，返回缺失项列表（空列表表示齐全）。

        仅做存在性检查，绝不打印/记录密钥原文。
        """
        required = {
            "SILICONFLOW_API_KEY": self.siliconflow_api_key,
        }
        missing = [name for name, val in required.items() if not (val and val.strip())]
        # 天气预测当前使用 Open-Meteo 免费接口，不需要 WEATHER_API_KEY，故不再作为必需项校验。
        # 邮件通知启用时，密码才视为必需。
        if self.email_config.get("enabled") and not (self.email_config.get("password") or "").strip():
            missing.append("EMAIL_PASSWORD")
        return missing

    @staticmethod
    def mask_secret(value: str, keep: int = 4) -> str:
        """对密钥做脱敏展示，例如 'sk-abc1234567' -> 'sk-a****567'。"""
        if not value:
            return ""
        value = str(value)
        if len(value) <= keep:
            return "*" * len(value)
        return value[:keep] + "*" * max(1, len(value) - keep * 2) + value[-keep:]

    @property
    def email_config(self) -> Dict:
        return {
            # 邮件告警默认关闭，设置 EMAIL_ENABLED=true 并配置密码后启用，避免未启用时误报
            "enabled": os.getenv("EMAIL_ENABLED", "").strip().lower() in ("1", "true", "yes", "y"),
            "smtp_host": os.getenv("EMAIL_SMTP_HOST", "smtp.qq.com"),
            "smtp_port": int(os.getenv("EMAIL_SMTP_PORT", "587")),
            "username": os.getenv("EMAIL_USERNAME", ""),
            "password": os.getenv("EMAIL_PASSWORD", ""),
            "use_tls": True,
            "from_name": "桂备菜系统",
            "from_address": os.getenv("EMAIL_USERNAME", ""),
        }

    @property
    def wecom_webhook(self) -> str:
        return os.getenv("WECOM_WEBHOOK_URL", "")

    @property
    def admin_webhook(self) -> str:
        return os.getenv("ADMIN_WEBHOOK_URL", "")

    @property
    def admin_email(self) -> str:
        return os.getenv("EMAIL_USERNAME", "")

    # ---- 门店配置 ----
    @property
    def stores(self) -> list:
        return self._stores.get("stores", [])

    def get_store(self, store_name: str) -> Dict:
        for s in self.stores:
            if s["store_name"] == store_name:
                return s
        return {}

    # ---- 预测引擎配置 / 门店-城市映射：原 prediction、store_city_map 配置项已清理 ----
    # 说明：预测参数由 _llm.py 硬编码（模型名走 .env 的 LLM_MODEL）；天气城市映射改 _geo.py 动态解析。


# 全局单例
settings = Settings()