# Personal AI Assistant

个人 AI 助理的自托管工程基座。当前完成阶段 0：安全配置、FastAPI 服务、Provider 中立的领域契约、PostgreSQL/pgvector、Redis、MinIO、可选 Neo4j，以及 Docker Compose 部署骨架。

完整设计见 [docs/personal-ai-assistant-design.md](docs/personal-ai-assistant-design.md)。

## 当前能力

- FastAPI 应用与版本化 API；
- 存活和依赖就绪检查；
- 请求 ID 与结构化容器日志；
- 生产环境占位凭证校验；
- `ModelGateway`、`Tool`、`KnowledgeRetriever`、`TransportSearchProvider` 领域接口；
- PostgreSQL + pgvector、Redis、MinIO 和可选 Neo4j 的 Compose 服务；
- Alembic 迁移骨架和基础测试。

对话、文档处理、真实天气/票务 Provider 和 GraphRAG 将在后续阶段实现。当前 API 不会伪装成已经具备这些业务能力。

## Docker 快速启动

1. 复制环境变量模板：

   ```powershell
   Copy-Item .env.example .env
   ```

2. 修改 `.env` 中所有 `change-this-*` 密码。生产环境还需设置：

   ```dotenv
   ASSISTANT_ENVIRONMENT=production
   ASSISTANT_LOG_JSON=true
   ```

3. 启动基础服务：

   ```powershell
   docker compose up -d --build
   ```

4. 验证服务：

   ```powershell
   Invoke-RestMethod http://127.0.0.1:18000/api/v1/health/live
   Invoke-RestMethod http://127.0.0.1:18000/api/v1/health/ready
   ```

启用知识图谱服务：

```powershell
docker compose --profile graph up -d
```

启用 Caddy 接入层前，先配置域名解析和 `ASSISTANT_DOMAIN`：

```powershell
docker compose --profile ingress up -d
```

数据库、Redis 默认不映射宿主机端口。API、MinIO 和 Neo4j 的开发端口只绑定在 `127.0.0.1`。API 默认使用宿主机 `18000`，避免与远端现有 `deploy-api-1` 的 `8000` 冲突。

远端旧服务到新助理的并行部署与切换步骤见 [docs/deployment-transition.md](docs/deployment-transition.md)。

## 本地 Python 开发

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn assistant_app.main:app --reload
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check .
```

如果当前网络的默认 PyPI 代理不可用，可显式指定可信镜像，例如：

```powershell
.\.venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements-dev.txt
```

本地运行 API 时需要自行提供 PostgreSQL 和 Redis；存活接口不依赖它们，就绪接口会在依赖不可用时返回 `503`。

## API

- `GET /api/v1/health/live`：进程存活检查。
- `GET /api/v1/health/ready`：PostgreSQL、Redis 就绪检查。
- `GET /docs`：开发和测试环境的 OpenAPI 页面；生产环境关闭。

## 安全说明

- 不要提交 `.env`、真实 API key、Cookie 或票务账号。
- 现有演示代码曾包含硬编码凭证；相关值已从工作区源码中移除，但原凭证仍必须在供应商侧吊销并轮换。
- 生产环境不要直接使用 `.env.example` 中的占位密码。
- 票务查询只应接入官方或授权 Provider；支付和自动购票不属于首期范围。
