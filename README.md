# 桂备菜 · 餐饮备菜选品与采购预测系统

面向餐饮门店的「明天备什么菜」决策系统：基于历史销量，结合天气、节假日与特殊事件预测未来营收与菜品销量，按菜品 BOM 反推原料需求生成采购建议，并自动推送给门店老板。

> 本仓库为**脱敏开源版**：不含真实门店数据、密钥与商业文档，示例数据可自行生成。

## 核心功能

- **多门店 / 多租户**：门店、角色、用户管理与数据隔离，支持渠道账号
- **营收与销量预测**：Prophet 时间序列 + 天气因子（Open-Meteo 免费接口）+ 节假日（chinesecalendar）+ 手工特殊事件，支持回测与偏差修正
- **采购建议**：按菜品 BOM 反推原料需求，叠加易腐/非易腐安全库存系数生成采购单，支持原料级预测
- **毛利与损耗测算**：营收、成本、毛利率跟踪
- **定时任务**：每日自动预测，结果经 SMTP 邮件 / 企业微信 Webhook 推送
- **移动端 H5**（`mp/` 目录）：Vue 3 + Vite，微信场景登录、手机号绑定、门店选择，对接后端 REST API
- **数据导入**：Excel 模板批量导入门店、菜品、销量历史

## 技术栈

| 层 | 技术 |
|---|---|
| 界面 / 后端 | Python + Streamlit + Plotly（多页应用，30+ 模块：鉴权、审计、加密、缓存、多租户） |
| 数据 | SQLite（零部署依赖）+ Excel（pandas）导入导出 |
| 算法 | Prophet、回测与偏差修正、安全库存模型 |
| 移动端 | Vue 3 + Vite + Pinia |
| 通知 | SMTP 邮件 / 企业微信 Webhook |
| LLM | 菜品知识生成与预测解释（硅基流动 API，密钥全部走 `.env`） |

## 快速开始

```bash
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt

python cli.py init             # 初始化目录与 .env 骨架
python scripts/gen_demo_data.py    # 生成演示数据（可选）
python cli.py web              # 启动 Web 界面
python cli.py scheduler        # 启动定时任务
```

- `data/桂备菜数据模板.xlsx` 为数据导入模板
- `.env` 参考 `guibeicai/config/settings.example.env`，API 密钥、SMTP 密码只放本地，不入仓库

## 目录结构

```
guibeicai/            # Python 包
├── api/              # REST API（移动端对接）
├── config/           # 统一配置管理（yaml + env）
├── data_import/      # Excel 导入
├── scheduler/        # 定时任务
└── web/              # Streamlit 多页应用（_auth/_tenancy/_forecast/_backtest…）
mp/                   # 移动端 H5（Vue 3 + Vite）
data/                 # SQLite 数据与导入模板（运行时生成）
docs/                 # 设计文档、功能说明、操作与部署文档
scripts/              # 演示数据生成、BOM 重算、校验脚本
tests/                # 手动冒烟测试
```

## 文档

- `docs/桂备菜_设计文档.md`
- `docs/桂备菜_功能说明文档.md`
- `docs/桂备菜_操作文档.md`
- `docs/桂备菜部署与验收说明.md`
- `docs/H5网页版设计方案_20260908.md`

## 说明

- 商业化文档（GTM 计划、推广材料、市场洞察）与真实经营数据**未包含**在本仓库
- 预测模型缓存（`_prophet_cache`、`_llm_cache`）运行时自动生成，已在 `.gitignore` 中忽略
