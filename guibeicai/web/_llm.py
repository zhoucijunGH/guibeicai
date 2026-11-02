import json
import logging
import os
import sys
import hashlib
import re
import time
from datetime import date as date_cls
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# 确保 .env 在 Web 进程中也加载（settings 的 load_dotenv 仅 cli.py 触发；
# 包 __init__ 也会加载，这里再加一层自保证，使本模块的 API_KEY 在 import 时即生效）
try:
    from dotenv import load_dotenv

    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
    else:
        load_dotenv()
except Exception:
    pass

# 本地依赖（不污染全局环境）：json_repair 用于修复 LLM 偶发的括号错配/截断 JSON。
# 放在项目根 .pylibs 目录，运行时动态加入 sys.path。
_LOCAL_LIBS = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".pylibs"))
if os.path.isdir(_LOCAL_LIBS) and _LOCAL_LIBS not in sys.path:
    sys.path.insert(0, _LOCAL_LIBS)

import httpx
from chinese_calendar import is_holiday, is_workday

logger = logging.getLogger(__name__)

# ===== 配置 =====
API_KEY = os.getenv("SILICONFLOW_API_KEY", "").strip()
API_URL = "https://api.siliconflow.cn/v1/chat/completions"
MODEL = os.getenv("LLM_MODEL", "Qwen/Qwen3-8B")

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_llm_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ===== 性能/成本相关配置（12-factor，可经环境变量覆盖）=====
LLM_CACHE_ENABLED = os.getenv("LLM_CACHE_ENABLED", "1") not in ("0", "false", "False", "")
LLM_CACHE_TTL_SECONDS = int(os.getenv("LLM_CACHE_TTL_SECONDS", "86400"))  # 默认缓存 1 天
LLM_CACHE_MAX_WORKERS = max(1, int(os.getenv("LLM_MAX_WORKERS", "5")))     # 并发批处理线程数

SYSTEM_PROMPT = """你是餐饮采购预测助手。
输入：菜品名称、天气、日历维度（周几/是否工作日/是否节假日/是否调休/寒暑假/本地民俗/特殊事件）、人工事件。
输出：仅输出一个紧凑 JSON 对象，不要 markdown，不要解释，不要思考过程，不要换行，不要注释。

注意：输出必须是标准 JSON 对象，字符串内部如需引号才转义；键名、逗号、冒号、花括号不能带反斜杠。

规则约束：
- 高温（>=30℃）通常抑制热菜/重口味/油炸/汤煮类需求，不得仅因"热食"而提高multiplier。
- 若证据不足，multiplier取0.95~1.05。

字段：
{
  "multiplier": 0.0~1.5 之间的数，默认1.0（0=停售/下架），
  "temp_adj": 温度影响，如 0.5/-0.3，
  "rain_penalty": 降雨惩罚，如 -0.1，
  "note": "简短原因，需综合天气+日历+人工事件",
  "risk_level": "低"或"偏高"或"高",
  "conclusion": "销量结论（结合菜品）",
  "purchase_advice": "采购建议（结合菜品）",
  "risk_factors": ["高温","周末","工作日","调休","促销"]  ← 根据实际影响填写，不要只写天气，
  "purchase_details": ["减少原材料采购"] 或 []
}
"""


# ========== 工具 ==========

def _norm_dish_name(s: str) -> str:
    return (s or "").strip()


# ========== 日期解析 ==========

