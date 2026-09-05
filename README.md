# 知伴 · 懂你的 AI 视频工作室

核心目标：在更了解用户创作喜好的前提下，让用户更方便地生成符合期待的视频。
产品主线是 **创意 → 故事 → 分镜 → 视频与声音 → 字幕 → 合片 → 质检与用户验收**。
聊天用于澄清创作意图、沉淀创作偏好和讨论修改意见；工作室是默认入口。

当前定位、取舍与验收标准见 [视频创作产品方案](docs/video-product-focus.md)。
原个人助理设计已归档，不再作为本期范围。

## 支撑能力

- 邮箱验证码注册与登录，全站每天最多注册 3 个用户；
- 用户端和运营后台登录均有一次性计算验证码，支持邮件重置密码；
- 视频工作室、创意对话与作品验收界面；
- 独立运营后台：用户启停、渠道管理与请求日志；
- 大模型渠道管理：单一当前渠道的 Base URL、加密 API Key 与全局 QPS；
- 用户在对话前端选择模型；优先读取渠道的 OpenAI 兼容模型目录，也可直接输入模型 ID；
- 视频生成渠道管理：Base URL、加密 API Key、模型名、渠道切换与全局 QPS；
- 邮件渠道管理：运营后台配置 SMTP、加密授权码并发送测试邮件；
- Agent 可按对话意图生成安全的文本类文件，并提供用户隔离的下载入口；
- Agent 可通过自托管的免费开源 SearXNG 聚合搜索公开互联网，并在摘要不足时安全提取网页正文；
- 最新信息回答展示可点击来源卡片，网页内容按不可信数据处理并阻止内网地址访问；
- 内置 MCP Streamable HTTP 客户端与工具白名单，默认连接隔离运行的 Microsoft MarkItDown MCP；
- “文档理解 Skill”支持在聊天页上传 PDF、Word、PowerPoint、Excel、CSV 和文本附件，随后直接总结、问答或提取表格；
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

本期不再规划天气、票务、通用生活助理、积分商城或支付。
上述历史基础能力中，账号、渠道、文件、检索与记忆继续服务视频制作；
签到和套餐已从用户产品界面移除，历史数据与兼容接口保留。

视频创作的新能力：

- 可编辑的视觉风格、受众、叙事、节奏、声音与避讳偏好。
- 导演规划检索相关创作记忆；向量检索失败时回退到近期创作偏好。
- 每个项目保存偏好与记忆快照，故事、视觉、导演预演共用，本次要求优先。
- 未启动草案可编辑；新项目在文本规划完成后等待用户确认具体分镜，再生成视频。
- 技术质检与用户验收分开；明确勾选的作品反馈可成为今后的创作记忆。
- 支持 4 秒试片，以一个镜头验证声音、字幕、合片和质检流程。
- 仅将明确的长期创作偏好写入记忆；不将虚构剧情推断为用户经历。

尚未提供精细的逐镜版本编辑、自动内容审片或价格预算承诺，具体边界见产品方案。

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

3. 构建镜像、迁移数据库并启动服务：

   ```powershell
   docker compose build
   docker compose up -d --wait postgres redis
   docker compose run --rm --no-deps assistant-api alembic upgrade head
   docker compose up -d
   ```

   已有环境升级前请先阅读 [执行可靠性与升级](docs/execution-reliability.md)，停止旧版任务入口再切换。

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

联网检索默认启用并在 Compose 内部启动 `SearXNG`，不需要 API Key，也不向公网暴露
搜索服务端口。默认启用百度、搜狗和 Bing 中国站，Agent 遇到最新数据、新闻、
价格、政策或用户明确要求搜索时会自动调用；可通过
`ASSISTANT_WEB_SEARCH_ENABLED=false` 关闭，也可用 `ASSISTANT_WEB_SEARCH_BASE_URL` 指向已有
SearXNG 实例。网页读取限制响应大小和超时，并拒绝本机、内网、保留地址及非标准端口。

文档理解默认启用：聊天页的“回形针”按钮上传附件后，用自然语言提出“总结附件”、
“比较两份报告”或“提取表格”等要求即可调用。页面显示“文档理解 Skill · MCP 就绪”时，
代表基于 Microsoft MarkItDown 引擎的 MCP 服务已连通。MCP 服务仅挂载只读附件卷，并位于无公网出口的内部网络；
应用只允许调用 `convert_to_markdown`。可通过 `ASSISTANT_MCP_ENABLED=false` 整体关闭。

远端旧服务到新助理的并行部署与切换步骤见 [docs/deployment-transition.md](docs/deployment-transition.md)。
生产部署使用 Nginx 作为唯一 Web 入口，并在 `18000`（用户端）和 `19000`（运营后台）终止 HTTPS；详见 [deploy/README.md](deploy/README.md)。

## 可选 Pi Agent Runtime PoC

默认对话仍使用现有 Python Runtime。仓库包含一个隔离的 Pi Agent Core sidecar PoC，
用于对比通用工具循环、并行执行和运行状态能力；它不会获得数据库、文件系统或媒体渠道
权限。启用、回滚、安全边界与验收清单见
[docs/pi-runtime-poc.md](docs/pi-runtime-poc.md)。

## 本地 Python 开发

媒体任务现在由独立 worker 执行；升级与恢复语义见
[执行可靠性与升级](docs/execution-reliability.md)。本地运行 API 后还需启动
`python -m assistant_app.worker`。发布前必须执行 `alembic upgrade head`。

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
- `GET /api/v1/chat/capabilities`：读取已安装 Skill 与 MCP 服务状态。
- `POST /api/v1/files/upload`：上传当前用户的文档附件。
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
