"""
桂备菜 - 企业微信机器人通知渠道
通过 Webhook 发送 Markdown 消息
"""

import json
import logging
from typing import Dict, List

import requests

from .base import BaseNotifier

logger = logging.getLogger("GuiBeiCai-Scheduler")


class WeComNotifier(BaseNotifier):
    """企业微信机器人 Webhook 推送"""

    def __init__(self, config: Dict):
        super().__init__(config)
        webhook = config.get("webhook_url", "")
        if webhook.startswith("${") and webhook.endswith("}"):
            import os
            env_var = webhook[2:-1]
            self.webhook_url = os.getenv(env_var, "")
        else:
            self.webhook_url = webhook

    def send(
        self,
        subject: str,
        content: str,
        recipients: List[str] = None,
        **kwargs,
    ) -> bool:
        """
        发送企业微信 Markdown 消息
        
        注意：企业微信机器人不支持指定收件人，消息发到群聊
        """
        if not self.enabled:
            logger.debug("企业微信渠道未启用，跳过")
            return False

        if not self.webhook_url:
            logger.error("企业微信 Webhook URL 未配置")
            return False

        # 企业微信 Markdown 有长度限制（4096字符），超长需截断
        MAX_LEN = 4000
        if len(content) > MAX_LEN:
            content = content[:MAX_LEN] + "\n\n... (内容过长已截断)"

        payload = {
            "msgtype": "markdown",
            "markdown": {
                "content": f"## {subject}\n\n{content}",
            },
        }

        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            result = response.json()

            if result.get("errcode") == 0:
                logger.info("✅ 企业微信消息发送成功")
                return True
            else:
                logger.error(f"❌ 企业微信发送失败: {result}")
                return False

        except Exception as e:
            logger.error(f"❌ 企业微信请求异常: {e}")
            return False