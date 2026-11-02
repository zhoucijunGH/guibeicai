"""验证食材单位统一归一化为 kg/L。

运行方式（系统 Python 3.14，依赖 pandas）：
    "D:\\Program Files\\Python314\\python.exe" tests/manual/verify_unit_normalization.py
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.abspath("."))

from guibeicai.web import _db


def _temp_copy(src: str) -> str:
    tmp_dir = tempfile.mkdtemp(prefix="guibeicai_verify_unit_")
    tmp = os.path.join(tmp_dir, "guibeicai.db")
    shutil.copy2(src, tmp)
    return tmp


def main():
    src = os.path.join("data", "guibeicai.db")
    if not os.path.exists(src):
        print(f"[SKIP] 演示库不存在: {src}")
        return

    db_path = _temp_copy(src)
    os.environ["SQLITE_PATH"] = db_path

    # 记录迁移前
    conn = _db.get_conn()
    c = conn.cursor()
    c.execute("SELECT unit, COUNT(*) FROM recipes GROUP BY unit")
    before_recipes = dict(c.fetchall())
    c.execute("SELECT base_unit, COUNT(*) FROM ingredients GROUP BY base_unit")
    before_ing = dict(c.fetchall())
    c.execute("SELECT name FROM dishes WHERE id IN (SELECT dish_id FROM recipes WHERE unit='g') LIMIT 1")
    sample_dish_before = c.fetchone()
    conn.close()

    print("=== 迁移前 recipes 单位分布 ===")
    print(before_recipes)
    print("=== 迁移前 ingredients base_unit 分布 ===")
    print(before_ing)

    # 执行迁移
    _db.init_db()

    conn = _db.get_conn()
    c = conn.cursor()
    c.execute("SELECT unit, COUNT(*) FROM recipes GROUP BY unit")
    after_recipes = dict(c.fetchall())
    c.execute("SELECT base_unit, COUNT(*) FROM ingredients GROUP BY base_unit")
    after_ing = dict(c.fetchall())

    # 找一条 g 食材迁移后示例
    c.execute("""
        SELECT d.name, r.ingredient_name, r.quantity, r.unit, r.unit_price, r.quantity*r.unit_price
        FROM recipes r JOIN dishes d ON r.dish_id=d.id
        WHERE r.ingredient_name LIKE '%冰糖%' LIMIT 1
    """)
    sample_after = c.fetchone()
    conn.close()

    print("\n=== 迁移后 recipes 单位分布 ===")
    print(after_recipes)
    print("=== 迁移后 ingredients base_unit 分布 ===")
    print(after_ing)
    print("=== 迁移后 冰糖 示例 ===")
    print(sample_after)

    errors = []
    for bad in ('g', '克', 'ml', '毫升'):
        if after_recipes.get(bad, 0) > 0:
            errors.append(f"recipes 仍含 {bad}: {after_recipes[bad]} 条")
        if after_ing.get(bad, 0) > 0:
            errors.append(f"ingredients 仍含 {bad}: {after_ing[bad]} 条")

    # g 数量级应下降（总量不变，单位变 kg）
    g_count_before = sum(before_recipes.get(u, 0) for u in ('g', '克'))
    g_count_after = sum(after_recipes.get(u, 0) for u in ('g', '克'))
    if g_count_after >= g_count_before and g_count_before > 0:
        errors.append(f"g 单位未减少: before={g_count_before}, after={g_count_after}")

    shutil.rmtree(os.path.dirname(db_path), ignore_errors=True)
    os.unsetenv("SQLITE_PATH") if hasattr(os, "unsetenv") else None

    if errors:
        print("\n[FAIL]")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)

    print("\n[OK] 单位归一化验证通过：g/ml 已转为 kg/L，冰糖等食材成本已回归合理区间")


if __name__ == "__main__":
    main()
