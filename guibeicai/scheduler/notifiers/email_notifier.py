"""
桂备菜 - 邮件通知渠道
使用 smtplib 发送 HTML 格式采购清单
"""

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from email.utils import formataddr
from typing import Dict, List

from .base import BaseNotifier

logger = logging.getLogger("GuiBeiCai-Scheduler")


class EmailNotifier(BaseNotifier):
    """邮件推送（HTML格式）"""

    def __init__(self, config: Dict):
        super().__init__(config)
        # 密码从环境变量读取（支持 ${VAR_NAME} 语法）
        password = config.get("password", "")
        if password.startswith("${") and password.endswith("}"):
            env_var = password[2:-1]
            self.password = os.getenv(env_var, "")
        else:
            self.password = password

        self.smtp_host = config.get("smtp_host", "smtp.qq.com")
        self.smtp_port = config.get("smtp_port", 587)
        self.use_tls = config.get("use_tls", True)
        self.username = config.get("username", "")
        self.from_name = config.get("from_name", "桂备菜系统")
        self.from_address = config.get("from_address", self.username)

    def send(
        self,
        subject: str,
        content: str,
        recipients: List[str],
        **kwargs,
    ) -> bool:
        """
        发送HTML邮件
        
        参数:
            subject: 邮件主题
            content: HTML格式正文
            recipients: 收件人列表
        """
        if not self.enabled:
            logger.debug("邮件渠道未启用，跳过")
            return False

        if not self.password:
            logger.error("邮件密码未配置（请设置环境变量）")
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            # 中文显示名必须按 RFC2047 编码，否则 QQ/163 等 SMTP 会以
            # "550 From header is missing or invalid" 拒收
            msg["From"] = formataddr((self.from_name, self.from_address), charset="utf-8")
            msg["To"] = ", ".join(recipients)

            # 纯文本备选
            text_part = MIMEText(
                self._html_to_plain_text(content),
                "plain",
                "utf-8",
            )
            # HTML正文
            html_part = MIMEText(content, "html", "utf-8")

            msg.attach(text_part)
            msg.attach(html_part)

            # 发送
            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls()
                server.login(self.username, self.password)
                server.send_message(msg)

            masked = [self._mask_email(r) for r in recipients]
            logger.info(f"✅ 邮件发送成功 → {masked}")
            return True

        except Exception as e:
            logger.error(f"❌ 邮件发送失败: {e}")
            return False

    @staticmethod
    def _html_to_plain_text(html: str) -> str:
        """简单HTML转纯文本（去除标签）"""
        import re
        text = re.sub(r"<br\s*/?>", "\n", html)
        text = re.sub(r"<[^>]+>", "", text)
        text = text.replace("&nbsp;", " ").replace("&amp;", "&")
        return text.strip()