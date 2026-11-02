"""桂备菜 - 通用 POS/收银数据源接入客户端（#商业化：通用可配置数据源）

设计目标：一套代码对接多种数据源，页面可配置即可完成对接。

支持的数据源类型（source_type）：
  - http_json:  GET 一个 HTTP 接口，返回 JSON；按 field_mapping 抽取菜品销量数组。
  - csv_http:   GET 一个返回 CSV 的地址（或 ?format=csv），按表头映射到菜品销量。
  - webhook:    被动接收模式 —— 由 POS 方把销售数据 POST 到本系统 /api/actual-sales，
                此类型仅做配置登记，不主动拉取（拉取按钮会提示「推送模式」）。

字段映射 field_mapping（JSON，逻辑字段 -> 源字段名/路径）：
  - items_path: 数组在响应中的路径，如 "data.items" 或 "result.list"（点分隔）；空表示根即数组/对象列表。
  - dish:       菜品名称字段（必填）
  - qty:        销量字段（必填）
  - date:       日期字段（可选；缺省用 sale_date_default）
  - dish_id:    菜品ID字段（可选，优先于名称匹配）
  - amount:     金额字段（可选）
  - auto_create_dish: true/false（源中出现菜单未收录的菜品时，是否自动建档；默认 true）
  - date_format: 源日期格式（如 "%Y-%m-%d" / "%Y%m%d"），用于解析；留空则尽力解析

所有拉取结果按 门店+菜品+日期 upsert 进 actual_sales，与现有手工/小程序录入共用同一张表，
手工录入作为 POS 异常时的兜底（互不冲突）。
"""

import base64
import csv
import datetime
import io
import json
import logging
from typing import Dict, List, Optional

import requests

from guibeicai.web import _db
from guibeicai.web import _net

logger = logging.getLogger(__name__)

SUPPORTED_TYPES = ["http_json", "csv_http", "webhook"]


