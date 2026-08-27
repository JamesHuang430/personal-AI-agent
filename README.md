# Personal AI Assistant

个人 AI 助理的自托管应用。当前版本包含用户端、运营后台、账号积分体系、文件生成、视频任务和可切换的 OpenAI 兼容模型渠道，使用 FastAPI、PostgreSQL/pgvector、Redis、Nginx 与 Docker Compose 部署。

完整设计见 [docs/personal-ai-assistant-design.md](docs/personal-ai-assistant-design.md)。

## 当前能力

- 邮箱验证码注册与登录，全站每天最多注册 3 个用户；
- 用户端和运营后台登录均有一次性计算验证码，支持邮件重置密码；
- 每位用户每天签到一次并获得 100 积分；
- 用户工作台、积分套餐展示和大模型对话界面；
- 独立运营后台：用户启停、积分调整、套餐管理；
- 大模型渠道管理：单一当前渠道的 Base URL、加密 API Key 与全局 QPS；
- 用户在对话前端选择模型；优先读取渠道的 OpenAI 兼容模型目录，也可直接输入模型 ID；
- 视频生成渠道管理：Base URL、加密 API Key、模型名、渠道切换与全局 QPS；
- 邮件渠道管理：运营后台配置 SMTP、加密授权码并发送测试邮件；
- Agent 可按对话意图生成安全的文本类文件，并提供用户隔离的下载入口；
- Agent 可通过免费开源 DDGS 聚合搜索公开互联网，并在摘要不足时安全提取网页正文；
- 最新信息回答展示可点击来源卡片，网页内容按不可信数据处理并阻止内网地址访问；
- Agent 可创建异步视频任务，查询进度并在完成后下载结果；
- 服务端持久化会话输入输出，并支持最近对话恢复；
- PostgreSQL/pgvector 长期记忆检索与 Apache AGE 知识图谱；
- 用户可查看个人记忆图谱并删除不希望继续使用的记忆；
- 存活和依赖就绪检查；
- 请求 ID 与结构化容器日志；
- 生产环境占位凭证校验；
- `ModelGateway`、`Tool`、`KnowledgeRetriever`、`TransportSearchProvider` 领域接口；
- PostgreSQL 16 + pgvector + Apache AGE、Redis 和可选 MinIO 的 Compose 服务；
- Alembic 迁移骨架和基础测试。

文档处理、真实天气/票务 Provider 和支付将在后续阶段实现。当前界面不会伪装成已经具备这些业务能力。

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

用户端默认访问 `http://127.0.0.1:18000/`，运营后台默认访问
`http://127.0.0.1:19000/`。

pgvector 与 Apache AGE 已包含在 PostgreSQL 服务中，无需单独启动图数据库。数据库和 Redis 默认不映射宿主机端口；MinIO 的开发端口只绑定在 `127.0.0.1`。API 默认使用宿主机 `18000`，避免与远端现有 `deploy-api-1` 的 `8000` 冲突。

向量召回默认使用镜像内置的免费中文模型 `BAAI/bge-small-zh-v1.5`，无需大模型
渠道额外提供 Embeddings API。若要改用渠道的向量模型，可将
`ASSISTANT_MEMORY_EMBEDDING_PROVIDER` 设为 `channel`，并配置对应模型名；向量服务
暂时不可用时，会话、长期记忆和图谱仍会正常保存，并自动使用关键词回退检索。

联网检索默认启用并使用 MIT 许可证的 `DDGS`，不需要 API Key。Agent 遇到最新数据、
新闻、价格、政策或用户明确要求搜索时会自动调用；可通过
`ASSISTANT_WEB_SEARCH_ENABLED=false` 关闭。搜索服务只读取公开 HTTP/HTTPS 页面，限制
响应大小和超时，并拒绝本机、内网、保留地址及非标准端口。

远端旧服务到新助理的并行部署与切换步骤见 [docs/deployment-transition.md](docs/deployment-transition.md)。
生产部署使用 Nginx 作为唯一 Web 入口，并在 `18000`（用户端）和 `19000`（运营后台）终止 HTTPS；详见 [deploy/README.md](deploy/README.md)。

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
- `POST /api/v1/auth/register`：邮箱注册。
- `GET /api/v1/auth/captcha`：创建一次性登录验证码。
- `POST /api/v1/auth/register/email-code`：发送注册邮箱验证码。
- `POST /api/v1/auth/password-reset/request`：发送一次性密码重置链接。
- `POST /api/v1/users/check-in`：每日签到。
- `GET /api/v1/chat/models`：动态读取当前 LLM 渠道的 OpenAI 兼容模型目录。
- `POST /api/v1/chat`：使用用户选择的模型调用当前启用的 LLM 渠道。
- `GET /api/v1/files`：列出当前用户由 Agent 生成的文件。
- `GET /api/v1/videos`：列出当前用户的视频生成任务。
- `GET /api/v1/admin/model-channels`：运营后台渠道清单（仅 `19000` 服务提供）。
- `GET /api/v1/admin/video-channels`：运营后台视频渠道清单（仅 `19000` 服务提供）。
- `GET /api/v1/admin/email-channel`：运营后台邮件渠道配置（授权码不回显）。
- `GET /docs`：开发和测试环境的 OpenAPI 页面；生产环境关闭。

## 安全说明

- 不要提交 `.env`、真实 API key、Cookie 或票务账号。
- 现有演示代码曾包含硬编码凭证；相关值已从工作区源码中移除，但原凭证仍必须在供应商侧吊销并轮换。
- 生产环境不要直接使用 `.env.example` 中的占位密码。
- 票务查询只应接入官方或授权 Provider；支付和自动购票不属于首期范围。
