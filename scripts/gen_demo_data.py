"""
桂备菜 - 3 门店模拟销售数据生成器
=================================
用途：为演示/预测实测生成贴近真实餐饮场景的数据。
特征：
  - 3 个门店，各约 100 个菜品（共约 300 个菜品主数据）
  - 时间范围：近半年（约 180 天）
  - 销量含：基线 + 周末系数(↑) + 节假日系数(↑) + 天气效应(温度/降雨) + 缓慢增长趋势 + 噪声
  - 同步生成 date_factors（温度/天气文本/节假日），使数据自洽
  - 生成 recipes（菜品→食材），供食材级预测/采购单演示
  - 生成最近 14 天 actual_sales，供回测/预测对比演示
落库：追加到现有 guibeicai.db，不破坏已有门店数据。可重复运行（幂等覆盖）。

依赖：仅 sqlite3 / random / datetime（标准库），可离线运行。
"""
import sqlite3
import random
import os
from datetime import date, timedelta
from pathlib import Path

DB_PATH = str(Path(__file__).resolve().parent.parent / "data" / "guibeicai.db")

# 2026 年中国法定节假日（仅取本数据时间窗 2026-03 ~ 2026-09 内的区间）
HOLIDAYS_2026 = []
for d0, d1 in [("2026-04-04", "2026-04-06"),   # 清明
               ("2026-05-01", "2026-05-05"),   # 劳动
               ("2026-06-19", "2026-06-21")]:  # 端午
    a = date.fromisoformat(d0); b = date.fromisoformat(d1)
    while a <= b:
        HOLIDAYS_2026.append(a); a += timedelta(days=1)
HOLIDAY_SET = set(HOLIDAYS_2026)


def is_holiday(d: date) -> bool:
    return d in HOLIDAY_SET


# ============ 门店 ============
STORES = [
    ("南宁青秀店", "广西", "南宁市"),
    ("柳州城中店", "广西", "柳州市"),
    ("桂林象山店", "广西", "桂林市"),
]

