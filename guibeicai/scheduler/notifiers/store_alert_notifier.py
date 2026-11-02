"""
桂备菜 - 门店业务预警推送（邮件 + 企业微信）
===========================================
把「智能预警」生成的加备/减备/节假日/偏差类预警，推送给门店配置的接收渠道：
  - 邮件：老板邮箱（stores.owner_email）
  - 企业微信：门店群机器人 webhook（stores.wecom_webhook）
微信订阅消息本期未接入（需小程序订阅模板 + 用户 openid 授权），后续可扩展。
"""

import logging
from typing import Dict, List

from guibeicai.config.settings import Settings
from .email_notifier import EmailNotifier
from .wecom_notifier import WeComNotifier

logger = logging.getLogger("GuiBeiCai-Scheduler")


class StoreAlertNotifier:
    """门店业务预警通知器：同时走邮件 + 企微（按门店配置的目标渠道）。"""

    def __init__(self, config: Dict = None):
        self._settings = Settings()
        cfg = config or {}
        self.email_cfg = dict(cfg.get("email", self._settings.email_config))

    def send_store_alert(self, store_name: str, alerts: List[Dict], targets: Dict) -> bool:
        """推送该门店的预警清单。

        targets: get_store_push_targets(store_id) 的返回值
                 {"emails": [...], "openids": [...], "wecom_webhook": "..."}
        返回是否至少成功发送到一个渠道。
        """
        if not alerts:
            return False
        emails = [e for e in (targets.get("emails") or []) if e]
        wecom = (targets.get("wecom_webhook") or "").strip()
        if not emails and not wecom:
            return False

        # 组装正文：邮件用 HTML，企微用 Markdown
        html_parts = []
        md_parts = []
        for a in alerts:
            sev = a.get("severity", "")
            title = a.get("title", "")
            detail = a.get("detail", "")
            html_parts.append(f"<p><b>[{sev}] {title}</b><br/>{detail}</p>")
            md_parts.append(f"**{sev} {title}**\n> {detail}")
        subject = f"🔔 桂备菜预警：{store_name} 有 {len(alerts)} 条待处理"
        html = ("<h3>桂备菜智能预警</h3>" + "".join(html_parts) +
                "<p><small>请在系统中「智能预警」页查看并处理。</small></p>")
        md = "\n\n".join(md_parts) + "\n\n> 请在系统中「智能预警」页查看并处理"

        ok = False
        if emails:
            try:
                en = EmailNotifier(self.email_cfg)
                en.send(subject=subject, content=html, recipients=emails)
                ok = True
                logger.info("[预警推送] 邮件已发送 store=%s to=%s", store_name, emails)
            except Exception:
                logger.exception("[预警推送] 邮件发送失败 store=%s", store_name)
        if wecom:
            try:
                wn = WeComNotifier({"enabled": True, "webhook_url": wecom})
                wn.send(subject=subject, content=md)
                ok = True
                logger.info("[预警推送] 企微已发送 store=%s", store_name)
            except Exception:
                logger.exception("[预警推送] 企微发送失败 store=%s", store_name)
        return ok
