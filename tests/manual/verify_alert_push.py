"""验证预警自动推送链路（临时库，不碰演示数据）。

覆盖：
1. get_alert_push_stores 正确返回开启推送的门店；
2. 有渠道配置的门店 → 推送被调用；
3. 无渠道配置的门店 → 当日标记但不推送（不刷屏）；
4. 同日同店去重：第二次不再推送；
5. mark_alert_pushed / alert_pushed_today 去重表生效。
"""
import os
import sys
import tempfile
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 使用独立临时库，避免污染演示数据
db_path = tempfile.mktemp(suffix=".db")
os.environ["SQLITE_PATH"] = db_path

from guibeicai.web import _db
from guibeicai.web._db import (
    init_db, upsert_store, get_alert_push_stores,
    alert_pushed_today, mark_alert_pushed,
)

passed = []
failed = []


def check(cond, msg):
    if cond:
        passed.append(msg)
        print(f"  ✅ {msg}")
    else:
        failed.append(msg)
        print(f"  ❌ {msg}")


init_db()

# 门店 A：有老板邮箱 + 企微 webhook，开启推送
sidA = upsert_store("测试店A", "南宁市", "广西", owner_email="boss@demo.com",
                    wecom_webhook="https://qyapi.weixin.qq.com/fake_hook",
                    auto_predict=1, alert_push_enabled=1)
# 门店 B：无任何渠道，开启推送（应标记不去推）
sidB = upsert_store("测试店B", "柳州市", "广西", owner_email="", wecom_webhook="",
                    auto_predict=1, alert_push_enabled=1)
# 门店 C：关闭推送（不应出现在推送名单）
sidC = upsert_store("测试店C", "桂林市", "广西", owner_email="boss@c.com",
                    wecom_webhook="https://qyapi.weixin.qq.com/c_hook",
                    auto_predict=1, alert_push_enabled=0)

# 导入调度器并打桩：generate_alerts 返回假预警，StoreAlertNotifier 记录调用
import guibeicai.web._scheduler as sch
fake_alerts = [{"severity": "高", "type": "加备", "title": "【鱼香肉丝】建议加备", "detail": "预测 120 份"}]
sch.generate_alerts = lambda sid: fake_alerts

sent = []


class FakeNotifier:
    def send_store_alert(self, store_name, alerts, targets):
        sent.append((store_name, len(alerts), targets))
        return True


sch.StoreAlertNotifier = FakeNotifier

today = date.today().strftime("%Y-%m-%d")

# ---- 1. 推送名单 ----
push_stores = get_alert_push_stores()
check({s["id"] for s in push_stores} == {sidA, sidB}, "推送名单仅含开启推送的门店(A/B)，不含 C")

# ---- 2+3. 执行推送：A 有渠道→推送；B 无渠道→标记不推送 ----
# 注意：本函数按传入列表推送，开关过滤在调度层（get_alert_push_stores 已排除 C）。
# 真实的自动推送只传 get_alert_push_stores() 的结果（A/B）。
sent.clear()
sch.push_store_alerts_for_stores([sidA, sidB])
check(len(sent) == 1, "仅 1 个有渠道门店被推送（A）")
check(sent and sent[0][0] == "测试店A", "推送对象为测试店A")
check(alert_pushed_today(sidA, today), "A 已写入去重标记")
check(alert_pushed_today(sidB, today), "B 无渠道也已标记（避免反复尝试）")
check(not alert_pushed_today(sidC, today), "C 不在推送名单，无标记")

# ---- 4. 同日去重：再次推送不应重复 ----
sent.clear()
sch.push_store_alerts_for_stores([sidA, sidB])
check(len(sent) == 0, "同日同店去重：二次调用不再推送")

# ---- 5. 显式去重表函数 ----
mark_alert_pushed(sidA, "2099-01-01")
check(alert_pushed_today(sidA, "2099-01-01"), "mark_alert_pushed 写入后可查")

print()
if failed:
    print(f"❌ 失败 {len(failed)} 项：{failed}")
    sys.exit(1)
print(f"✅ 全部验证通过（{len(passed)} 项）")
print(f"临时库：{db_path}（验证结束，可手动删除）")
