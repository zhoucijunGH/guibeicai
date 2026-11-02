# 🥘 桂备菜 - 中小餐饮智能备菜系统

用 AI 帮单店/小连锁餐厅预测明日销量，自动生成采购清单，每天晚上自动推送到老板微信/邮箱。

## 快速开始

### 方式一：Docker（推荐生产环境）

\`\`\`bash
git clone <repo>
cd guibeicai
cp .env.example .env   # 编辑填入 API Key
docker compose up -d
# 访问 http://服务器IP:8501
\`\`\`

### 方式二：本地运行

\`\`\`bash
pip install -r requirements.txt
cp .env.example .env   # 编辑填入 API Key
python -m guibeicai.cli all
# 访问 http://localhost:8501
\`\`\`

## 需要配置的 API Key

| 服务 | 用途 | 获取地址 |
|------|------|----------|
| DeepSeek | AI 销量预测 | platform.deepseek.com |
| SMTP | 邮件推送 | 你的邮箱服务商 |
| 企业微信 Webhook | 微信群推送 | 企业微信群 → 添加机器人 |
| OpenWeatherMap | 天气数据 | openweathermap.org |

## 使用流程

1. 登录 Web 界面 → 上传历史销售数据
2. 配置预测参数 → 点击"开始预测"
3. 查看采购清单 → 打印或导出
4. 系统每天 22:00 自动跑一次，推送到老板微信/邮箱

## 项目结构

（见上方完整目录树）