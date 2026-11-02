"""桂备菜 - 网络安全校验（S5 SSRF 防护）

POS/收银数据源的 base_url 由用户配置、服务端代发 HTTP 请求。低权限用户若可
任意配置地址，可能被用于探测/攻击内网（SSRF）。此模块集中校验 URL 安全性：
- 仅允许 http/https；
- 禁止回环/内网/链路本地/组播地址（含云元数据 169.254.169.254、IPv6 ULA/链路本地）；
- 支持可选域名白名单（GUOBEICAI_POS_URL_ALLOWLIST，逗号分隔）。

使用位置：pos_client.fetch_raw（实际拉取）、/api/pos-sources/test（连通测试）、
upsert_pos_source（保存时即拒绝危险地址）。
"""

import ipaddress
import os
import socket
from urllib.parse import urlparse

# 禁止访问的网段（SSRF 防护）
_BLOCKED_NETS = [
    ipaddress.ip_network("127.0.0.0/8"),       # 回环
    ipaddress.ip_network("10.0.0.0/8"),        # RFC1918 A
    ipaddress.ip_network("172.16.0.0/12"),     # RFC1918 B
    ipaddress.ip_network("192.168.0.0/16"),    # RFC1918 C
    ipaddress.ip_network("169.254.0.0/16"),    # 链路本地（含云元数据 169.254.169.254）
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT
    ipaddress.ip_network("::1/128"),           # IPv6 回环
    ipaddress.ip_network("fc00::/7"),          # IPv6 唯一本地地址
    ipaddress.ip_network("fe80::/10"),         # IPv6 链路本地
]


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    # 命中明确的危险网段，或属于回环/私网/链路本地/组播（已覆盖 RFC1918、云元数据
    # 169.254.169.254、IPv6 ULA/链路本地等），即拒绝。
    # 注意：不使用 ip.is_reserved —— 它会把 Teredo(2001::/32) 等合法公网隧道地址误判为
    # 保留地址，导致正常公网域名（如解析到 Teredo）被误杀。
    if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast:
        return True
    try:
        return any(ip in net for net in _BLOCKED_NETS)
    except Exception:
        return True


def validate_pos_url(url: str) -> tuple[bool, str]:
    """校验 POS base_url 是否可安全代发请求。返回 (ok, 原因)。"""
    if not url or not str(url).strip():
        return False, "接口地址为空"
    try:
        p = urlparse(str(url).strip())
    except Exception:
        return False, "地址格式非法"
    if p.scheme not in ("http", "https"):
        return False, "仅支持 http/https 协议"
    host = (p.hostname or "").strip().lower()
    if not host:
        return False, "缺少主机名"

    # 可选域名白名单：命中则放行（仍建议配合网络层隔离）
    allow = os.getenv("GUOBEICAI_POS_URL_ALLOWLIST", "").strip()
    if allow:
        allowed = [h.strip().lower() for h in allow.split(",") if h.strip()]
        if host in allowed:
            return True, ""
        return False, f"该域名不在允许列表：{host}"

    # 解析主机并校验每个解析出的 IP（基础防护，防常见 SSRF）
    try:
        infos = socket.getaddrinfo(host, None)
        ips = {info[4][0] for info in infos}
    except Exception:
        return False, f"无法解析主机：{host}"
    if not ips:
        return False, f"主机无可用地址：{host}"
    for ip_s in ips:
        try:
            ip = ipaddress.ip_address(ip_s)
        except ValueError:
            return False, f"非法 IP 地址：{ip_s}"
        if _ip_blocked(ip):
            return False, f"禁止访问内网/保留地址：{ip_s}"
    return True, ""