# ============ 菜品池（100 个）============
# (名称, 分类, 基准价, 温度系数, 降雨系数)
# 温度系数>0：天热卖得多（凉菜/饮品）；<0：天热卖得少（热菜/汤羹）
# 降雨系数>0：雨天卖得多（汤羹/烧烤/热菜）；<0：雨天卖得少（凉菜/饮品）
DISH_POOL = [
    # 热菜
    ("宫保鸡丁", "热菜", 32, -0.3, 0.3), ("鱼香肉丝", "热菜", 30, -0.3, 0.3),
    ("麻婆豆腐", "热菜", 22, -0.3, 0.3), ("红烧肉", "热菜", 48, -0.4, 0.4),
    ("糖醋里脊", "热菜", 42, -0.2, 0.2), ("回锅肉", "热菜", 38, -0.3, 0.3),
    ("辣子鸡", "热菜", 46, -0.3, 0.3), ("干锅花菜", "热菜", 28, -0.2, 0.2),
    ("地三鲜", "热菜", 26, -0.2, 0.2), ("西红柿炒蛋", "热菜", 24, -0.1, 0.1),
    ("青椒肉丝", "热菜", 30, -0.3, 0.3), ("可乐鸡翅", "热菜", 36, -0.2, 0.2),
    ("清蒸鲈鱼", "热菜", 58, 0.1, 0.2), ("水煮鱼", "热菜", 56, -0.2, 0.3),
    ("酸菜鱼", "热菜", 52, -0.2, 0.3), ("毛血旺", "热菜", 50, -0.3, 0.4),
    ("蒜蓉西兰花", "热菜", 26, -0.1, 0.1), ("蚝油生菜", "热菜", 22, -0.1, 0.1),
    ("京酱肉丝", "热菜", 38, -0.3, 0.3), ("铁板牛肉", "热菜", 52, -0.3, 0.3),
    ("黑椒牛柳", "热菜", 54, -0.3, 0.3), ("咖喱鸡", "热菜", 36, -0.2, 0.2),
    ("红烧排骨", "热菜", 50, -0.4, 0.4), ("梅菜扣肉", "热菜", 46, -0.4, 0.4),
    ("啤酒鸭", "热菜", 44, -0.3, 0.3), ("干煸豆角", "热菜", 28, -0.2, 0.2),
    ("鱼香茄子", "热菜", 26, -0.2, 0.2), ("肉末茄子", "热菜", 26, -0.2, 0.2),
    # 凉菜
    ("拍黄瓜", "凉菜", 14, 0.8, -0.3), ("凉拌木耳", "凉菜", 18, 0.7, -0.3),
    ("口水鸡", "凉菜", 28, 0.5, -0.2), ("夫妻肺片", "凉菜", 32, 0.5, -0.2),
    ("皮蛋豆腐", "凉菜", 16, 0.7, -0.3), ("凉拌海带丝", "凉菜", 12, 0.8, -0.3),
    ("蒜泥白肉", "凉菜", 30, 0.4, -0.2), ("凉拌腐竹", "凉菜", 14, 0.7, -0.3),
    ("卤牛肉", "凉菜", 38, 0.3, -0.1), ("凉拌藕片", "凉菜", 16, 0.8, -0.3),
    ("酸辣土豆丝", "凉菜", 12, 0.8, -0.3), ("凉拌豆角", "凉菜", 14, 0.7, -0.3),
    # 汤羹
    ("西红柿蛋汤", "汤羹", 14, -0.6, 0.5), ("紫菜蛋花汤", "汤羹", 12, -0.6, 0.5),
    ("冬瓜排骨汤", "汤羹", 32, -0.6, 0.5), ("菌菇鸡汤", "汤羹", 38, -0.6, 0.5),
    ("酸辣汤", "汤羹", 16, -0.5, 0.5), ("西湖牛肉羹", "汤羹", 28, -0.6, 0.5),
    ("银耳莲子羹", "汤羹", 18, -0.4, 0.4), ("老鸭汤", "汤羹", 36, -0.6, 0.5),
    # 主食
    ("米饭", "主食", 3, 0.0, -0.1), ("蛋炒饭", "主食", 16, 0.1, -0.1),
    ("扬州炒饭", "主食", 22, 0.1, -0.1), ("牛肉面", "主食", 26, -0.1, 0.1),
    ("炸酱面", "主食", 20, -0.1, 0.1), ("葱油拌面", "主食", 18, -0.1, 0.1),
    ("鲜肉饺子", "主食", 28, -0.1, 0.1), ("小笼包", "主食", 24, -0.1, 0.1),
    ("皮蛋瘦肉粥", "主食", 14, -0.2, 0.1), ("小米粥", "主食", 8, -0.2, 0.1),
    ("八宝粥", "主食", 12, -0.1, 0.1), ("杂粮馒头", "主食", 6, 0.0, -0.1),
    # 饮品
    ("酸梅汤", "饮品", 10, 1.2, -0.2), ("柠檬水", "饮品", 12, 1.2, -0.2),
    ("冰镇可乐", "饮品", 8, 1.0, -0.1), ("鲜榨橙汁", "饮品", 16, 1.1, -0.2),
    ("椰汁", "饮品", 12, 1.0, -0.1), ("豆浆", "饮品", 6, 0.3, -0.1),
    ("奶茶", "饮品", 14, 0.8, -0.1), ("椰子水", "饮品", 18, 1.1, -0.2),
    # 小吃
    ("煎饼果子", "小吃", 12, 0.1, -0.2), ("肉夹馍", "小吃", 14, -0.1, 0.0),
    ("南瓜饼", "小吃", 10, 0.2, -0.1), ("麻团", "小吃", 8, 0.2, -0.1),
    ("春卷", "小吃", 12, 0.1, -0.1), ("驴打滚", "小吃", 14, 0.3, -0.1),
    # 海鲜
    ("蒜蓉粉丝虾", "海鲜", 58, 0.2, 0.1), ("香辣蟹", "海鲜", 88, -0.1, 0.3),
    ("油焖大虾", "海鲜", 68, 0.2, 0.1), ("清蒸扇贝", "海鲜", 48, 0.3, 0.0),
    ("辣炒花蛤", "海鲜", 42, 0.2, 0.1), ("椒盐虾", "海鲜", 62, 0.2, 0.1),
    ("红烧带鱼", "海鲜", 46, 0.1, 0.2),
    # 烧烤/锅
    ("烤羊肉串", "烧烤", 48, -0.2, 0.4), ("烤生蚝", "烧烤", 38, 0.1, 0.3),
    ("麻辣香锅", "烧烤", 52, -0.2, 0.4), ("冒菜", "烧烤", 36, -0.2, 0.4),
    ("砂锅粥", "烧烤", 28, -0.3, 0.4), ("关东煮", "烧烤", 22, -0.2, 0.3),
    # 甜品
    ("双皮奶", "甜品", 14, 0.5, -0.1), ("杨枝甘露", "甜品", 18, 0.6, -0.1),
    ("红豆沙", "甜品", 12, -0.2, 0.1), ("龟苓膏", "甜品", 14, 0.6, -0.1),
    ("芒果布丁", "甜品", 16, 0.6, -0.1),
]

# 基准日销量（按分类），再叠加随机
BASE_QTY = {
    "热菜": (18, 45), "凉菜": (25, 55), "汤羹": (15, 35), "主食": (50, 120),
    "饮品": (40, 90), "小吃": (20, 50), "海鲜": (10, 30), "烧烤": (25, 70), "甜品": (20, 45),
}

