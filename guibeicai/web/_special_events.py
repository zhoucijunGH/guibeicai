"""特殊事件配置加载（寒暑假/本地民俗节日/店铺活动），按城市匹配"""

import json
import logging
from pathlib import Path
from datetime import date, datetime, timedelta
import pandas as pd

logger = logging.getLogger(__name__)

# 多候选路径，确保无论怎么运行都能找到 config/special_events.json
def _find_config():
    here = Path(__file__).resolve()
    candidates = [
        here.parent.parent.parent / "config" / "special_events.json",
        here.parent.parent / "config" / "special_events.json",
        Path("config/special_events.json"),
        Path("special_events.json"),
    ]
    for p in candidates:
        if p.exists():
            return p
    return candidates[0]  # 返回第一个候选，让报错信息有意义

_CONFIG_PATH = _find_config()


def _normalize_city(city: str) -> str:
    """南宁 / 南宁市 归一化"""
    if not city:
        return ""
    city = city.strip()
    if city.endswith("市"):
        return city[:-1]
    return city


def _city_match(event_city: str, store_city: str) -> bool:
    """匹配城市：支持 'all' / 精确匹配 / 去'市'匹配"""
    if not event_city:
        return False
    event_city = event_city.strip()
    store_city = (store_city or "").strip()
    if event_city.lower() == "all":
        return True
    return _normalize_city(event_city) == _normalize_city(store_city)


def _load_config() -> list:
    if not _CONFIG_PATH.exists():
        logger.error("[special_events] 配置文件不存在: %s (候选已检查)", _CONFIG_PATH)
        return []
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        events = data.get("events", [])
        logger.info("[special_events] 加载成功 path=%s count=%d", _CONFIG_PATH, len(events))
        return events
    except Exception as e:
        logger.exception("[special_events] 读取/解析失败: %s err=%s", _CONFIG_PATH, e)
        return []


def get_special_events_for_city(city: str) -> list:
    """返回该城市匹配的所有特殊事件（含 city='all' 的通用事件）"""
    all_events = _load_config()
    result = []
    for ev in all_events:
        if _city_match(ev.get("city", ""), city):
            result.append(ev)
    return result


def check_special_event(d: date, city: str = "") -> str:
    """
    检查某日期是否命中特殊事件。
    优先级：法定节假日（chinese_calendar 名称） > 配置里的寒暑假/民俗/活动
    返回：命中的事件 label，未命中返回空字符串。
    """
    # 1) 法定节假日（含国务院公布的放假区间）
    try:
        from chinese_calendar import get_holiday_detail
        on_holiday, hname = get_holiday_detail(d)
        if on_holiday and hname:
            return hname
    except Exception:
        pass

    # 2) 配置里的寒暑假 / 本地民俗 / 店铺活动
    events = get_special_events_for_city(city)
    d_str = d.strftime("%m-%d")
    d_md = (d.month, d.day)

    for ev in events:
        if "date" in ev and ev["date"] == d_str:
            return ev["label"]
        elif "dates" in ev and isinstance(ev["dates"], list):
            if d_str in ev["dates"]:
                return ev["label"]
        elif "start" in ev and "end" in ev:
            try:
                sm, sd = map(int, ev["start"].split("-"))
                em, ed = map(int, ev["end"].split("-"))
                if sm <= em:
                    if (sm, sd) <= d_md <= (em, ed):
                        return ev["label"]
                else:
                    if d_md >= (sm, sd) or d_md <= (em, ed):
                        return ev["label"]
            except Exception:
                pass
    return ""


def get_special_events_text(dates: list, city: str) -> str:
    """返回日期列表中的特殊事件文本，供 LLM 使用"""
    lines = []
    for d in dates:
        label = check_special_event(d, city)
        if label:
            lines.append(f"{d.strftime('%m-%d')} {label}")
    return "；".join(lines) if lines else ""


