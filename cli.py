"""
桂备菜 - 命令行入口
"""

import sys
import subprocess
import argparse

from guibeicai.config.settings import BASE_DIR


def main():
    parser = argparse.ArgumentParser(
        description="桂备菜 - 中小餐饮智能备菜系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
命令示例:
  guibeicai-run web              # 启动 Web 界面
  guibeicai-run scheduler        # 启动后台调度器
  guibeicai-run all              # 同时启动 Web + 调度器
  guibeicai-run init             # 初始化配置目录
        """,
    )
    parser.add_argument(
        "command",
        choices=["web", "scheduler", "all", "init", "provision", "list-tenants"],
        help="要执行的命令",
    )
    args, _ = parser.parse_known_args()

    if args.command == "init":
        _init_project()
    elif args.command == "web":
        _run_web()
    elif args.command == "scheduler":
        _run_scheduler()
    elif args.command == "all":
        _run_all()
    elif args.command == "provision":
        _cmd_provision()
    elif args.command == "list-tenants":
        _cmd_list_tenants()


def _init_project():
    """初始化项目目录和示例配置"""
    import shutil
    from guibeicai.config.settings import DATA_DIR, OUTPUT_DIR, LOGS_DIR

    for d in [DATA_DIR, OUTPUT_DIR, LOGS_DIR]:
        d.mkdir(exist_ok=True)

    # 复制示例配置
    example_env = BASE_DIR / "guibeicai" / "config" / "settings.example.env"
    target_env = BASE_DIR / "guibeicai" / ".env"
    if example_env.exists() and not target_env.exists():
        shutil.copy(example_env, target_env)
        print(f"✅ 已创建 {target_env} 文件，请编辑填入您的 API Key")
    elif not example_env.exists():
        print(f"⚠️ 未找到示例配置 {example_env}，请手动创建 .env 并填入 API Key")

    print("✅ 项目初始化完成！")


def _run_web():
    """启动 Streamlit Web"""
    web_path = BASE_DIR / "guibeicai" / "web" / "app.py"
    cmd = [
        sys.executable, "-m", "streamlit", "run",
        str(web_path),
        "--server.port", "8501",
        "--server.address", "0.0.0.0",
    ]
    print("🚀 启动 Web 界面: http://localhost:8501")
    subprocess.run(cmd)


def _run_scheduler():
    """启动 APScheduler"""
    from guibeicai.scheduler.app import main
    main()


def _run_all():
    """同时启动 Web + 调度器（用 subprocess 分别跑）"""
    import threading

    def web_thread():
        _run_web()

    def scheduler_thread():
        _run_scheduler()

    t1 = threading.Thread(target=web_thread, daemon=True)
    t2 = threading.Thread(target=scheduler_thread, daemon=True)
    t1.start()
    t2.start()

    print("🚀 桂备菜系统已启动 (Web + 调度器)")
    print("   Web: http://localhost:8501")
    print("   按 Ctrl+C 停止")
    try:
        t1.join()
        t2.join()
    except KeyboardInterrupt:
        print("\n👋 已停止")


def _cmd_provision():
    """开通一个新租户（多租户托管）：建独立库 + 门店 + 管理员 + 注册表。

    用法示例：
      guibeicai-run provision \\
          --tenant-id nanning-laoyou \\
          --name "南宁老友粉（望州店）" \\
          --store "望州店" \\
          --admin-user laoyou_admin --admin-pass "初始密码" \\
          --city 南宁 --province 广西 \\
          --valid-until 2026-12-31 --remark "渠道商引荐"
    """
    import argparse as _ap
    from guibeicai.web import _tenancy

    p = _ap.ArgumentParser(description="开通新租户（独立数据库，物理隔离）")
    p.add_argument("--tenant-id", required=True, help="租户标识，小写字母/数字/-/_，3-63位")
    p.add_argument("--name", required=True, help="客户名称（对外展示）")
    p.add_argument("--store", required=True, help="首店名称")
    p.add_argument("--admin-user", required=True, help="该租户入口账号登录名")
    p.add_argument("--admin-pass", required=True, help="初始密码（建议强制首次登录改密）")
    p.add_argument("--admin-role", default="admin",
                   help="入口账号角色编码：admin(租户管理员) / channel(渠道商) / merchant(单店老板)，默认 admin")
    p.add_argument("--city", default="", help="首店所在城市")
    p.add_argument("--province", default="", help="首店所在省份")
    p.add_argument("--valid-until", default=None, help="账号有效期 YYYY-MM-DD（对齐合同期限，缺省长期）")
    p.add_argument("--remark", default="", help="备注")
    # 解析 sys.argv[2:]，跳过顶层已消费的 "provision" 命令
    a = p.parse_args(sys.argv[2:]) if len(sys.argv) > 2 else p.parse_args([])

    try:
        res = _tenancy.create_tenant(
            tenant_id=a.tenant_id, tenant_name=a.name, store_name=a.store,
            admin_username=a.admin_user, admin_password=a.admin_pass,
            entry_role=a.admin_role,
            city=a.city, province=a.province,
            valid_until=a.valid_until, remark=a.remark,
        )
    except ValueError as e:
        print(f"❌ 开通失败：{e}")
        sys.exit(1)

    print("✅ 租户开通成功：")
    print(f"   租户ID : {res['tenant_id']}")
    print(f"   名称   : {res['name']}")
    print(f"   首店ID : {res['store_id']}")
    print(f"   入口账号: {res['admin_username']}（角色：{res['entry_role']}）")
    print(f"   有效期 : {res['valid_until'] or '长期'}")
    print(f"   库文件 : {res['db_path']}")
    print("\n   登录地址与运营方相同；该账号仅能访问自己的独立数据库。")


def _cmd_list_tenants():
    """列出全部租户（含库文件路径与大小），便于运维核对。"""
    from guibeicai.web import _tenancy

    rows = _tenancy.list_tenants()
    if not rows:
        print("（暂无租户）")
        return
    print(f"{'租户ID':<22} {'状态':<10} {'名称':<28} {'库大小':<8}")
    print("-" * 72)
    for r in rows:
        size = f"{r['db_size_mb']}MB" if r["db_size_mb"] is not None else "-"
        print(f"{r['tenant_id']:<22} {r['status']:<10} {r['name']:<28} {size:<8}")


if __name__ == "__main__":
    main()