# ============ 食材库（用于 recipes）============
# (名称, 分类, 单位, 单价)
PANTRY = [
    ("鸡肉", "肉类", "kg", 18), ("猪肉", "肉类", "kg", 24), ("牛肉", "肉类", "kg", 56),
    ("羊肉", "肉类", "kg", 62), ("鸭肉", "肉类", "kg", 20), ("鸡蛋", "蛋奶", "kg", 12),
    ("虾", "水产", "kg", 68), ("鱼", "水产", "kg", 22), ("蟹", "水产", "kg", 90),
    ("扇贝", "水产", "kg", 48), ("花蛤", "水产", "kg", 16), ("带鱼", "水产", "kg", 20),
    ("黄瓜", "蔬菜", "kg", 6), ("番茄", "蔬菜", "kg", 7), ("土豆", "蔬菜", "kg", 4),
    ("青椒", "蔬菜", "kg", 8), ("西兰花", "蔬菜", "kg", 10), ("生菜", "蔬菜", "kg", 6),
    ("木耳", "蔬菜", "kg", 40), ("豆腐", "蔬菜", "kg", 5), ("茄子", "蔬菜", "kg", 6),
    ("白菜", "蔬菜", "kg", 4), ("莲藕", "蔬菜", "kg", 9), ("海带", "蔬菜", "kg", 8),
    ("腐竹", "蔬菜", "kg", 22), ("胡萝卜", "蔬菜", "kg", 4), ("洋葱", "蔬菜", "kg", 5),
    ("蒜", "调料", "kg", 14), ("姜", "调料", "kg", 12), ("大米", "主食", "kg", 6),
    ("面条", "主食", "kg", 8), ("粉条", "主食", "kg", 10), ("油", "调料", "L", 14),
    ("酱油", "调料", "L", 16), ("醋", "调料", "L", 12), ("辣椒", "调料", "kg", 20),
    ("可乐", "饮品", "L", 8), ("柠檬", "饮品", "kg", 14), ("酸梅", "饮品", "kg", 18),
    ("椰浆", "饮品", "L", 26), ("牛奶", "蛋奶", "L", 14), ("糖", "调料", "kg", 10),
]
PANTRY_BY_CAT = {}
for p in PANTRY:
    PANTRY_BY_CAT.setdefault(p[1], []).append(p)


def seasonal_temp(d: date) -> float:
    """广西亚热带：按月季节性均温 + 日噪声。"""
    month_temp = {3: 20, 4: 24, 5: 27, 6: 29, 7: 31, 8: 31, 9: 30}
    base = month_temp.get(d.month, 26)
    return round(base + random.uniform(-3, 3), 1)


def gen_rain(d: date) -> float:
    """5-9 月雨季，降雨概率更高。"""
    rainy_season = d.month in (5, 6, 7, 8, 9)
    if random.random() < (0.45 if rainy_season else 0.2):
        return round(random.uniform(2, 28), 1)
    return 0.0