# ----------------------------- 路径/类型工具 -----------------------------
def _resolve_path(obj, path: str):
    """按点分隔路径从 dict/list 取值；支持 list 索引（如 items.0）。"""
    if not path:
        return obj
    cur = obj
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def _to_float(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def _to_int(v):
    try:
        return int(float(v))
    except Exception:
        return None


def _normalize_date(value, fmt: str = ""):
    """尽量把源日期规整为 YYYY-MM-DD；失败返回 None。"""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for f in [x for x in [fmt, "%Y-%m-%d", "%Y/%m/%d", "%Y%m%d"] if x]:
        try:
            return datetime.datetime.strptime(s, f).strftime("%Y-%m-%d")
        except Exception:
            pass
    for f in ["%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"]:
        try:
            return datetime.datetime.strptime(s, f).strftime("%Y-%m-%d")
        except Exception:
            pass
    if len(s) >= 10 and s[4] in "-/" and s[7] in "-/":
        return s[:10].replace("/", "-")
    return None


# ----------------------------- 认证头构造 -----------------------------
def _build_headers(source: Dict) -> Dict:
    headers = {"User-Agent": "GuiBeiCai-POS-Client/1.0"}
    auth_type = source.get("auth_type", "none")
    auth = source.get("auth_config") or {}
    if auth_type == "api_key":
        headers[auth.get("header", "X-API-Key")] = auth.get("api_key", "")
    elif auth_type == "bearer":
        headers["Authorization"] = f"Bearer {auth.get('token', '')}"
    elif auth_type == "basic":
        raw = f"{auth.get('user', '')}:{auth.get('pass', '')}"
        headers["Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
    return headers


# ----------------------------- 拉取原始数据 -----------------------------
def _csv_bytes_to_records(content: bytes) -> List[Dict]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def _substitute_placeholders(template: str, sale_date: str) -> str:
    """把请求模板中的 {sale_date} 占位符替换为指定日期（拉取某天销量用）。"""
    if not template:
        return template
    return template.replace("{sale_date}", sale_date or "")


def _build_request(source: Dict, sale_date: str):
    """依据 request_config 构造 requests 调用参数（支持 method/params/body/自定义头/{sale_date}）。

    返回 (method, url, kwargs)。webhook 类型抛 NotImplementedError。
    """
    stype = source.get("source_type", "http_json")
    if stype == "webhook":
        raise NotImplementedError("webhook 为推送模式，无需主动拉取；请由 POS 方 POST 到本系统 /api/actual-sales")

    url = (source.get("base_url") or "").strip()
    if not url:
        raise ValueError("数据源 base_url 为空")
    # [S5] SSRF 防护：代发请求前校验地址安全性
    ok, reason = _net.validate_pos_url(url)
    if not ok:
        raise ValueError(f"接口地址不安全，已拒绝请求：{reason}")

    req = source.get("request_config") or {}
    if isinstance(req, str):
        try:
            req = json.loads(req or "{}")
        except Exception:
            req = {}
    method = (req.get("method") or "GET").upper()
    content_type = (req.get("content_type") or "json").lower()
    verify_ssl = req.get("verify_ssl", True)

    headers = _build_headers(source)
    for k, v in (req.get("headers") or {}).items():
        headers[str(k)] = str(v)
    if req.get("body") and method == "POST" and "Content-Type" not in headers and content_type == "json":
        headers["Content-Type"] = "application/json"

    # 查询参数 / 请求体：替换 {sale_date} 占位符
    params = {k: _substitute_placeholders(str(v), sale_date) for k, v in (req.get("params") or {}).items()}
    data = None
    json_body = None
    if req.get("body"):
        body_str = _substitute_placeholders(str(req.get("body")), sale_date)
        if method == "POST" and content_type == "json":
            try:
                json_body = json.loads(body_str)
            except Exception:
                data = body_str
        else:
            data = body_str

    kwargs = {"headers": headers, "timeout": 15, "verify": verify_ssl}
    if params:
        kwargs["params"] = params
    if json_body is not None:
        kwargs["json"] = json_body
    elif data is not None:
        kwargs["data"] = data
    return method, url, kwargs


def fetch_raw(source: Dict, sale_date: str = None, timeout: int = 15):
    """拉取源数据，返回解析后的 Python 对象（dict/list）。webhook 类型抛 NotImplementedError。

    request_config 支持 method/params/body/content_type/headers/{sale_date} 占位。
    """
    if sale_date is None:
        sale_date = datetime.date.today().strftime("%Y-%m-%d")
    method, url, kwargs = _build_request(source, sale_date)
    kwargs["timeout"] = timeout
    r = requests.request(method, url, **kwargs)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "")
    if "csv" in ctype or source.get("source_type") == "csv_http":
        return _csv_bytes_to_records(r.content)
    return r.json()


# ----------------------------- 字段映射抽取 -----------------------------
def extract_records(raw, mapping: Dict) -> List[Dict]:
    """从 raw 中按 mapping 抽取菜品销量记录。每条: {dish_id?, dish_name, qty, sale_date?, amount?}"""
    if raw is None:
        return []
    arr = _resolve_path(raw, (mapping.get("items_path") or "").strip())
    if arr is None:
        arr = raw if isinstance(raw, list) else [raw]
    if not isinstance(arr, list):
        arr = [arr]

    dish_f = mapping.get("dish") or "dish_name"
    qty_f = mapping.get("qty") or "qty"
    date_f = mapping.get("date") or ""
    did_f = mapping.get("dish_id") or ""
    amt_f = mapping.get("amount") or ""

    out = []
    for rec in arr:
        if not isinstance(rec, dict):
            continue
        name = rec.get(dish_f)
        qty = rec.get(qty_f)
        if name is None or qty is None:
            continue
        item = {"dish_name": str(name).strip(), "qty": _to_float(qty)}
        if did_f and rec.get(did_f) not in (None, ""):
            item["dish_id"] = _to_int(rec.get(did_f))
        if date_f and rec.get(date_f) not in (None, ""):
            item["sale_date"] = _normalize_date(rec.get(date_f), mapping.get("date_format", ""))
        if amt_f and rec.get(amt_f) not in (None, ""):
            item["amount"] = _to_float(rec.get(amt_f))
        out.append(item)
    return out


def _validate_envelope(raw, mapping: Dict) -> tuple:
    """响应信封校验：success_path/success_value 命中才算成功；否则返回错误文案。

    返回 (ok: bool, msg: str)。未配置 success_path 时视为成功。
    """
    success_path = (mapping or {}).get("success_path")
    if not success_path:
        return True, ""
    expected = (mapping or {}).get("success_value")
    actual = _resolve_path(raw, success_path)
    # 宽松比较：JSON 中的数字(int)与配置中的字符串("0")等价
    if str(actual) != str(expected):
        msg_path = (mapping or {}).get("message_path")
        detail = _resolve_path(raw, msg_path) if msg_path else actual
        return False, f"响应信封校验未通过（{success_path}={actual!r}，期望 {expected!r}）：{detail}"
    return True, ""


# ----------------------------- 归一化写入 -----------------------------
def normalize_and_upsert(store_id: int, records: List[Dict], sale_date_default: str, mapping: Dict) -> Dict:
    """把抽取记录写入 actual_sales（按 门店+菜品+日期 upsert）。返回统计。

    POS 自动对接（无人值守）增强：
    - 不存在的菜品按 auto_create_dish 自动建档（默认开）；
    - 新建档菜品置 bom_status='pending'，函数末尾自动触发配方重生成（auto_regen_bom，默认开），
      使新菜自动获得食材/BOM；
    - POS 写入的销量同步置 reconciled=1（同步成功即视为已对账），供后续回灌预测历史。
    """
    auto_create = mapping.get("auto_create_dish", True)
    auto_regen_bom = mapping.get("auto_regen_bom", True)
    dishes = _db.get_dishes_by_store(store_id, active_only=False)
    name2id = {d["name"]: d["id"] for d in dishes}
    norm2id = {_db.normalize_name(d["name"]): d["id"] for d in dishes}

    stats = {"total": len(records), "upserted": 0, "skipped": 0, "auto_created": 0,
             "auto_bom_triggered": False, "auto_bom_ok": None, "errors": []}
    new_dish_ids = []
    for rec in records:
        name = rec.get("dish_name", "")
        if not name:
            stats["skipped"] += 1
            continue
        did = rec.get("dish_id")
        if did:
            dish = _db.get_dish_by_id(did)
            if not dish or dish.get("store_id") != store_id:
                did = None
        if not did:
            did = name2id.get(name)
        if not did:
            did = norm2id.get(_db.normalize_name(name))
        if not did:
            if auto_create:
                did, is_new = _db.upsert_dish_ex(store_id, name, category="", standard_price=0)
                if is_new:
                    stats["auto_created"] += 1
                    new_dish_ids.append(did)
                name2id[name] = did
            else:
                stats["skipped"] += 1
                stats["errors"].append(f"菜品未匹配且未自动建档: {name}")
                continue
        sale_date = rec.get("sale_date") or sale_date_default
        if not sale_date:
            stats["skipped"] += 1
            stats["errors"].append(f"缺日期且无可兜底日期: {name}")
            continue
        try:
            # POS 同步写入即视为已对账，落 reconciled=1
            _db.upsert_actual_sales(store_id, did, sale_date, rec.get("qty", 0), 0,
                                    actual_amount=rec.get("amount", 0), reconciled=1)
            stats["upserted"] += 1
        except Exception as e:
            stats["skipped"] += 1
            stats["errors"].append(f"写入失败 {name}: {e}")

    # 新建档菜品自动生成食材/BOM（增量、幂等；仅处理 pending）
    if new_dish_ids and auto_regen_bom:
        stats["auto_bom_triggered"] = True
        try:
            # 延迟导入，避免循环依赖且不必在仅拉数时加载 LLM
            from guibeicai.web._bom_regen import regen_bom
            result = regen_bom(store_id=store_id)
            if result is None:
                stats["auto_bom_ok"] = False
                stats["errors"].append("未配置 LLM API_KEY，新菜配方生成跳过（保留 pending，配置后重试）")
            else:
                stats["auto_bom_ok"] = True
                stats["auto_bom_result"] = result
        except Exception as e:
            stats["auto_bom_ok"] = False
            stats["errors"].append(f"新菜配方生成异常: {e}")
    return stats


# ----------------------------- 对外入口 -----------------------------
def test_connection(source: Dict) -> Dict:
    """仅测试连通性与字段映射是否可用，不写库。返回 {ok, msg, sample_count, note?}。"""
    try:
        raw = fetch_raw(source)
        ok, msg = _validate_envelope(raw, source.get("field_mapping") or {})
        if not ok:
            return {"ok": False, "msg": f"连通成功但{msg}", "sample_count": 0}
        recs = extract_records(raw, source.get("field_mapping") or {})
        return {"ok": True, "msg": f"连通成功，解析到 {len(recs)} 条销量记录", "sample_count": len(recs)}
    except NotImplementedError as e:
        return {"ok": True, "msg": str(e), "sample_count": 0, "note": "push_mode"}
    except Exception as e:
        return {"ok": False, "msg": f"连接失败: {e}", "sample_count": 0}


def pull_source(source_id: int, sale_date_default: str = None) -> Dict:
    """完整拉取并写入。返回统计；同时写同步日志。"""
    src = _db.get_pos_source_decrypted(source_id)
    if not src:
        return {"ok": False, "msg": "数据源不存在", "stats": None}
    if src.get("source_type") == "webhook":
        msg = "webhook 为推送模式，无需主动拉取；请由 POS 方 POST 到 /api/actual-sales"
        _db.set_pos_sync_result(source_id, "skip", msg, src.get("store_id"))
        return {"ok": True, "msg": msg, "stats": None, "push_mode": True}
    if not src.get("enabled"):
        msg = "数据源已禁用，跳过"
        _db.set_pos_sync_result(source_id, "skip", msg, src.get("store_id"))
        return {"ok": True, "msg": msg, "stats": None}

    try:
        raw = fetch_raw(src, sale_date_default or datetime.date.today().strftime("%Y-%m-%d"))
        mapping = src.get("field_mapping") or {}
        env_ok, env_msg = _validate_envelope(raw, mapping)
        if not env_ok:
            _db.set_pos_sync_result(source_id, "error", f"响应信封校验失败: {env_msg}", src.get("store_id"))
            return {"ok": False, "msg": f"响应信封校验失败: {env_msg}", "stats": None}
        recs = extract_records(raw, mapping)
        if not sale_date_default:
            sale_date_default = datetime.date.today().strftime("%Y-%m-%d")
        stats = normalize_and_upsert(src["store_id"], recs, sale_date_default, mapping)
        ok = stats["upserted"] > 0 or stats["total"] == 0
        # 实际销量完成对账后持续 feed 预测历史（sales_history）
        bf = {"rows": 0, "upserted": 0}
        try:
            bf = _db.backfill_history_from_actual(store_id=src["store_id"], only_reconciled=True)
        except Exception as e:
            logger.warning("POS 回灌 sales_history 失败 source_id=%s: %s", source_id, e)
        stats["backfill"] = bf
        _db.set_pos_sync_result(
            source_id,
            "success" if ok else "error",
            f"解析 {stats['total']} 条，写入 {stats['upserted']} 条，自动建档 {stats['auto_created']} 条，"
            f"配方生成 {'成功' if stats.get('auto_bom_ok') else '跳过'}，回灌历史 {bf['upserted']} 条，"
            f"跳过 {stats['skipped']} 条",
            src.get("store_id"),
        )
        return {"ok": ok, "msg": "拉取完成", "stats": stats}
    except Exception as e:
        logger.exception("POS 拉取失败 source_id=%s: %s", source_id, e)
        _db.set_pos_sync_result(source_id, "error", f"拉取异常: {e}", src.get("store_id"))
        return {"ok": False, "msg": f"拉取异常: {e}", "stats": None}


def run_all_pos_pulls(sale_date_default: str = None) -> Dict:
    """遍历所有启用的「主动拉取型」数据源并拉取（供调度器定时调用）。

    返回 {(source_id, name): result}。webhook 类型与禁用项自动跳过。
    """
    results = {}
    for src in _db.get_all_pos_sources():
        if not src.get("enabled") or src.get("source_type") == "webhook":
            continue
        try:
            results[(src["id"], src["name"])] = pull_source(src["id"], sale_date_default)
        except Exception as e:
            logger.exception("POS 调度拉取失败 source_id=%s: %s", src["id"], e)
            results[(src["id"], src["name"])] = {"ok": False, "msg": str(e)}
    return results

