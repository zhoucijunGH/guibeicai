"""
桂备菜 - 管理员告警通知
连续失败达到阈值时，通知系统管理员
"""

import logging
import os
from typing import Dict, List

from .email_notifier import EmailNotifier
from .wecom_notifier import WeComNotifier
from guibeicai.config.settings import Settings

logger = logging.getLogger("GuiBeiCai-Scheduler")


class AlertNotifier:
    """
    管理员告警通知器
    支持多渠道同时发送
    """

    def __init__(self, config: Dict):
        self.email_cfg = config.get("email", {})
        self.wecom_cfg = config.get("wecom_webhook", "")
        self.admin_email = config.get("admin_email", "")
        self._settings = Settings()

    def send_alert(
        self,
        store_name: str,
        failure_count: int,
        error_msg: str,
        target_date: str,
    ) -> None:
        """发送告警"""
        subject = f"🚨 桂备菜告警：{store_name} 连续{failure_count}次预测失败"

        content = f"""
        <h3>桂备菜系统告警</h3>
        <p><b>门店：</b>{store_name}</p>
        <p><b>目标日期：</b>{target_date}</p>
        <p><b>连续失败次数：</b>{failure_count}</p>
        <p><b>错误信息：</b></p>
        <pre style="background:#f5f5f5;padding:10px;border-radius:4px;">{error_msg}</pre>
        <p><small>请检查数据源、API Key配置及网络连接</small></p>
        """

        # 邮件告警
        if self.admin_email:
            # 从统一配置读取真实密码（避免写死字面量导致发不出邮件）
            email_cfg = dict(self._settings.email_config)
            email_cfg["username"] = self.admin_email
            email_cfg["from_address"] = self.admin_email
            email_notifier = EmailNotifier(email_cfg)
            email_notifier.send(
                subject=subject,
                content=content,
                recipients=[self.admin_email],
            )

        # 企业微信告警
        if self.wecom_cfg:
            wecom = WeComNotifier({"enabled": True, "webhook_url": self.wecom_cfg})
            wecom.send(subject=subject, content=content)

        logger.warning(f"🚨 管理员告警已发送: {store_name} × {failure_count}次失败")