def _parse_date(d_str: str):
    if not d_str:
        return None
    d_str = str(d_str).strip()
    try:
        parts = d_str.split("-")
        if len(parts) == 2:
            y = date_cls.today().year
            m, d = int(parts[0]), int(parts[1])
            return date_cls(y, m, d)
        elif len(parts) == 3:
            return date_cls(int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return None


# ========== 单日日历标签 ==========

def _date_calendar_label(d_obj: date_cls) -> str:
    dow_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    dow = dow_names[d_obj.weekday()]

    special_label = ""
    try:
        from guibeicai.web._special_events import check_special_event
        label = check_special_event(d_obj, "南宁市")
        if label:
            special_label = f" [{label}]"
    except Exception:
        pass

    if is_holiday(d_obj):
        return f"{d_obj.strftime('%m-%d')}({dow}) 法定节假日{special_label}"
    if is_workday(d_obj) and d_obj.weekday() >= 5:
        return f"{d_obj.strftime('%m-%d')}({dow}) 调休上班{special_label}"
    if d_obj.weekday() >= 5:
        return f"{d_obj.strftime('%m-%d')}({dow}) 周末{special_label}"
    return f"{d_obj.strftime('%m-%d')}({dow}) 工作日{special_label}"


# ========== 日历维度描述 ==========

def _build_calendar_desc(holiday_dates: list, manual_event: str = "", predict_date_str: str = "") -> str:
    seen = set()
    items = []

    if predict_date_str:
        d_obj = _parse_date(predict_date_str)
        if d_obj:
            items.append(_date_calendar_label(d_obj))
            seen.add(d_obj.strftime("%Y-%m-%d"))

    for d_str in (holiday_dates or []):
        d_obj = _parse_date(d_str)
        if not d_obj:
            continue
        key = d_obj.strftime("%Y-%m-%d")
        if key in seen:
            continue
        seen.add(key)
        items.append(_date_calendar_label(d_obj))

    if not items:
        return "无特殊日期"

    return "；".join(items)


# ========== 从 weather_text 提取日期 ==========

def _extract_date_from_weather(weather_text: str) -> str:
    if not weather_text:
        return ""
    m = re.search(r"(\d{2}-\d{2})", weather_text)
    return m.group(1) if m else ""


# ========== 缓存（读+写，避免重复请求） ==========

def _llm_cache_key(dish: str, weather_text: str, holiday_dates: list, manual_event: str, city: str, model: str) -> str:
    """相同输入（菜品+天气+日历+人工事件+模型）应命中同一缓存。"""
    param_str = f"{dish}|{weather_text}|{sorted(holiday_dates or [])}|{manual_event}|{city}|{model}"
    return hashlib.md5(param_str.encode("utf-8")).hexdigest()


def _llm_cache_file(key: str) -> str:
    return os.path.join(CACHE_DIR, f"result_{key}.json")


def _llm_cache_get(key: str):
    if not LLM_CACHE_ENABLED:
        return None
    try:
        fp = _llm_cache_file(key)
        if not os.path.exists(fp):
            return None
        with open(fp, "r", encoding="utf-8") as f:
            rec = json.load(f)
        age = time.time() - rec.get("ts", 0)
        if age > LLM_CACHE_TTL_SECONDS:
            logger.info("[LLM] 缓存过期 key=%s age=%.0fs", key, age)
            return None
        return rec.get("content")
    except Exception as e:
        logger.warning("[LLM] 缓存读取失败: %s", e)
        return None


def _llm_cache_put(key: str, content: str):
    if not LLM_CACHE_ENABLED:
        return
    try:
        with open(_llm_cache_file(key), "w", encoding="utf-8") as f:
            json.dump({"ts": time.time(), "model": MODEL, "content": content}, f, ensure_ascii=False)
    except Exception as e:
        logger.warning("[LLM] 缓存写入失败: %s", e)


def _cache_raw_response(content: str, weather_text: str, holiday_dates: list, manual_event: str, city: str, dish: str = ""):
    """保留原始响应便于排查（与结果缓存分离，纯调试用途）。"""
    try:
        param_str = f"{dish}_{len(weather_text)}_{holiday_dates}_{manual_event}_{city}_{date_cls.today().strftime('%Y%m%d_%H%M%S')}"
        digest = hashlib.md5(param_str.encode()).hexdigest()[:8]
        ts = date_cls.today().strftime("%Y%m%d_%H%M%S")
        filename = f"{ts}_{digest}.txt"
        filepath = os.path.join(CACHE_DIR, filename)
        meta = {
            "timestamp": ts, "model": MODEL, "dish": dish, "city": city,
            "weather_text_len": len(weather_text), "holiday_dates": holiday_dates,
            "manual_event": manual_event, "content_len": len(content or ""),
        }
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False, indent=2))
            f.write("\n\n===== RAW CONTENT =====\n\n")
            f.write(content or "")
        logger.info("[LLM] 原始响应已缓存: %s (len=%d)", filename, len(content or ""))
        return filepath
    except Exception as e:
        logger.error("[LLM] 原始响应缓存写入失败: %s", e)
        return None


# ========== 清洗/修复/解析/校验 ==========