def main():
    random.seed(20260903)  # 可复现
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    cur = con.cursor()

    end_date = date(2026, 9, 2)
    start_date = end_date - timedelta(days=179)  # 180 天
    total_days = (end_date - start_date).days + 1

    # ---- 门店 ----
    store_ids = {}
    for name, prov, city in STORES:
        row = cur.execute("SELECT id FROM stores WHERE name=?", (name,)).fetchone()
        if row:
            sid = row[0]
        else:
            cur.execute("INSERT INTO stores(name,province,city,status) VALUES(?,?,?,?)",
                        (name, prov, city, "active"))
            sid = cur.lastrowid
        store_ids[name] = sid
    print(f"门店: {store_ids}")

    # ---- 菜品 ----
    dish_lookup = {}  # (store_id, name) -> dish_id
    for sname, sid in store_ids.items():
        for (dname, cat, price, tcoef, rcoef) in DISH_POOL:
            price_j = round(price * random.uniform(0.92, 1.08), 1)
            row = cur.execute("SELECT id FROM dishes WHERE store_id=? AND name=?", (sid, dname)).fetchone()
            if row:
                did = row[0]
            else:
                cur.execute(
                    "INSERT INTO dishes(store_id,name,category,standard_price,status) VALUES(?,?,?,?,?)",
                    (sid, dname, cat, price_j, "active"))
                did = cur.lastrowid
            dish_lookup[(sid, dname)] = (did, cat, price_j, tcoef, rcoef)
    all_dish_ids = [v[0] for v in dish_lookup.values()]
    print(f"菜品主数据: {len(dish_lookup)} 条")

    # ---- 销量 / 天气 / 实际销量 ----
    sales_rows = []
    date_factor_rows = []
    actual_rows = []
    actual_cutoff = end_date - timedelta(days=13)  # 最近 14 天

    day_idx = 0
    d = start_date
    while d <= end_date:
        dow = d.weekday()  # 0=Mon .. 6=Sun
        weekend = 1.0
        if dow == 5:       weekend = 1.12   # 周五
        elif dow == 6:     weekend = 1.38   # 周六
        elif dow == 0:     weekend = 0.92   # 周一略低
        hol = is_holiday(d)
        holiday_f = 1.45 if hol else 1.0
        trend = 1.0 + 0.0009 * day_idx  # 半年缓慢增长 ~ +16%

        for sname, sid in store_ids.items():
            # 天气（每店每日略不同）
            temp = seasonal_temp(d)
            rain = gen_rain(d)
            wtext = f"均温{temp:.1f}℃ 降水{rain:.0f}mm"
            date_factor_rows.append((sid, d.isoformat(), wtext, temp, 1 if hol else 0, ""))

            for (dname, cat, price, tcoef, rcoef) in DISH_POOL:
                did, _, _, _, _ = dish_lookup[(sid, dname)]
                lo, hi = BASE_QTY[cat]
                base = random.uniform(lo, hi)
                weather_f = 1.0 + tcoef * (temp - 26) * 0.015 - (rcoef * 0.12 if rain > 8 else 0.0)
                noise = random.lognormvariate(0, 0.12)
                qty = base * weekend * holiday_f * weather_f * trend * noise
                # 约 2% 概率当日未售（售罄/未备）
                if random.random() < 0.02:
                    qty = 0.0
                qty = max(0, round(qty))
                revenue = round(qty * price, 2)
                sales_rows.append((sid, did, d.isoformat(), qty, price, revenue))

                # 最近 14 天实际销量
                if d >= actual_cutoff:
                    aq = max(0, round(qty * random.uniform(0.94, 1.06)))
                    waste = round(aq * random.uniform(0.0, 0.03), 1)
                    actual_rows.append((sid, did, d.isoformat(), aq, waste, "模拟实测"))

        d += timedelta(days=1)
        day_idx += 1

    # ---- 批量写入 ----
    cur.executemany(
        "INSERT OR REPLACE INTO sales_history(store_id,dish_id,sale_date,qty,unit_price,revenue) "
        "VALUES(?,?,?,?,?,?)", sales_rows)
    cur.executemany(
        "INSERT OR REPLACE INTO date_factors(store_id,factor_date,weather_text,temperature,is_holiday,special_event) "
        "VALUES(?,?,?,?,?,?)", date_factor_rows)
    cur.executemany(
        "INSERT OR REPLACE INTO actual_sales(store_id,dish_id,sale_date,actual_qty,actual_waste,notes) "
        "VALUES(?,?,?,?,?,?)", actual_rows)
    con.commit()
    print(f"sales_history 新增: {len(sales_rows)} 行")
    print(f"date_factors 新增: {len(date_factor_rows)} 行")
    print(f"actual_sales 新增: {len(actual_rows)} 行")

    # ---- recipes：先清后插（幂等）----
    cur.execute(f"DELETE FROM recipes WHERE dish_id IN ({','.join('?'*len(all_dish_ids))})", all_dish_ids)
    recipe_rows = []
    for (sid, dname), (did, cat, price, tcoef, rcoef) in dish_lookup.items():
        n = random.randint(2, 4)
        # 优先取与分类相关的食材，否则随机
        related = PANTRY_BY_CAT.get(cat, []) or PANTRY
        picks = random.sample(PANTRY, min(n, len(PANTRY)))
        # 确保含一个主料（肉类/水产/蔬菜）
        for ing in related:
            if ing not in picks:
                picks[0] = ing
                break
        for (iname, icat, iunit, iprice) in picks[:n]:
            qty = round(random.uniform(0.05, 0.35), 2)
            recipe_rows.append((did, iname, qty, iunit, iprice, icat))
    cur.executemany(
        "INSERT INTO recipes(dish_id,ingredient_name,quantity,unit,unit_price,category) VALUES(?,?,?,?,?,?)",
        recipe_rows)
    con.commit()
    print(f"recipes 新增: {len(recipe_rows)} 行")

    # ---- 验收统计 ----
    stats = cur.execute(
        "SELECT s.name, COUNT(DISTINCT d.id) dishes, COUNT(sh.id) sales, MIN(sh.sale_date), MAX(sh.sale_date) "
        "FROM stores s LEFT JOIN dishes d ON d.store_id=s.id "
        "LEFT JOIN sales_history sh ON sh.store_id=s.id "
        "WHERE s.id IN (%s) GROUP BY s.id" % ",".join("?"*len(store_ids)),
        list(store_ids.values())).fetchall()
    con.close()
    print("\n===== 门店统计 =====")
    for r in stats:
        print(f"  {r[0]}: 菜品 {r[1]} | 销量记录 {r[2]} | {r[3]}~{r[4]}")


if __name__ == "__main__":
    main()
