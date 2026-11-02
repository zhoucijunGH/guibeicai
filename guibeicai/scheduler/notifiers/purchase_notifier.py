"""
桂备菜 - 采购单推送编排器

把生成的采购单同时推送到多个渠道：
    - 邮件（HTML 清单，给老板邮箱）
    - 企业微信机器人（群消息）
    - 微信订阅消息（主动推到老板微信，移动端核心）

各渠道独立、互不阻塞：任一渠道失败不影响其他渠道，也不影响 WEB 端使用。
未配置的渠道自动跳过（优雅降级）。
"""

import logging
from typing import Dict, List, Optional

from .base import BaseNotifier
from .email_notifier import EmailNotifier
from .wecom_notifier import WeComNotifier
from .wx_subscribe_notifier import WxSubscribeNotifier

logger = logging.getLogger("GuiBeiCai-Scheduler")


def build_purchase_summary(order: Dict, store_name: str = "", date: str = "") -> Dict:
    """由 build_purchase_order 返回结构，生成推送用的文本与 HTML。

    返回: {"text": ..., "html": ..., "title": ..., "top_items": [...]}
    """
    items = order.get("items", []) or []
    total_cost = order.get("total_cost", 0) or 0
    total_qty = order.get("total_qty", 0) or 0
    dish_count = order.get("dish_count", 0) or 0

    title = f"🧾 {store_name or '门店'} {date or ''} 食材采购单"

    lines = [title, ""]
    lines.append(f"菜品数：{dish_count}　采购总额：¥{total_cost:,.2f}　总重量：{total_qty:,.2f}")
    lines.append("—" * 18)
    top = sorted(items, key=lambda x: x.get("est_cost", 0) or 0, reverse=True)[:15]
    for it in top:
        name = it.get("ingredient_name") or it.get("name") or "—"
        qty = it.get("quantity", 0) or 0
        unit = it.get("unit", "") or ""
        cost = it.get("est_cost", 0) or 0
        lines.append(f"· {name}  {qty:g}{unit}  ¥{cost:,.2f}")
    if len(items) > len(top):
        lines.append(f"… 其余 {len(items) - len(top)} 项详见小程序/后台")
    lines.append("")
    lines.append("👉 打开「桂备菜」小程序查看完整采购单并确认。")
    text = "\n".join(lines)

    # HTML 版本（邮件用）
    rows = "".join(
        f"<tr><td>{it.get('ingredient_name') or it.get('name') or '—'}</td>"
        f"<td>{it.get('quantity', 0):g}{it.get('unit', '') or ''}</td>"
        f"<td>¥{(it.get('est_cost', 0) or 0):,.2f}</td></tr>"
        for it in top
    )
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto;max-width:600px;margin:auto;">
      <h2 style="color:#e15554;">{title}</h2>
      <p>菜品数：<b>{dish_count}</b>　采购总额：<b>¥{total_cost:,.2f}</b>　总重量：<b>{total_qty:,.2f}</b></p>
      <table border="1" cellpadding="8" cellspacing="0" width="100%" style="border-collapse:collapse;font-size:14px;">
        <thead style="background:#f5f7fa;"><tr><th>食材</th><th>数量</th><th>预估金额</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      <p style="color:#888;font-size:12px;margin-top:12px;">打开「桂备菜」小程序查看完整采购单并确认下单。</p>
    </div>
    """
    return {
        "text": text,
        "html": html,
        "title": title,
        "top_items": [
            {"name": it.get("ingredient_name") or it.get("name"), "cost": it.get("est_cost", 0)}
            for it in top
        ],
    }


class PurchaseNotifier:
    """采购单多渠道路推编排器。"""

    def __init__(self, config: Dict = None):
        self.config = config or {}

    def send_purchase_order(
        self,
        order: Dict,
        store_name: str = "",
        date: str = "",
        emails: List[str] = None,
        openids: List[str] = None,
        wecom_webhook: str = "",
        wx_template_data: Dict = None,
        wx_page: str = "pages/ingredients/ingredients",
    ) -> Dict[str, bool]:
        """
        推送采购单到全部已配置渠道。

        返回各渠道结果：{"email": bool, "wecom": bool, "wx_subscribe": bool}
        """
        summary = build_purchase_summary(order, store_name=store_name, date=date)
        results: Dict[str, bool] = {}

        # 1) 邮件
        emails = [e for e in (emails or []) if e]
        if emails:
            try:
                email_cfg = dict(self.config.get("email", {}))
                notifier = EmailNotifier(email_cfg)
                ok = notifier.send(
                    subject=summary["title"],
                    content=summary["html"],
                    recipients=emails,
                )
                results["email"] = ok
            except Exception as e:
                logger.error("❌ 采购单邮件推送异常: %s", e)
                results["email"] = False
        else:
            results["email"] = False

        # 2) 企业微信
        if wecom_webhook:
            try:
                notifier = WeComNotifier({"enabled": True, "webhook_url": wecom_webhook})
                ok = notifier.send(
                    subject=summary["title"],
                    content=summary["text"],
                )
                results["wecom"] = ok
            except Exception as e:
                logger.error("❌ 采购单企微推送异常: %s", e)
                results["wecom"] = False
        else:
            results["wecom"] = False

        # 3) 微信订阅消息（移动端核心）
        openids = [o for o in (openids or []) if o]
        if openids:
            try:
                wx_cfg = dict(self.config.get("wx_subscribe", {}))
                notifier = WxSubscribeNotifier(wx_cfg)
                notifier.enabled = True
                ok = notifier.send(
                    subject=summary["title"],
                    content=summary["text"],
                    recipients=openids,
                    template_data=wx_template_data,
                    page=wx_page,
                )
                results["wx_subscribe"] = ok
            except Exception as e:
                logger.error("❌ 采购单微信订阅推送异常: %s", e)
                results["wx_subscribe"] = False
        else:
            results["wx_subscribe"] = False

        logger.info(
            "📤 采购单推送完成 store=%s date=%s => %s",
            store_name or "?", date or "?", results,
        )
        return results
