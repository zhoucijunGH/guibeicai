"""
桂备菜 - 微信订阅消息推送渠道（移动端采购单推送核心）

通过「微信小程序订阅消息」把采购单主动推送到老板/店长的微信。
依赖环境变量：
    GUOBEICAI_WX_APPID   微信小程序 AppID
    GUOBEICAI_WX_SECRET  微信小程序 AppSecret
    GUOBEICAI_WX_TEMPLATE_ID  订阅消息模板 ID（在微信公众平台「订阅消息」中申请）

注意：
- 未配置 AppID/Secret 或模板 ID 时，send() 直接返回 False（优雅降级，不抛错），
  不影响 WEB 端与其他渠道（邮件/企微）正常运行。
- 订阅消息要求用户先在小程序内「发起订阅」（wx.requestSubscribeMessage），
  openid 来自 users.openid（由微信登录 code2session 绑定）。
"""

import logging
import os
import time
from typing import Dict, List, Optional

import requests

from .base import BaseNotifier

logger = logging.getLogger("GuiBeiCai-Scheduler")

WX_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
WX_SUBSCRIBE_SEND_URL = "https://api.weixin.qq.com/cgi-bin/message/subscribe/send"


class WxSubscribeNotifier(BaseNotifier):
    """微信小程序订阅消息推送"""

    def __init__(self, config: Dict):
        super().__init__(config)
        # 配置可来自 settings 环境变量或显式传入
        self.appid = os.getenv("GUOBEICAI_WX_APPID", "") or config.get("appid", "")
        self.secret = os.getenv("GUOBEICAI_WX_SECRET", "") or config.get("secret", "")
        self.template_id = (
            os.getenv("GUOBEICAI_WX_TEMPLATE_ID", "") or config.get("template_id", "")
        )
        self._access_token: Optional[str] = None
        self._token_exp: float = 0.0

    # ------------------------------------------------------------------ 配置

    def is_configured(self) -> bool:
        return bool(self.appid and self.secret and self.template_id)

    # ------------------------------------------------------------- access_token

    def _get_access_token(self) -> Optional[str]:
        if self._access_token and time.time() < self._token_exp - 60:
            return self._access_token
        if not self.is_configured():
            return None
        try:
            r = requests.get(
                WX_TOKEN_URL,
                params={
                    "grant_type": "client_credential",
                    "appid": self.appid,
                    "secret": self.secret,
                },
                timeout=8,
            )
            data = r.json()
            if "access_token" not in data:
                logger.error("❌ 微信 access_token 获取失败: %s", data)
                return None
            self._access_token = data["access_token"]
            # expires_in 通常 7200s，提前 5 分钟失效
            self._token_exp = time.time() + int(data.get("expires_in", 7200)) - 300
            return self._access_token
        except Exception as e:
            logger.error("❌ 微信 access_token 请求异常: %s", e)
            return None

    # -------------------------------------------------------------------- send

    def send(
        self,
        subject: str,
        content: str,
        recipients: List[str] = None,
        **kwargs,
    ) -> bool:
        """
        发送微信订阅消息。

        recipients: openid 列表（来自 users.openid）。
        kwargs:
            template_data: dict 订阅消息模板字段，形如 {"thing1": {"value": "采购单"}, ...}
            page: 小程序页面路径（点击消息跳转），如 "pages/ingredients/ingredients"
        若未提供 template_data，则尝试用 subject/content 自动构造一个最小模板。
        """
        if not self.enabled:
            logger.debug("微信订阅消息渠道未启用，跳过")
            return False
        if not self.is_configured():
            logger.warning("⚠️ 微信订阅消息未配置 AppID/Secret/模板ID，跳过（不影响其他渠道）")
            return False
        if not recipients:
            logger.warning("⚠️ 微信订阅消息无接收人（openid 为空），跳过")
            return False

        token = self._get_access_token()
        if not token:
            return False

        template_data = kwargs.get("template_data") or self._auto_template(subject, content)
        page = kwargs.get("page") or "pages/ingredients/ingredients"

        ok_all = True
        for openid in recipients:
            if not openid:
                continue
            payload = {
                "touser": openid,
                "template_id": self.template_id,
                "page": page,
                "data": template_data,
            }
            try:
                r = requests.post(
                    WX_SUBSCRIBE_SEND_URL,
                    params={"access_token": token},
                    json=payload,
                    timeout=8,
                )
                data = r.json()
                if data.get("errcode") == 0:
                    logger.info("✅ 微信订阅消息已推送给 %s", openid[:6] + "***")
                else:
                    logger.error("❌ 微信订阅消息发送失败 openid=%s: %s", openid[:6], data)
                    ok_all = False
            except Exception as e:
                logger.error("❌ 微信订阅消息请求异常 openid=%s: %s", openid[:6], e)
                ok_all = False
        return ok_all

    # 当用户未自定义模板字段时，用 subject/content 兜底（模板需含对应 key）
    @staticmethod
    def _auto_template(subject: str, content: str) -> Dict:
        snippet = content.strip().split("\n")[0][:20] if content else subject[:20]
        return {
            "thing1": {"value": subject[:20]},
            "thing2": {"value": snippet[:20]},
        }
