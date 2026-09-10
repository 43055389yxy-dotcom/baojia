# AstraQuote 多云智能报价

AstraQuote 面向销售报价场景，当前统一支持 AWS、Microsoft Azure、Oracle Cloud、Google Cloud、腾讯云、阿里云、华为云、百度智能云、火山引擎和天翼云。

GPT 负责需求理解、组件拆分、官方查询参数、产品与 SKU 选择、用量换算、阶梯价计算和方案比较；MCP 只负责调用所选厂商官方价格目录/询价 API、保存官方候选与费率身份、校验事实和金额、生成 Excel，并把报价与下载链接返回销售页面。各云厂商的产品、区域、购买方式和优惠语义彼此隔离，不会用 AWS 产品或规则替代其他云。

系统遵循 fail-closed 原则：查询超过 10 个候选时要求 GPT 继续收窄，不截断；缺少官方价格证据、事实未消费或金额无法对账时停止发布。正式商业报价不抵扣 Free Tier、Always Free、免费试用、促销赠送或账户信用额度。

## 架构

```text
销售粘贴客户资料并选定云厂商
    -> GPT 清洗并删除原文，生成 RequirementIR 与 Fact Ledger
    -> GPT 为每个组件组织该厂商官方目录/询价请求
    -> MCP 签名并执行只读官方 API，返回完整小结果集与费率身份
    -> GPT 继续收窄候选、选型、换算用量并计算各方案
    -> MCP 编译校验 RequirementIR -> ResourceIR -> BillingUsageIR -> PriceIR
    -> 生成精简客户版 Excel 与销售页下载链接
```

报价任务保存阶段与 `price_batch_id`。断线恢复只补缺失查询或交付步骤，已经成功的官方查价、文件生成和页面交付不会重复执行。

## 本地启动

macOS 日常开发可直接双击项目根目录的 `start-local.command`。该脚本启动的后端会监视
`backend/app`，保存 Python 代码后自动重启，因此无需每次手动停止再启动。它只使用当前
本地工作区代码，不会自动执行 `git pull`，避免覆盖尚未提交的修改。

后端要求 Python 3.11+：

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env
uvicorn app.main:app --reload --port 8000
```

前端要求 Node.js 20+：

```bash
cd frontend
npm install
cp .env.example .env.local
npm run dev
```

打开 `http://localhost:3000`。

## 官方 API 凭证

AWS 使用 boto3 默认凭证链；Azure 与 OCI 使用公开价格目录；GCP 使用 Billing Catalog API Key。腾讯云、阿里云、华为云、百度智能云、火山引擎和天翼云使用只读子账号密钥。所有密钥只保存在后端运行环境，禁止写入 Git、前端或 MCP 参数。

详细环境变量见 [`backend/.env.example`](backend/.env.example)。

## 验证

```bash
cd backend
pytest
ruff check .

cd ../frontend
npm run lint
npm run build
```
