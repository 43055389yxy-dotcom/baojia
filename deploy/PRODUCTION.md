# AstraQuote 单 MCP 生产部署

生产版不再启动销售前端、独立 HTTP 后端或远程 GPT/桌面中继。GPT 直接调用一个 AstraQuote MCP：

- MCP 进程直接调用 Python 官方价目库，不经过另一个后端端口。
- MCP 自己生成 Excel，保存到持久化数据目录，并返回带随机令牌的下载链接。
- 仅 AWS 报价同时返回 Excel 和 AWS Pricing Calculator 公开分享链接。
- Azure、OCI、GCP、腾讯云、阿里云、华为云、百度智能云、火山引擎和天翼云只返回 Excel，不调用也不返回官网报价链接。

公网入口仍使用同容器内的 OAuth 边界程序，它只负责登录、令牌和转发，不参与报价。公网 MCP 地址为：

```text
https://baojia.tontianit.com/mcp
```

## 配置

服务器继续使用三个现有环境文件，以便保留云厂商凭据、MCP 内部令牌和 OAuth 客户端：

```text
/home/ec2-user/astraquote/config/backend.env
/home/ec2-user/astraquote/config/mcp.env
/home/ec2-user/astraquote/config/oauth.env
```

`backend.env` 现在只被 MCP 内部的官方价格查询库读取，不会启动后端 Web 服务。AWS 使用服务器现有的 AWS 凭证链；Azure Retail Prices API 和 OCI Price List API 是公开价目接口；其他需要凭据的云厂商继续使用现有只读配置。

Excel 和报价中间状态保存在宿主机：

```text
/home/ec2-user/astraquote/data/v2-quotes
/home/ec2-user/astraquote/data/downloads
```

OAuth 客户端和刷新令牌仍在：

```text
/home/ec2-user/astraquote/data/oauth/oauth.db
```

## 运行结构

`astraquote:production` 一个容器内只启动：

1. `node server.js`：AstraQuote MCP，内置官方价格查询桥接和 Excel 下载。
2. `uvicorn app:app`：公网 MCP 的 OAuth 2.1 安全边界。

不启动 frontend、backend API、Codex relay、Gemini relay、VNC 或浏览器进程。

## 发布与检查

Jenkins 使用 [`deploy/jenkins-shell.sh`](./jenkins-shell.sh) 构建并替换容器。脚本会：

1. 事务性备份 OAuth 数据库并验证客户端数量未减少。
2. 构建单 MCP 镜像并启动容器。
3. 验证 MCP 就绪、OAuth 就绪和带内部令牌的 MCP `initialize`。
4. 更新 Caddy 的 `/mcp` 和 `/downloads/*` 路由。
5. 停用旧的远程 GPT、Gemini 和桌面中继。

上线前运行：

```bash
cd deploy/astraquote-mcp && npm test
cd ../../backend && pytest
```

容器上线后的核心检查：

```bash
docker exec astraquote curl -fsS http://127.0.0.1:8200/readyz
docker exec astraquote curl -fsS http://127.0.0.1:8001/readyz
curl -fsS https://baojia.tontianit.com/.well-known/oauth-protected-resource/mcp
```
