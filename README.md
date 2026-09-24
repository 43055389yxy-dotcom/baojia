# AstraQuote 本地多云报价 MCP

AstraQuote 现在以一个本地 MCP 的形式工作。在 GPT/Codex 里提交客户需求后，MCP 读取对应云厂商的官方价格、校验金额、生成 Excel，然后把报价和链接直接返回对话。

不再需要销售前端、单独启动的 HTTP 后端、远程桌面或 GPT 中继机。仓库里仍保留旧前端和部署文件，仅用于兼容和迁移，本地日常报价不会启动它们。

## 返回规则

- AWS：返回结构化报价、本地 Excel 下载链接和 AWS Pricing Calculator 公开共享链接。
- Azure、OCI、GCP、腾讯云、阿里云、华为云、百度智能云、火山引擎和天翼云：返回结构化报价和 Excel 链接，不生成云厂商官网报价链接。

AWS 公开共享链接有效期为 1 年。链接中的数字是 AstraQuote 已经根据官方价格证据校验过的交付副本；最终价格依据仍是本次 MCP 保存的官方价格查询记录。

## 工作方式

```text
GPT/Codex 接收需求并拆分组件
    -> 本地 AstraQuote MCP
        -> 调用官方价格目录/询价 API
        -> 保存价格证据并机械校验合计
        -> 生成本地 Excel
        -> 仅 AWS 生成 AWS Pricing Calculator 共享链接
    -> 链接直接返回 GPT/Codex
```

系统遵循 fail-closed 原则：候选过多时要求 GPT 继续收窄；缺少官方价格证据、客户事实未消费、金额无法对账，或 AWS 组件无法安全映射到官方计算器时，不会发布误导性链接。

## 首次安装

需要 Python 3.11+ 和 Node.js 22+。

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

cd ../deploy/astraquote-mcp
npm install
```

云厂商凭证仍放在 `backend/.env` 或本机环境中。AWS 使用 boto3 默认凭证链；Azure 和 OCI 使用公开价格目录；GCP 使用 Billing Catalog API Key；其他云使用只读子账号凭证。密钥不得写入 Git 或 MCP 参数。

## 启动和连接

双击项目根目录的 `start-local.command`，或在终端运行：

```bash
./start-local.command
```

脚本只会启动 `http://127.0.0.1:8200/mcp`。首次将它加入 Codex：

```bash
codex mcp add astraquote --url http://127.0.0.1:8200/mcp
codex mcp list
```

本地报价状态、价格快照和 Excel 保存在项目的 `.astraquote/` 目录，该目录已忽略 Git。Excel 下载链接只在本地 MCP 运行时可访问。

## 验证

```bash
cd deploy/astraquote-mcp
npm test

cd ../../backend
pytest
ruff check .
```
