# AWS 与 Microsoft Azure 智能报价

面向销售场景的 AWS 自然语言报价系统。AI 只把客户描述转换成结构化需求；产品型号、规格、区域支持、`usageType`、`operation` 均由 AWS 官方 API 发现；最终金额仅采用 AWS Billing and Cost Management Pricing Calculator 返回的 `cost` / `totalCost`。

同一页面也提供独立的 Microsoft Azure 报价引擎。销售编号会被视为组件硬边界；系统为每个组件启动隔离的 AI 参数解析，再由 Azure 服务插件查询 Microsoft Azure Retail Prices API。公开报价无需 Azure 账号；可选连接 Azure 订阅后，系统还能通过 Resource SKUs API 验证订阅级 VM 规格与区域限制。

Azure 支持 Pay-as-you-go、1/3 年预留、1/3 年 Savings Plan 和 Spot。最终报价展示 `productId`、`skuId`、`meterId`、`armSkuName`、单位价格、月用量和组件小计，并明确标注不包含 EA/MCA/CSP 协议折扣。

两套引擎共享销售工作流、客户确认、任务进度和报价数据模型，但提示词、字段模板、官方目录、服务插件、计费规则与缓存命名空间完全隔离。Azure 官方目录和已验证组件结果使用独立 SQLite 持久化缓存，后台预热常用区域与服务。

当前首批插件：

- Amazon EC2
- Amazon EBS
- Amazon RDS
- Amazon ElastiCache for Redis OSS / Valkey
- Application Load Balancer
- Amazon S3 Standard
- Amazon CloudFront

系统遵循 fail-closed 原则：只要产品、计费维度或区域支持不能唯一确认，就返回“需要人工确认”，不会用本地价格表或模型猜价。

## 架构

```text
销售粘贴客户原话
    -> AI 单次生成结构化报价意图（禁止型号和价格猜测）
    -> 后端校验并标准化区域、数量、规格与购买方式
    -> 对应 AWS 服务插件
       -> 服务 API：规格、引擎、区域支持
       -> AWS Price List API：产品属性、usageType、operation
    -> 先排除不适合业务的系列，再按官方价格选择最低成本合格型号
    -> bcm-pricing-calculator Workload Estimate
       -> 行项目 cost
       -> totalCost
    -> 单一推荐方案 / 必要的替代说明
```

页面默认是一键流程：销售直接粘贴客户原话。AI 只调用一次完成结构化，之后由确定性服务适配器查询官方规格并提交 BCM。最终金额以 BCM 返回的行项目 `cost` / `totalCost` 为准。EC2 系统盘与公网流量、RDS 存储、ALB 小时费与 LCU、S3 存储、CloudFront 流量与请求等会作为独立行项目提交。

页面会展示脱敏后的实时执行记录，包括 AI 计划校验、各服务官方发现、销售选择复核和 BCM 返回状态。系统提示词、凭证以及完整 AWS 原始响应不会发送到浏览器。

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

## 凭证与 BCM Estimate

应用仅使用 boto3 默认凭证链。推荐部署到 AWS 时使用 IAM Role；本地开发使用 AWS Profile。不要把 AK/SK 写进 `.env` 或前端变量。

默认每次报价创建一个带 `Application=aws-smart-quote` 标签的独立 Workload Estimate 并保留，便于审计，不会修改账号中已有的其他 Estimate。大规模生产环境也可以配置专用复用池并通过 `BCM_WORKLOAD_ESTIMATE_IDS` 指定。

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
