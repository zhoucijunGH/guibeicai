"""桂备菜 · 批量数据导入（数据初始化 / 新客户快速录入）

提供幂等的 CSV 导入函数，覆盖上线割接时最痛的「手工逐条录入」缺口：

- 门店（stores）
- 菜品配方 / BOM（recipes）：同时自动创建菜品与食材主数据，无需依赖大模型

（门店导入支持 bind_user_id，渠道商导入自身门店清单时自动归属）。

所有导入函数均接受 pandas.DataFrame（由页面层用 pd.read_csv(..., dtype=str) 读取，
避免类型自动推断），返回结构化结果：
    {"created": int, "updated": int, "skipped": int, "errors": [str, ...]}
导入过程对单行错误做容错：记录错误并跳过，不中断整批。
"""

import pandas as pd

from guibeicai.web import _db as db
from guibeicai.web._db import normalize_name, canonicalize_unit

# ==================== 表头别名（中/英都能认） ====================
_STORE_COLS = {
    "name": ["门店", "门店名称", "名称", "name", "store", "store_name"],
    "province": ["省份", "province"],
    "city": ["城市", "city"],
    "owner_email": ["老板邮箱", "邮箱", "email", "owner_email"],
    "wecom_webhook": ["企微", "企业微信", "webhook", "wecom", "wecom_webhook"],
    "auto_predict": ["自动预测", "参与自动预测", "auto_predict"],
    "auto_predict_time": ["预测时间", "每日预测时间", "auto_predict_time"],
    "auto_predict_target_offset": ["预测目标日", "目标日", "target_offset", "auto_predict_target_offset"],
}

_RECIPE_COLS = {
    "store": ["门店", "门店名称", "store", "store_name"],
    "dish": ["菜品", "菜品名称", "dish", "dish_name"],
    "standard_price": ["菜品单价", "售价", "standard_price", "price"],
    "ingredient": ["食材", "食材名称", "ingredient", "ingredient_name"],
    "category": ["品类", "category"],
    "quantity": ["每份用量", "用量", "quantity", "qty"],
    "unit": ["单位", "unit"],
    "unit_price": ["食材单价", "单价", "unit_price", "price"],
}


def _map_cols(df, alias_map: dict) -> dict:
    """把 DataFrame 的列映射到标准字段名；找不到的字段返回 None。"""
    lower = {str(c).strip().lower(): c for c in df.columns}
    out = {}
    for std, aliases in alias_map.items():
        found = None
        for a in aliases:
            if a.lower() in lower:
                found = lower[a.lower()]
                break
        out[std] = found
    return out


def _num(val, default=0.0):
    try:
        if val is None or str(val).strip() == "":
            return default
        return float(str(val).replace(",", "").replace("¥", "").strip())
    except Exception:
        return default


def _store_id_map():
    return {s["name"].strip(): s["id"] for s in db.get_all_stores(active_only=False)}


# ==================== 1. 门店 ====================
def import_stores_csv(df: "pd.DataFrame", bind_user_id: int = None) -> dict:
    """导入门店。同 (省,市,名) 视为更新。

    bind_user_id：若传入（渠道商导入自身门店清单），导入的每家门店自动绑定到该账号，
    使渠道只能在其权限范围内管理这些门店。
    """
    cols = _map_cols(df, _STORE_COLS)
    res = {"created": 0, "updated": 0, "skipped": 0, "errors": []}
    if not cols["name"]:
        res["errors"].append("缺少「门店/name」列")
        return res
    for _, row in df.iterrows():
        name = str(row[cols["name"]]).strip()
        if not name or name in ("nan", "None"):
            res["skipped"] += 1
            continue
        try:
            province = str(row[cols["province"]]).strip() if cols["province"] else ""
            city = str(row[cols["city"]]).strip() if cols["city"] else ""
            if not city or city in ("nan", "None"):
                res["errors"].append(f"门店「{name}」缺少城市，已跳过")
                res["skipped"] += 1
                continue
            owner_email = str(row[cols["owner_email"]]).strip() if cols["owner_email"] else ""
            wecom = str(row[cols["wecom_webhook"]]).strip() if cols["wecom_webhook"] else ""
            ap_raw = str(row[cols["auto_predict"]]).strip().lower() if cols["auto_predict"] else ""
            auto_predict = 0 if ap_raw in ("0", "否", "no", "false") else 1
            ap_time = str(row[cols["auto_predict_time"]]).strip() if cols["auto_predict_time"] else "06:00"
            if not ap_time or ap_time in ("nan", "None"):
                ap_time = "06:00"
            offset_raw = str(row[cols["auto_predict_target_offset"]]).strip() if cols["auto_predict_target_offset"] else ""
            offset = {"今天": 0, "明天": 1, "后天": 2}.get(offset_raw, 1)
            if offset_raw in ("0", "1", "2"):
                offset = int(offset_raw)

            existed = db.get_all_stores(active_only=False)
            is_exist = any(s["name"].strip() == name and s["city"] == city and s["province"] == province for s in existed)
            sid = db.upsert_store(name, city, province, owner_email=owner_email, wecom_webhook=wecom,
                                  auto_predict=auto_predict, auto_predict_target_offset=offset, auto_predict_time=ap_time)
            if bind_user_id:
                try:
                    db.add_user_store(bind_user_id, sid)
                except Exception:
                    pass
            if is_exist:
                res["updated"] += 1
            else:
                res["created"] += 1
        except Exception as e:
            res["errors"].append(f"门店「{name}」导入失败：{e}")
    return res