def _clean_llm_json(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    s = s.replace("﻿", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        s = s[start:end + 1]
    return s.strip()


def _repair_basic(s: str) -> str:
    s = s.replace("'", '"')
    s = s.replace("True", "true").replace("False", "false").replace("None", "null")
    s = s.replace(",}", "}").replace(",]", "]")
    return s


def _repair_escaped(s: str) -> str:
    candidate = s.replace('\\"', '"')
    try:
        json.loads(candidate)
        return candidate
    except Exception:
        pass
    fixed = s
    for key in ["risk_level", "conclusion", "purchase_advice", "risk_factors", "purchase_details", "multiplier", "temp_adj", "rain_penalty", "note"]:
        fixed = fixed.replace(f'\\"{key}"', f'","{key}"')
    try:
        json.loads(fixed)
        return fixed
    except Exception:
        pass
    return s


def _repair_aggressive(s: str) -> dict:
    fields = {}
    for key in ["multiplier", "temp_adj", "rain_penalty"]:
        m = re.search(rf'"{key}"\s*:\s*([0-9.-]+)', s)
        if m:
            try:
                fields[key] = float(m.group(1))
            except Exception:
                fields[key] = 0.0
    for key in ["note", "risk_level", "conclusion", "purchase_advice"]:
        m = re.search(rf'"{key}"\s*:\s*"([^"]*)', s)
        if m:
            fields[key] = m.group(1)
    for key in ["risk_factors", "purchase_details"]:
        m = re.search(rf'"{key}"\s*:\s*\[(.*?)\]', s, re.DOTALL)
        if m:
            try:
                val = json.loads("[" + m.group(1) + "]")
                fields[key] = val
            except Exception:
                fields[key] = []
        else:
            fields[key] = []
    fields.setdefault("multiplier", 1.0)
    fields.setdefault("temp_adj", 0.0)
    fields.setdefault("rain_penalty", 0.0)
    fields.setdefault("note", "激进修复提取")
    fields.setdefault("risk_level", "低")
    fields.setdefault("conclusion", "")
    fields.setdefault("purchase_advice", "")
    fields.setdefault("risk_factors", [])
    fields.setdefault("purchase_details", [])
    return fields


def _parse_with_strategies(content: str, filepath: str = None):
    cleaned = _clean_llm_json(content)
    logger.info("[LLM] 清洗后前缀: %s", cleaned[:200])
    try:
        result = json.loads(cleaned)
        logger.info("[解析] 策略1(直接) 成功")
        return result
    except json.JSONDecodeError as e:
        logger.warning("[解析] 策略1(直接) 失败: %s", e)
    try:
        fixed = _repair_escaped(cleaned)
        result = json.loads(fixed)
        logger.info("[解析] 策略2(转义修复) 成功")
        return result
    except Exception as e:
        logger.warning("[解析] 策略2(转义修复) 失败: %s", e)
    try:
        fixed = _repair_basic(cleaned)
        result = json.loads(fixed)
        logger.info("[解析] 策略3(基础修复) 成功")
        return result
    except Exception as e:
        logger.warning("[解析] 策略3(基础修复) 失败: %s", e)
    try:
        from json_repair import repair_json
        result = repair_json(cleaned, skip_json_loads=True, return_objects=True)
        if isinstance(result, dict):
            logger.info("[解析] 策略4(json_repair) 成功")
            return result
    except ImportError:
        logger.warning("[解析] 策略4(json_repair) 未安装，跳过")
    except Exception as e:
        logger.warning("[解析] 策略4(json_repair) 失败: %s", e)
    try:
        result = _repair_aggressive(cleaned)
        if result and isinstance(result, dict):
            logger.info("[解析] 策略5(激进提取) 成功")
            return result
    except Exception as e:
        logger.warning("[解析] 策略5(激进提取) 失败: %s", e)
    logger.error("[解析] 所有策略均失败")
    return None


def _validate_analysis(result: dict) -> dict:
    try:
        multiplier = max(0.0, min(1.5, float(result.get("multiplier", 1.0))))
    except Exception:
        multiplier = 1.0
    try:
        temp_adj = float(result.get("temp_adj", 0.0))
    except Exception:
        temp_adj = 0.0
    try:
        rain_penalty = float(result.get("rain_penalty", 0.0))
    except Exception:
        rain_penalty = 0.0
    risk_level = str(result.get("risk_level", "低") or "低")
    if risk_level not in ("低", "偏高", "高"):
        if "高" in risk_level:
            risk_level = "高"
        elif "偏" in risk_level or "中" in risk_level:
            risk_level = "偏高"
        else:
            risk_level = "低"
    return {
        "multiplier": round(multiplier, 3),
        "temp_adj": round(temp_adj, 2),
        "rain_penalty": round(rain_penalty, 2),
        "note": str(result.get("note", "") or ""),
        "risk_level": risk_level,
        "conclusion": str(result.get("conclusion", "") or ""),
        "purchase_advice": str(result.get("purchase_advice", "") or ""),
        "risk_factors": [str(x) for x in (result.get("risk_factors") or []) if str(x).strip()],
        "purchase_details": [str(x) for x in (result.get("purchase_details") or []) if str(x).strip()],
    }


def _fallback_result(note: str = "默认系数") -> dict:
    return {
        "multiplier": 1.0, "temp_adj": 0.0, "rain_penalty": 0.0,
        "note": note, "risk_level": "低", "conclusion": "", "purchase_advice": "",
        "risk_factors": [], "purchase_details": [],
    }


def _finalize(dish: str, content: str):
    """解析+校验+打标签，供缓存命中与实时请求两套路径复用。失败返回 None。"""
    result = _parse_with_strategies(content)
    if result is None or not isinstance(result, dict):
        return None
    out = _validate_analysis(result)
    out["dish"] = dish
    return out


# ========== 单菜品 LLM 请求 ==========

def _call_llm_for_dish(dish: str, weather_text: str, holiday_dates: list, manual_event: str, city: str = "") -> dict:
    """对单个菜品请求 LLM，返回解析后的 dict（失败返回兜底）"""
    if not API_KEY:
        logger.error("[LLM] API_KEY 未设置")
        return _fallback_result("API_KEY未配置")

    try:
        API_KEY.encode("ascii")
    except UnicodeEncodeError:
        logger.error("[LLM] API_KEY 含非ASCII字符")
        return _fallback_result("API_KEY编码非法")

    weather_text = weather_text or ""
    manual_event = manual_event or ""
    holiday_dates = holiday_dates or []
    city = city or ""

    # ★★★ 从 special_events 配置注入事件 ★★★
    predict_date_str = _extract_date_from_weather(weather_text)
    try:
        from guibeicai.web._special_events import get_special_event_context
        extra_dates, extra_manual = get_special_event_context(predict_date_str, city, manual_event)
        logger.info("[LLM] get_special_event_context 返回: dates=%s manual=%s (predict_date=%s city=%s)",
                    extra_dates, extra_manual, predict_date_str, city)
        if extra_dates:
            holiday_dates = list(set(holiday_dates + extra_dates))
        if extra_manual:
            manual_event = extra_manual
    except Exception as e:
        logger.warning("[LLM] get_special_event_context 调用失败: %s", e)

    # ===== 结果缓存：相同输入直接命中，跳过 API（省成本+降延迟）=====
    cache_key = _llm_cache_key(dish, weather_text, holiday_dates, manual_event, city, MODEL)
    _cached = _llm_cache_get(cache_key)
    if _cached is not None:
        _hit = _finalize(dish, _cached)
        if _hit is not None:
            logger.info("[LLM] 缓存命中 dish=%s，跳过 API 请求", dish)
            return _hit

    # 构造日历维度描述
    calendar_desc = _build_calendar_desc(holiday_dates, manual_event, predict_date_str)

    user_msg = f"""菜品：{dish}

天气信息：
{weather_text}

日历维度：{calendar_desc}

人工事件：{manual_event if manual_event else '无'}

只输出一个紧凑 JSON 对象，不要任何解释。"""

    # ===== 日志 =====
    logger.info(
        "[LLM] ===== 单菜品请求 =====\n"
        "  dish=%s\n"
        "  city=%s\n"
        "  weather_text_len=%d\n"
        "  weather_text=%s\n"
        "  holiday_dates=%s\n"
        "  predict_date_str=%s\n"
        "  calendar_desc=%s\n"
        "  manual_event=%s\n"
        "  prompt_preview=%s",
        dish, city, len(weather_text), weather_text[:100] if weather_text else "",
        holiday_dates, predict_date_str, calendar_desc, manual_event,
        user_msg[:400].replace("\n", " / "),
    )

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.2,
        "max_tokens": 256,
        "chat_template_kwargs": {"enable_thinking": False},
        "extra_body": {"enable_thinking": False},
    }

    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json; charset=utf-8",
    }

    content = None
    for attempt in range(3):
        try:
            transport = httpx.HTTPTransport(retries=1)
            with httpx.Client(timeout=120, transport=transport) as client:
                r = client.post(API_URL, headers=headers, json=payload)
                logger.info("[LLM] HTTP status=%s dish=%s attempt=%d", r.status_code, dish, attempt + 1)
                r.raise_for_status()
                data = r.json()

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
            finish_reason = data.get("choices", [{}])[0].get("finish_reason", "")
            usage = data.get("usage", {})
            logger.info("[LLM] dish=%s finish_reason=%s usage=%s content_len=%d", dish, finish_reason, usage, len(content))

            if content and content.strip():
                break
            else:
                logger.warning("[LLM] content 为空, 重试 %d/3 dish=%s", attempt + 1, dish)
        except Exception as e:
            logger.exception("[LLM] 请求异常 attempt=%d dish=%s: %s", attempt + 1, dish, e)

    if not content or not content.strip():
        logger.warning("[LLM] 单菜品 LLM 失败 dish=%s，使用兜底", dish)
        return _fallback_result("LLM连续返回空内容，使用默认系数")

    _llm_cache_put(cache_key, content)
    _cache_raw_response(content, weather_text, holiday_dates, manual_event, city, dish)
    out = _finalize(dish, content)

    if out is None:
        logger.error("[LLM] 解析失败 dish=%s", dish)
        return _fallback_result("JSON解析全部失败，使用默认系数")

    logger.info(
        "[LLM] ===== 单菜品完成 =====\n"
        "  dish=%s multiplier=%.3f risk=%s\n"
        "  risk_factors=%s\n"
        "  note=%s",
        dish, out["multiplier"], out["risk_level"], out["risk_factors"], out["note"],
    )
    return out


