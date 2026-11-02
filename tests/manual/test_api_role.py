"""API 层只读角色校验测试：渠道账号调写接口应 403，普通商家不受影响"""
import sys, os, base64, json, time, hmac, hashlib
sys.path.insert(0, r"D:/Project/guibeicaiV1.5")
from dotenv import load_dotenv
load_dotenv("guibeicai/.env")

from fastapi.testclient import TestClient
import guibeicai.api.main as m

secret = os.getenv("GUOBEICAI_API_SECRET") or "test-secret"


def make_token(uid: int) -> str:
    payload = json.dumps({"uid": uid, "exp": int(time.time()) + 3600}).encode()
    b64 = base64.urlsafe_b64encode(payload).decode()
    sig = hmac.new(secret.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


import sqlite3
c = sqlite3.connect("data/guibeicai.db")
rows = c.execute("SELECT id, username, role FROM users").fetchall()
c.close()
print("用户:", rows)
ch = [r for r in rows if r[2] == "channel"]
mc = [r for r in rows if r[2] == "merchant"]
ad = [r for r in rows if r[2] == "admin"]
if not ch or not mc:
    print("缺少 channel/merchant 用户，无法测试"); sys.exit(1)

client = TestClient(m.app)
ok = fail = 0

WRITE_CALLS = [
    ("POST", "/api/actual-sales", {"store_id": 2, "sale_date": "2026-09-08", "items": []}),
    ("POST", "/api/predict/run", {"store_id": 2, "predict_date": "2026-09-10"}),
    ("POST", "/api/pos-sources", {"store_id": 2, "name": "x", "source_type": "http", "base_url": "http://127.0.0.1:1/x"}),
    ("DELETE", "/api/pos-sources/999999", None),
    ("POST", "/api/pos-sources/test", {"store_id": 2}),
    ("POST", "/api/pos-sources/999999/pull", None),
    ("POST", "/api/push/purchase-order", {"store_id": 2}),
]

print("\n=== 1) 渠道账号（channel）调用写接口应 403 ===")
tok_ch = {"Authorization": "Bearer " + make_token(ch[0][0])}
for method, path, body in WRITE_CALLS:
    try:
        r = client.request(method, path, headers=tok_ch, json=body) if body is not None else client.request(method, path, headers=tok_ch)
    except Exception as e:
        r = None
        print(f"  [ERR ] {method} {path} -> {e}")
        fail += 1
        continue
    good = r.status_code == 403
    ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
    print(f"  [{'PASS' if good else 'FAIL'}] {method} {path} -> {r.status_code} {r.text[:60]}")

print("\n=== 2) 普通商家（merchant）不应被误伤（403 仅限只读角色）===")
tok_mc = {"Authorization": "Bearer " + make_token(mc[0][0])}
for method, path, body in WRITE_CALLS[:1] + [("POST", "/api/push/purchase-order", {"store_id": 2})]:
    r = client.request(method, path, headers=tok_mc, json=body)
    good = r.status_code != 403
    ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
    print(f"  [{'PASS' if good else 'FAIL'}] {method} {path} -> {r.status_code}（非403即未被误伤）")

print("\n=== 3) 渠道账号只读接口仍可用 ===")
r = client.get("/api/me", headers=tok_ch)
good = r.status_code == 200
ok, fail = (ok + 1, fail) if good else (ok, fail + 1)
print(f"  [{'PASS' if good else 'FAIL'}] GET /api/me -> {r.status_code} {r.text[:100]}")

r = client.get("/api/stores", headers=tok_ch)
print(f"  [INFO] GET /api/stores -> {r.status_code} {r.text[:120]}")

print(f"\n===== 结果: PASS {ok} / FAIL {fail} =====")
sys.exit(1 if fail else 0)
