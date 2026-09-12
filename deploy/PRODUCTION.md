# AstraQuote 生产部署

生产版本使用一个 MCP 汇聚十个云厂商的官方价目或询价接口：

- AWS Price List API
- Microsoft Azure Retail Prices API
- Oracle Cloud Infrastructure Price List API
- Google Cloud Billing Catalog API
- 腾讯云官方目录/询价 API
- 阿里云官方目录/询价 API
- 华为云官方目录/询价 API
- 百度智能云官方目录/询价 API
- 火山引擎官方目录/询价 API
- 天翼云官方目录/询价 API

销售在报价页选择云厂商。GPT 负责理解需求、组织官网查询参数、选择官方价格项、换算用量和计算报价；MCP 只负责执行官方查询、保存原始证据、做 schema / 事实归属 / 金额加总一致性等机械校验，并交付页面结果或 Excel。程序不替 GPT 选型号、补业务参数或算钱。

官方 API 优先。同一组件、计费项和方案使用三个不同查询仍未取得可用费率后，GPT 可选择当前云厂商、当前账号站点的官方 HTTPS 价格页，并提交价格项目、地区、币种、单位、读取时间和前三次查询 ID。MCP 只校验失败次数、计费归属、官方域名与价格上下文；权限/凭据失败仍由管理员修复，最近权限拒绝的五分钟预检保持不变。有可用 API 价格时禁止网页证据覆盖。

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

腾讯云、阿里云、华为云、百度智能云、火山引擎和天翼云使用各自只读子账号的访问密钥。在 `backend.env` 配置以下变量；不要写入 Git：

```text
TENCENTCLOUD_SECRET_ID=...
TENCENTCLOUD_SECRET_KEY=...
ALIBABA_CLOUD_ACCESS_KEY_ID=...
ALIBABA_CLOUD_ACCESS_KEY_SECRET=...
HUAWEICLOUD_ACCESS_KEY=...
HUAWEICLOUD_SECRET_KEY=...
BAIDUCLOUD_ACCESS_KEY_ID=...
BAIDUCLOUD_SECRET_ACCESS_KEY=...
VOLCENGINE_ACCESS_KEY=...
VOLCENGINE_SECRET_KEY=...
CTYUN_ACCESS_KEY=...
CTYUN_SECRET_KEY=...
```

这些密钥只由后端签名器读取。GPT 只提供官方 endpoint、查询动作、区域、精确参数和响应字段路径，不能读取或提交密钥。

Excel 交付需要配置私有 S3 和稳定下载入口；结果只回到销售页面，不发送企业微信：

```text
ASTRAQUOTE_XLSX_BUCKET=...
ASTRAQUOTE_XLSX_REGION=...
ASTRAQUOTE_PUBLIC_BASE_URL=https://baojia.tontiancloud.com
```

官方价格证据缓存和瞬时故障退避可按需调整；不配置时分别为新鲜 6 小时、最大陈旧 72 小时、重试等待 0.25 秒和 0.75 秒：

```text
ASTRAQUOTE_OFFICIAL_PRICE_FRESH_SECONDS=21600
ASTRAQUOTE_OFFICIAL_PRICE_MAX_STALE_SECONDS=259200
ASTRAQUOTE_PROVIDER_RETRY_DELAY_1=0.25
ASTRAQUOTE_PROVIDER_RETRY_DELAY_2=0.75
```

缓存键包含云厂商、账号站点、地域和完整标准化查询。陈旧快照只允许在官方服务维护、限流或连接中断时临时降级，并只在销售页面披露；客户 Excel 不展示技术故障或缓存降级。权限、凭据或请求错误不会使用陈旧价格。

## MCP 工具

生产 MCP 只公开七个工具：

1. `describe_service`：查询 AWS Price List 服务元数据。
2. `get_attribute_values`：查询 AWS 官方属性值。
3. `get_prices`：按 GPT 提供的精确参数查询所选云厂商的官方价目或询价接口。
4. `get_price_results`：按需读取已保存的官方价格明细，不重复请求官网。
5. `get_quote_job_status`：读取已保存的报价阶段和批次。
6. `resume_quote_job`：只返回下一缺失步骤，不重跑已成功动作。
7. `build_estimate`：验证 GPT 选中的官方 API 证据，或满足三次失败门槛后的官方价格页证据及计算结果，然后生成 Excel 并返回销售页面。

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