# ========== 按菜品分别预测（唯一主入口） ==========

def analyze_by_dishes(dish_list, weather_text: str = "", holiday_dates: list = None, manual_event: str = "", city: str = "") -> dict:
    seen = []
    for d in dish_list or []:
        d = _norm_dish_name(d)
        if d and d not in seen:
            seen.append(d)

    logger.info("[LLM] analyze_by_dishes 实际入参菜品 count=%d dishes=%s city=%s", len(seen), seen, city)

    if not seen:
        logger.warning("[LLM] 上传菜品为空，不预测。")
        return {"by_dish": {}, "dishes": []}

    by_dish = {}
    max_workers = min(len(seen), LLM_CACHE_MAX_WORKERS)
    logger.info("[LLM] 并发批处理开始：菜品数=%d 并发线程=%d", len(seen), max_workers)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {
            ex.submit(_call_llm_for_dish, dish, weather_text, holiday_dates or [], manual_event or "", city): dish
            for dish in seen
        }
        for fut in as_completed(futures):
            dish = futures[fut]
            try:
                by_dish[dish] = fut.result()
            except Exception as e:
                logger.exception("[LLM] 并发请求异常 dish=%s: %s", dish, e)
                by_dish[dish] = _fallback_result(f"并发异常:{e}")

    return {"by_dish": by_dish, "dishes": seen}


