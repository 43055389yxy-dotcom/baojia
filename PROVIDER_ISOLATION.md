# AstraQuote 双程序隔离

AWS 与 Microsoft Azure 是两个独立运行的报价程序，不再通过同一页面切换。

## AWS 程序

- 前端目录：`frontend`
- 前端地址：`http://localhost:3000`
- 后端入口：`app.aws_main:app`
- 后端地址：`http://127.0.0.1:8000`
- 数据目录：`backend/.cache/aws`
- 客户确认编号前缀：`aws_`
- 报价任务前缀：`aws-`

## Microsoft Azure 程序

- 前端目录：`azure-frontend`
- 前端地址：`http://localhost:3001`
- 后端入口：`app.azure_main:app`
- 后端地址：`http://127.0.0.1:8001`
- 数据目录：`backend/.cache/azure`
- 客户确认编号前缀：`azure_`
- 报价任务前缀：`azure-`

## 隔离规则

1. 两个前端分别生成自己的客户链接、会话状态和地区状态。
2. 两个接口代理会拒绝另一云的接口、任务和确认链接。
3. 两个后端只初始化本云的目录、模板、缓存、确认单和报价队列。
4. 两套 SQLite 数据位于不同物理目录，不进行跨云读取或回退。
5. 允许复制已经验证的业务流程，但不得共享运行时状态或产品数据。

## 本地启动

AWS 后端：

```bash
cd backend
.venv/bin/uvicorn app.aws_main:app --host 127.0.0.1 --port 8000
```

AWS 前端：

```bash
cd frontend
npm run dev
```

Azure 后端：

```bash
cd backend
.venv/bin/uvicorn app.azure_main:app --host 127.0.0.1 --port 8001
```

Azure 前端：

```bash
cd azure-frontend
npm run dev
```
