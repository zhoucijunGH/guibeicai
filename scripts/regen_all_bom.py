"""CLI：菜品配方增量重生成（大模型驱动）。

增量、可恢复：小批请求 -> 每批返回立即落库 -> 下一批；重跑自动跳过已 generated。
用法：
  python regen_all_bom.py                     # 全门店全量（仅 pending）
  python regen_all_bom.py --store 3           # 只重算门店#3（新门店大批菜品场景）
  python regen_all_bom.py --batch 3 --force   # 每批3菜、重置全部后重算
  python regen_all_bom.py --max-batches 2     # 调试：只跑前2批即停（验证增量落库）

日志：regen_bom.log
"""
import sys
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from guibeicai.web._bom_regen import regen_bom, _port_in_use

LOG = str(ROOT / "logs" / "regen_bom.log")


def L(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="菜品配方增量重生成")
    ap.add_argument("--store", type=int, default=None, help="只重算某门店(新门店场景)")
    ap.add_argument("--batch", type=int, default=6, help="每批菜名数(小批粒度, 默认6)")
    ap.add_argument("--workers", type=int, default=2, help="LLM 并发数(仅影响请求阶段, 默认2)")
    ap.add_argument("--force", action="store_true", help="重置全部活跃菜品为pending后重算")
    ap.add_argument("--max-batches", type=int, default=None, help="调试: 最多处理批次数")
    args = ap.parse_args()

    open(LOG, "w", encoding="utf-8").close()
    L(f"[启动] store={args.store} batch={args.batch} workers={args.workers} force={args.force} max_batches={args.max_batches}")

    # 仅 CLI 场景检测：若 Streamlit(8501) 占用库，先停服务再跑，避免 database is locked 整批失败
    if _port_in_use("127.0.0.1", 8501):
        L("ERROR: 检测到 Streamlit(8501) 正在占用数据库，请先停止服务再重生成，避免 database is locked")
        sys.exit(2)

    res = regen_bom(
        store_id=args.store,
        batch=args.batch,
        force=args.force,
        max_batches=args.max_batches,
        max_workers=args.workers,
        log=L,
    )
    if res is None:
        L("[退出] 前置检查未通过（缺 API_KEY 或 8501 被占用）")
        sys.exit(2)
    L(f"[汇总] ok={res['ok']} failed={res['failed']} total={res['total']}")