# ==================== 菜品配方（BOM）批量生成 ====================

RECIPE_SYSTEM_PROMPT = """你是餐饮配方与成本核算专家，熟悉中式餐饮常见菜品的投料标准。
输入：一批菜品名称。
输出：仅输出一个紧凑 JSON 对象，不要 markdown，不要解释，不要思考过程，不要换行，不要注释。

严格输出格式（用量与单价必须量纲一致）：
{"dishes":[{"name":"菜品名","items":[{"ingredient":"食材名","qty":0.15,"unit":"kg","unit_price":24.0,"category":"肉类"}]}]}

【核心约束：单价与用量统一以 kg / L 计】
- unit 只能取 "kg" 或 "L" 两种：固体/干货/生鲜/禽蛋一律用 kg，液体（油、酱油、醋、料酒、水等）用 L。严禁使用 g、ml、个、只、片、份。
- qty 为该食材「做一份菜品」的用量，单位必须与 unit 一致（kg 或 L），必须大于 0。
- unit_price 为该食材的市场采购单价，单位必须与 unit 一致：kg 对应「元/千克」，L 对应「元/升」。按中国大陆近两年普通餐饮采购价估算，取合理中位值。
- 计数类食材请换算成重量：鸡蛋≈0.05kg/个、鸭蛋≈0.07kg/个；葱/姜/蒜/香菜等少量调料按实际克数换算为 kg（如葱 0.01kg）。
- 合理单价参考（元/kg 或 元/L）：猪肉/牛肉 20~60、鸡胸肉 16~22、大米 5~7、食用油 10~14、酱油 8~12、青菜 3~8、鸡蛋约 10~14（按 kg 计，即 0.05kg≈0.5~0.7元/个）。

其他规则：
- 每个菜品给 3~8 个主要食材（含主料、配料、调料），不要给水电费。
- category 只能取：蔬菜、肉类、禽蛋、水产、豆制品、菌菇、米面、调料、粮油、干货、其他。
- 食材名用通用标准名（如"鸡胸肉"而非"鸡肉(切块)"），同种食材在不同菜品中名称必须一致。
"""