def get_special_event_context(predict_date_str: str, city: str = "", manual_event: str = "") -> tuple:
    """
    给 LLM 调用方用。
    返回 (holiday_dates, merged_manual_event)
    """
    events = _load_config()
    matched_dates = []
    event_notes = []

    predict_date_str = (predict_date_str or "").strip()
    logger.info("[special_events] get_special_event_context 调用: predict_date=%s city=%s manual=%s events_count=%d",
                predict_date_str, city, manual_event, len(events))

    for ev in events:
        if not _city_match(ev.get("city", ""), city):
            continue

        # 收集该事件覆盖的所有日期
        ev_dates = []
        if "date" in ev and ev["date"]:
            ev_dates.append(ev["date"].strip())
        elif "dates" in ev and isinstance(ev["dates"], list):
            ev_dates.extend([str(d).strip() for d in ev["dates"] if d])
        elif "start" in ev and "end" in ev:
            try:
                s = datetime.strptime(ev["start"], "%m-%d")
                e = datetime.strptime(ev["end"], "%m-%d")
                if e >= s:
                    cur = s
                    while cur <= e:
                        ev_dates.append(cur.strftime("%m-%d"))
                        cur += timedelta(days=1)
            except Exception:
                pass

        # 预测日是否命中
        hit = predict_date_str and predict_date_str in ev_dates
        if not hit:
            continue

        # 命中：收集日期
        matched_dates.extend(ev_dates)

        # 收集事件说明
        label = ev.get("label", "")
        note = ev.get("note", "")
        if label and note:
            event_notes.append(f"{label}（{note}）")
        elif label:
            event_notes.append(label)
        elif note:
            event_notes.append(note)

        logger.info("[special_events] 命中! city=%s date=%s ev=%s ev_dates=%s", city, predict_date_str, label, ev_dates)

    # 去重
    seen = set()
    holiday_dates = []
    for d in matched_dates:
        if d not in seen:
            seen.add(d)
            holiday_dates.append(d)

    event_notes = list(dict.fromkeys(event_notes))

    # 合并 manual_event
    manual_parts = []
    if manual_event:
        manual_parts.append(manual_event.strip())
    if event_notes:
        manual_parts.append("；".join(event_notes))

    merged_manual = "；".join(manual_parts)

    logger.info("[special_events] 返回: holiday_dates=%s manual_event=%s", holiday_dates, merged_manual)

    return holiday_dates, merged_manual


def _expand_event_dates(ev: dict) -> set:
    """展开事件覆盖的 (月, 日) 集合（含跨年环绕）。仅配置事件，不含法定节假日。"""
    out = set()
    # 单日
    if ev.get("date"):
        try:
            out.add(tuple(int(x) for x in str(ev["date"]).split("-")[:2]))
        except Exception:
            pass
    # 多日列表
    if isinstance(ev.get("dates"), list):
        for x in ev["dates"]:
            try:
                out.add(tuple(int(x) for x in str(x).split("-")[:2]))
            except Exception:
                pass
    # 区间 start~end（支持跨年环绕）
    if ev.get("start") and ev.get("end"):
        try:
            s = datetime.strptime(str(ev["start"]), "%m-%d")
            e = datetime.strptime(str(ev["end"]), "%m-%d")
            if e >= s:
                n = (e - s).days
            else:
                # 跨年：start→年末 + 年初→end
                year_end = datetime(s.year, 12, 31)
                n = (year_end - s).days + (e - datetime(e.year, 1, 1)).days + 1
            for i in range(n + 1):
                d = s + timedelta(days=i)
                out.add((d.month, d.day))
        except Exception:
            pass
    return out


def build_special_event_series(city: str, ds_index, manual_event_dates=None) -> "pd.DataFrame":
    """
    构建 Prophet 外生变量 special_event 序列。

    对匹配城市（city 或 all）的配置事件日期、以及运行时手动事件日期置 1.0，其余 0.0。
    不含法定节假日（由 Prophet add_country_holidays('CN') 处理，避免重复计数）。

    参数:
        city: 门店城市，用于匹配 config/special_events.json
        ds_index: 历史+未来合并的日期索引（list / DatetimeIndex / Series）
        manual_event_dates: 运行时手动事件日期（如预测窗口 predict_dates），置 1.0；为 None 则忽略
    返回:
        DataFrame[['ds', 'special_event']]，ds 为归一化日期，special_event 为 float
    """
    idx = pd.DatetimeIndex(pd.to_datetime(ds_index, errors="coerce").normalize().dropna().unique())
    events = get_special_events_for_city(city)
    event_mds = set()
    for ev in events:
        event_mds |= _expand_event_dates(ev)

    rows = []
    for d in idx:
        flag = 1.0 if (d.month, d.day) in event_mds else 0.0
        rows.append((d, flag))
    df = pd.DataFrame(rows, columns=["ds", "special_event"])

    if manual_event_dates is not None:
        md = pd.DatetimeIndex(pd.to_datetime(manual_event_dates, errors="coerce").normalize().dropna().unique())
        if len(md) > 0:
            df.loc[df["ds"].isin(md), "special_event"] = 1.0
    return df


if __name__ == "__main__":
    print("=== 测试南宁市 09-04 ===")
    hd, me = get_special_event_context("09-04", "南宁市", "")
    print(f"  holiday_dates={hd}, manual_event={me}")

    print("=== 测试南宁 09-04 ===")
    hd, me = get_special_event_context("09-04", "南宁", "")
    print(f"  holiday_dates={hd}, manual_event={me}")

    print("=== 测试南宁市 03-20 ===")
    hd, me = get_special_event_context("03-20", "南宁市", "")
    print(f"  holiday_dates={hd}, manual_event={me}")

    print("=== check_special_event ===")
    print(f"  09-04 南宁市: {check_special_event(date(2026, 9, 4), '南宁市')}")
    print(f"  09-04 南宁: {check_special_event(date(2026, 9, 4), '南宁')}")