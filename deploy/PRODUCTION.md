# AstraQuote 生产部署

生产版本使用一个 MCP 汇聚四个云厂商的官方价目接口：

- AWS Price List API
- Microsoft Azure Retail Prices API
- Oracle Cloud Infrastructure Price List API
- Google Cloud Billing Catalog API

销售在报价页选择云厂商。GPT 负责理解需求、组织官网查询参数、选择官方价格项、换算用量和计算报价；MCP 只负责执行官方查询、保存原始证据、做 schema / 事实归属 / 金额加总一致性等机械校验，并交付页面结果或 Excel。

生产运行路径不包含 AWS Pricing Calculator、Calculator 浏览器、模板映射或创建后回读流程。

## 配置文件

服务器使用以下三个环境文件：

```text
/home/ec2-user/astraquote/config/backend.env
/home/ec2-user/astraquote/config/mcp.env
/home/ec2-user/astraquote/config/oauth.env
```

AWS 查询使用服务器已有的 AWS 凭证链。Azure Retail Prices API 和 OCI Price List API 是公开价目接口，不要求把账号密钥写进 MCP。Google Cloud Billing Catalog API 需要在 `backend.env` 配置：

```text
GCP_BILLING_API_KEY=...
```

API Key 只用于访问官方目录，MCP 不接收也不向 GPT 返回密钥。

Excel 交付需要配置私有 S3 和稳定下载入口；结果只回到销售页面，不发送企业微信：

```text
ASTRAQUOTE_XLSX_BUCKET=...
ASTRAQUOTE_XLSX_REGION=...
ASTRAQUOTE_PUBLIC_BASE_URL=https://baojia.tontiancloud.com
```

## MCP 工具

生产 MCP 只公开六个工具：

1. `describe_service`：查询 AWS Price List 服务元数据。
2. `get_attribute_values`：查询 AWS 官方属性值。
3. `get_prices`：按 GPT 提供的参数查询 AWS、Azure、OCI 或 GCP 官方价目。
4. `get_quote_job_status`：读取已保存的报价阶段和批次。
5. `resume_quote_job`：只返回下一缺失步骤，不重跑已成功动作。
6. `build_estimate`：验证 GPT 选中的官方证据与计算结果，然后生成 Excel 并返回销售页面。

## 部署与检查

Jenkins 使用 [`deploy/jenkins-shell.sh`](./jenkins-shell.sh) 构建并启动容器。上线前至少运行：

该脚本还会把同一版本的 `backend`、`tools`、`policies` 同步到 Docker 宿主机
`/home/ec2-user/astraquote/source`，然后通过临时的 Docker 宿主机命名空间调用
已安装的 `astraquote-gpt-relay.service` 完成明确重启。Jenkins 本身运行在容器
中，所以这里通过它已有的 Docker socket 完成宿主机文件同步和进程重启，不要求
Jenkins 容器安装 `rsync`、`sudo` 或 `systemctl`。临时容器只执行固定的
`systemctl restart astraquote-gpt-relay.service`，Docker 宿主机必须已经安装该
systemd 服务。

```bash
cd deploy/astraquote-mcp && npm test
cd frontend && npm test && npm run build
cd backend && pytest
```

部署完成后检查：

```bash
docker exec astraquote curl -fsS http://127.0.0.1:3000/api/backend/api/health
docker exec astraquote curl -fsS http://127.0.0.1:8200/readyz
docker exec astraquote curl -fsS http://127.0.0.1:8001/readyz
```
