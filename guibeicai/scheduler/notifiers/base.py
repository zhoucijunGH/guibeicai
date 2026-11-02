"""
桂备菜 - 通知渠道抽象基类
所有推送渠道必须继承此类
"""

import logging
from abc import ABC, abstractmethod
from typing import Dict, List

logger = logging.getLogger("GuiBeiCai-Scheduler")


class BaseNotifier(ABC):
    """通知渠道基类"""

    def __init__(self, config: Dict):
        self.config = config
        self.enabled = config.get("enabled", False)

    @abstractmethod
    def send(
        self,
        subject: str,
        content: str,
        recipients: List[str],
        **kwargs,
    ) -> bool:
        """
        发送通知
        
        返回 True 表示成功，False 表示失败
        """
        pass

    def is_enabled(self) -> bool:
        return self.enabled

    def _mask_email(self, email: str) -> str:
        """脱敏邮箱地址用于日志"""
        if "@" not in email:
            return email
        local, domain = email.split("@", 1)
        if len(local) <= 2:
            return f"**@{domain}"
        return f"{local[:2]}***@{domain}"