# 配方/采购统一使用 kg / L 两种量纲；LLM 输出若带 g/ml 会在校验时自动归一化。
# 计数单位（个/只/片/份）因无法确定单品重量，默认拒绝（LLM 应按提示词换算为 kg）。
_UNIT_RANGES = {
    "kg": (0.001, 5.0), "L": (0.001, 5.0),
}
_VALID_UNITS = set(_UNIT_RANGES.keys())
_VALID_CATEGORIES = {"蔬菜", "肉类", "禽蛋", "水产", "豆制品", "菌菇", "米面", "调料", "粮油", "干货", "其他"}
_PRICE_RANGE = (0.0, 2000.0)


def _norm_unit_for_llm(qty: float, unit: str):
    """把 LLM 返回的 g/ml/计数单位 归一化为 kg/L；返回 None 表示无法挽救。"""
    from guibeicai.web._db import canonicalize_unit
    res = canonicalize_unit(qty, unit, fallback_count_weight_kg=None)
    if res["is_count"]:
        return None
    # 仍然不在 kg/L 白名单的，视为无法挽救
    if res["unit"] not in _VALID_UNITS:
        return None
    return res


def _recipe_cache_key(dish_names: list, model: str) -> str:
    return "recipe_v2_" + hashlib.md5(("|".join(sorted(dish_names)) + "|" + model).encode("utf-8")).hexdigest()


def _recipe_max_tokens(n: int) -> int:
    """按批次菜品数估算所需 token，避免长批次被 max_tokens 截断导致整批解析失败。"""
    return min(8192, max(2048, 700 * max(1, n)))


def _validate_recipe_item(item: dict):
    """校验单条食材，返回规范化 dict；不合法返回 None。

    量纲统一：g→kg、ml→L（数量级相应缩放），个/只/片/份等计数单位无法自动
    确定单品重量，按不合法处理，避免采购单出现与 kg 混用的无意义数量。
    """
    try:
        ing = str(item.get("ingredient", "")).strip()
        if not ing or len(ing) > 30:
            return None
        qty = float(item.get("qty", 0))
        unit = str(item.get("unit", "")).strip()
        normed = _norm_unit_for_llm(qty, unit)
        if normed is None:
            return None
        qty, unit = normed["quantity"], normed["unit"]
        lo, hi = _UNIT_RANGES[unit]
        if not (lo <= qty <= hi):
            return None
        price = float(item.get("unit_price", 0) or 0)
        if not (_PRICE_RANGE[0] <= price <= _PRICE_RANGE[1]):
            return None
        category = str(item.get("category", "")).strip()
        if category not in _VALID_CATEGORIES:
            category = "其他"
        return {
            "ingredient_name": ing,
            "quantity": round(qty, 4),
            "unit": unit,
            "unit_price": round(price, 2),
            "category": category,
        }
    except Exception:
        return None


