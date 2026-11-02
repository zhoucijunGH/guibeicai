"""桂备菜 - web 包初始化（确保 Web 进程加载 .env）"""
# Web 页面通过 `from guibeicai.web.xxx import ...` 引入时，本 __init__ 会先执行。
# 显式加载 guibeicai/.env，确保 SILICONFLOW_API_KEY 等环境变量在 Web 进程可用
# （此前仅在 cli.py 触发 settings.load_dotenv，Web 端从未加载，导致 LLM Key 恒为空）。
try:
    from dotenv import load_dotenv
    from pathlib import Path

    _ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
    else:
        load_dotenv()
except Exception:
    pass