# ==================== 2. 菜品配方 / BOM（自动建菜品与食材） ====================
def import_recipes_csv(df: "pd.DataFrame") -> dict:
    """导入菜品配方。按 (门店, 菜品) 分组，整体替换该菜品配方；
    菜品不存在则自动创建；食材主数据由 replace_recipe_for_dish 自动归位。

    依赖门店已存在（先导入门店）。
    """
    cols = _map_cols(df, _RECIPE_COLS)
    res = {"created": 0, "updated": 0, "skipped": 0, "errors": []}
    missing = [k for k in ("store", "dish", "ingredient", "quantity") if not cols[k]]
    if missing:
        res["errors"].append(f"配方表缺少列：{missing}")
        return res

    stores = _store_id_map()
    # 先逐行解析，构建 (store_id, dish) -> items，并创建菜品
    groups = {}  # (store_id, dish_norm) -> {"dish_id", "dish_name", "items":[]}
    for _, row in df.iterrows():
        store_name = str(row[cols["store"]]).strip()
        dish_name = str(row[cols["dish"]]).strip()
        ing_name = str(row[cols["ingredient"]]).strip()
        if not store_name or store_name in ("nan", "None"):
            res["skipped"] += 1
            continue
        if not dish_name or dish_name in ("nan", "None"):
            res["errors"].append("存在空菜品名，已跳过该行")
            res["skipped"] += 1
            continue
        if not ing_name or ing_name in ("nan", "None"):
            res["errors"].append(f"菜品「{dish_name}」存在空食材名，已跳过该行")
            res["skipped"] += 1
            continue
        sid = stores.get(store_name)
        if sid is None:
            res["errors"].append(f"门店「{store_name}」在系统中不存在（请先导入门店），已跳过该行")
            res["skipped"] += 1
            continue
        qty = _num(row[cols["quantity"]], 0.0)
        if qty <= 0:
            res["errors"].append(f"菜品「{dish_name}」食材「{ing_name}」用量非正，已跳过")
            res["skipped"] += 1
            continue

        std_price = _num(row[cols["standard_price"]], 0.0) if cols["standard_price"] else 0.0
        did, is_new = db.upsert_dish_ex(sid, dish_name, category="", standard_price=std_price)
        if is_new:
            res["created"] += 1
        key = (sid, normalize_name(dish_name))
        if key not in groups:
            groups[key] = {"dish_id": did, "dish_name": dish_name, "items": []}
        unit_raw = str(row[cols["unit"]]).strip() if cols["unit"] else ""
        unit_price = _num(row[cols["unit_price"]], 0.0) if cols["unit_price"] else 0.0
        # 统一量纲为 kg / L（g→kg、ml→L）；计数单位（个/只/片/份）按默认 0.05 kg 换算
        norm = canonicalize_unit(qty, unit_raw, fallback_count_weight_kg=0.05)
        if norm["is_count"] and not unit_raw:
            res["errors"].append(f"菜品「{dish_name}」食材「{ing_name}」单位为空，已按 kg 处理")
        groups[key]["items"].append({
            "ingredient_name": ing_name,
            "quantity": norm["quantity"],
            "unit": norm["unit"],
            "unit_price": unit_price,
            "category": str(row[cols["category"]]).strip() if cols["category"] else "",
        })

    # 整批替换配方（覆盖导入，source=import）
    for key, g in groups.items():
        try:
            db.replace_recipe_for_dish(g["dish_id"], g["items"], source="import")
            res["updated"] += 1
        except Exception as e:
            res["errors"].append(f"菜品「{g['dish_name']}」配方写入失败：{e}")
    return res


# ==================== 模板（供页面下载） ====================
def template_stores():
    import pandas as pd
    return pd.DataFrame([
        {"门店": "南宁演示店", "省份": "广西壮族自治区", "城市": "南宁市",
         "老板邮箱": "boss@demo.com", "企微Webhook": "", "自动预测": "是",
         "预测时间": "06:00", "预测目标日": "明天"},
    ])


def template_recipes():
    import pandas as pd
    return pd.DataFrame([
        {"门店": "南宁演示店", "菜品": "老友粉", "菜品单价": 15, "食材": "米粉", "品类": "米面", "每份用量": 0.2, "单位": "kg", "食材单价": 6},
        {"门店": "南宁演示店", "菜品": "老友粉", "菜品单价": 15, "食材": "酸笋", "品类": "干货", "每份用量": 0.05, "单位": "kg", "食材单价": 12},
        {"门店": "南宁演示店", "菜品": "柠檬鸭", "菜品单价": 48, "食材": "鸭肉", "品类": "肉类", "每份用量": 0.5, "单位": "kg", "食材单价": 22},
    ])