def _salvage_recipe_dishes(content: str):
    """从可能被截断的批量响应中，按括号平衡提取完整菜品对象，丢弃尾部未闭合残片。

    即使 JSON 因 max_tokens 不足而被截断（没有结尾的 ]），也能抢救出前面已完整输出的菜品。
    """
    try:
        idx = content.find('"dishes"')
        if idx == -1:
            return None
        arr_start = content.find("[", idx)
        if arr_start == -1:
            return None
        dishes = []
        i = arr_start + 1
        n = len(content)
        depth = 0
        obj_start = -1
        while i < n:
            c = content[i]
            if c == "{":
                if depth == 0:
                    obj_start = i
                depth += 1
            elif c == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and obj_start != -1:
                        chunk = content[obj_start:i + 1]
                        try:
                            obj = json.loads(chunk)
                            if isinstance(obj, dict) and obj.get("name"):
                                dishes.append(obj)
                        except Exception:
                            pass
                        obj_start = -1
            i += 1
        if dishes:
            return {"dishes": dishes}
        return None
    except Exception:
        return None


def _parse_recipe_with_repair(content: str):
    """用 json_repair 直接修复原始配方响应（不经截断清洗），返回 {"dishes":[...]} 或 None。"""
    try:
        from json_repair import repair_json
        obj = repair_json(content, return_objects=True)
        if isinstance(obj, dict) and isinstance(obj.get("dishes"), list):
            return {"dishes": obj["dishes"]}
        if isinstance(obj, list):
            return {"dishes": obj}
    except Exception:
        pass
    return None


def _parse_recipe_batch(content: str) -> dict:
    """解析批量配方响应：标准策略 → json_repair 直接修复 → 数组/截取 → 截断抢救。"""
    result = _parse_with_strategies(content)
    if isinstance(result, dict) and isinstance(result.get("dishes"), list):
        return {"dishes": result["dishes"]}
    repaired = _parse_recipe_with_repair(content)
    if repaired:
        return repaired
    cleaned = _clean_llm_json(content)
    try:
        arr = json.loads(cleaned)
        if isinstance(arr, list):
            return {"dishes": arr}
    except Exception:
        pass
    try:
        idx = cleaned.find('"dishes"')
        if idx != -1:
            start = cleaned.find("[", idx)
            end = cleaned.rfind("]")
            if start != -1 and end > start:
                arr = json.loads(cleaned[start:end + 1])
                if isinstance(arr, list):
                    return {"dishes": arr}
    except Exception:
        pass
    salvaged = _salvage_recipe_dishes(content)
    if salvaged:
        return salvaged
    return {}


def _normalize_batch_result(parsed: dict, dish_names: list) -> dict:
    """按请求菜名对齐模型返回，并逐条校验食材。"""
    raw_by_name = {}
    for d in parsed.get("dishes", []):
        if not isinstance(d, dict):
            continue
        nm = str(d.get("name", "")).strip()
        items = d.get("items") or []
        if nm and isinstance(items, list):
            raw_by_name[nm] = items

    out = {}
    for req in dish_names:
        items = raw_by_name.get(req)
        if items is None:
            for k, v in raw_by_name.items():
                if k.replace(" ", "") == req.replace(" ", ""):
                    items = v
                    break
        if items is None:
            continue
        cleaned = [_v for _v in (_validate_recipe_item(it) for it in items) if _v]
        if cleaned:
            out[req] = cleaned
        else:
            logger.warning("[LLM-BOM] 菜品「%s」食材全部未通过校验，视为失败", req)
    return out


def _call_llm_for_recipe_batch(dish_names: list) -> dict:
    """请求一批菜品的配方，返回 {菜名: [校验后的食材列表]}。失败返回 {}。"""
    if not API_KEY:
        logger.error("[LLM-BOM] API_KEY 未设置")
        return {}

    cache_key = _recipe_cache_key(dish_names, MODEL)
    cached = _llm_cache_get(cache_key)
    if cached is not None:
        parsed = _parse_recipe_batch(cached)
        if parsed:
            logger.info("[LLM-BOM] 缓存命中 %d 菜，跳过 API", len(dish_names))
            return _normalize_batch_result(parsed, dish_names)

    user_msg = "请为以下菜品分别给出配方（每份用量与市场采购单价）：\n" + "\n".join(f"- {d}" for d in dish_names)
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": RECIPE_SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.3,
        "max_tokens": _recipe_max_tokens(len(dish_names)),
        "chat_template_kwargs": {"enable_thinking": False},
        "extra_body": {"enable_thinking": False},
    }
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json; charset=utf-8",
    }

    content = None
    for attempt in range(3):
        try:
            transport = httpx.HTTPTransport(retries=1)
            with httpx.Client(timeout=180, transport=transport) as client:
                r = client.post(API_URL, headers=headers, json=payload)
                if r.status_code == 429:
                    logger.warning("[LLM-BOM] 429 限流，退避重试 %d/3", attempt + 1)
                    time.sleep(5 + attempt * 5)
                    continue
                r.raise_for_status()
                data = r.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""
            if content.strip():
                break
            logger.warning("[LLM-BOM] content 为空，重试 %d/3", attempt + 1)
            time.sleep(2)
        except Exception as e:
            logger.exception("[LLM-BOM] 请求异常 attempt=%d: %s", attempt + 1, e)
            time.sleep(3)

    if not content or not content.strip():
        return {}

    parsed = _parse_recipe_batch(content)
    if not parsed:
        logger.error("[LLM-BOM] 解析失败，批次首菜: %s", dish_names[:1])
        return {}
    _llm_cache_put(cache_key, content)
    return _normalize_batch_result(parsed, dish_names)


