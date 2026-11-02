"""重生成后 DB 校验：bom_status 归位、recipes.ingredient_id 非空、成本非空。"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
from guibeicai.web import _db as db

db.init_db()
conn = db.get_conn()
cur = conn.cursor()


def q1(sql, args=()):
    cur.execute(sql, args)
    return cur.fetchone()[0]


print("=== 菜品 BOM 状态 ===")
total = q1("SELECT COUNT(*) FROM dishes")
done = q1("SELECT COUNT(*) FROM dishes WHERE bom_status='generated'")
pending = q1("SELECT COUNT(*) FROM dishes WHERE bom_status='pending' OR bom_status IS NULL")
print(f"菜品总数={total}  done={done}  pending/空={pending}")

print("\n=== 配方 ingredient_id 归位 ===")
rcount = q1("SELECT COUNT(*) FROM recipes")
rnull = q1("SELECT COUNT(*) FROM recipes WHERE ingredient_id IS NULL")
rorphan = q1("SELECT COUNT(*) FROM recipes r LEFT JOIN ingredients i ON r.ingredient_id=i.id WHERE r.ingredient_id IS NOT NULL AND i.id IS NULL")
print(f"配方行数={rcount}  空ingredient_id={rnull}  指向不存在食材={rorphan}")

print("\n=== 食材主数据 ===")
icount = q1("SELECT COUNT(*) FROM ingredients")
print(f"食材种类数={icount}")

print("\n=== 成本非空校验 ===")
cost_null = q1("SELECT COUNT(*) FROM dishes WHERE bom_status='generated' AND (unit_cost IS NULL OR unit_cost<=0)")
print(f"done 但成本空/<=0 的菜品={cost_null}")

print("\n=== 抽样 3 道菜（配方+有效单价）===")
cur.execute("SELECT id,name,unit_cost FROM dishes WHERE bom_status='generated' LIMIT 3")
for did, name, cost in cur.fetchall():
    print(f"\n▸ {name}  成本={cost}")
    for r in db.get_recipes_by_dish(did):
        print(f"   - {r.get('ingredient_name')}  {r.get('quantity')}{r.get('unit')}  ¥{r.get('effective_price')}/单位  行小计=¥{r.get('amount')}")

conn.close()

ok = (pending == 0) and (rnull == 0) and (rorphan == 0) and (cost_null == 0)
print("\n[结论]", "全部通过 ✅" if ok else "存在异常 ❌")
sys.exit(0 if ok else 1)