def generate_recipes_for_dishes(dish_names: list, batch_size: int = 8, max_workers: int = 5,
                                progress_cb=None, force: bool = False) -> dict:
    """
    批量生成菜品配方（每份食材、用量、市场估价）。

    :param dish_names: 菜品名称列表
    :param batch_size: 每批请求的菜品数（合并请求可显著降低调用次数）
    :param progress_cb: 进度回调 progress_cb(done, total)
    :param force: True 时忽略缓存
    :return: {"ok": {菜名: [items]}, "failed": [菜名], "total": int}
    """
    names = []
    for d in dish_names or []:
        d = str(d or "").strip()
        if d and d not in names:
            names.append(d)
    if not names:
        return {"ok": {}, "failed": [], "total": 0}

    ok_map = {}
    pending = list(names)
    if not force:
        still = []
        for nm in pending:
            single = _llm_cache_get(_recipe_cache_key([nm], MODEL))
            parsed = _parse_recipe_batch(single) if single else {}
            normalized = _normalize_batch_result(parsed, [nm]) if parsed else {}
            if normalized.get(nm):
                ok_map[nm] = normalized[nm]
            else:
                still.append(nm)
        pending = still

    if not pending:
        if progress_cb:
            progress_cb(len(names), len(names))
        return {"ok": ok_map, "failed": [], "total": len(names)}

    batches = [pending[i:i + batch_size] for i in range(0, len(pending), batch_size)]
    logger.info("[LLM-BOM] 开始生成：菜品 %d 个，分 %d 批，并发 %d", len(pending), len(batches), max_workers)

    done = len(ok_map)
    failed = []

    def _run_batch(batch):
        return batch, _call_llm_for_recipe_batch(batch)

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(batches)))) as ex:
        futures = [ex.submit(_run_batch, b) for b in batches]
        for fut in as_completed(futures):
            batch, res = fut.result()
            for nm in batch:
                if res.get(nm):
                    ok_map[nm] = res[nm]
                else:
                    failed.append(nm)
            done += len(batch)
            if progress_cb:
                try:
                    progress_cb(min(done, len(names)), len(names))
                except Exception:
                    pass

    if failed:
        retry_list = list(failed)
        failed = []
        logger.info("[LLM-BOM] 降级逐个重试 %d 个菜品", len(retry_list))
        for nm in retry_list:
            res = _call_llm_for_recipe_batch([nm])
            if res.get(nm):
                ok_map[nm] = res[nm]
            else:
                failed.append(nm)

    logger.info("[LLM-BOM] 完成：成功 %d，失败 %d", len(ok_map), len(failed))
    return {"ok": ok_map, "failed": failed, "total": len(names)}


# ========== 旧接口 ==========

def analyze_context(weather_text: str, holiday_dates: list = None, manual_event: str = "") -> dict:
    raise RuntimeError(
        "analyze_context 已移除。必须改用 "
        "analyze_by_dishes(df['关联菜品'], weather_text=..., holiday_dates=..., manual_event=..., city=...)"
    )


if __name__ == "__main__":
    res = analyze_by_dishes(
        dish_list=["水煮鱼", "宫保鸡丁"],
        weather_text="09-04 均温25.9℃ 降水0mm 风力12级",
        holiday_dates=[],
        manual_event="",
        city="南宁市",
    )
    for dish, r in res["by_dish"].items():
        print(dish, r["multiplier"], r["risk_level"], r["note"], r["risk_factors"])