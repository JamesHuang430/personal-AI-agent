# 远端服务器并行部署与切换方案

## 1. 已知现状

远端 Ubuntu 当前运行：

| 容器 | 当前端口 | 切换策略 |
|---|---|---|
| `deploy-api-1` | `8000` 对公网 | 新助理验证完成后停止 |
| `deploy-web-1` | `38880`、`38885` 对公网 | 新前端验证完成后停止 |
| `deploy-mailpit-1` | `1025`、`8025` 对公网 | 确认不再需要邮件测试后停止 |
| `deploy-mongo-1` | 仅 Docker 内网 | 暂不处理，先确认是否还有其他消费者 |
| `deploy-redis-1` | 仅 Docker 内网 | 暂不处理，先确认是否还有其他消费者 |

新助理使用独立 Compose project `personal-ai-assistant`、独立网络和独立卷。新 API 默认仅绑定 `127.0.0.1:18000`，不会与旧 API 的 `8000` 冲突。新 PostgreSQL 和 Redis 不映射宿主机端口，因此也不会与旧 MongoDB/Redis 冲突。

## 2. 切换原则

- 旧系统保持在线，直到新 API、数据服务、备份和入口全部验证通过。
- 不复用旧 Redis/MongoDB；避免数据结构、密码和生命周期互相影响。
- 第一次启动不启用 `ingress` profile，先通过服务器本机的 `127.0.0.1:18000` 验证。
- 域名或反向代理一次只切一个入口，保留快速回滚路径。
- 只停止已明确授权的旧 API、Web 和 Mailpit；MongoDB/Redis 在确认无消费者前不停止或删除。

## 3. 部署前只读检查

在服务器项目目录运行：

```bash
bash scripts/server_preflight.sh
```

重点确认：

- 至少 4 vCPU、8 GB 内存和约 40 GB 可用磁盘，才能较舒适地同时运行旧系统和完整新栈；
- `18000`、`9000`、`9001` 未被占用；
- 如果准备启用 Caddy，确认宿主机 `80/443` 未被其他进程或容器占用；
- Docker Compose v2 可用；
- 云安全组不开放 PostgreSQL、Redis、MinIO 的数据端口。

当前 `deploy-mailpit-1` 的 SMTP `1025` 和 Web `8025` 绑定在 `0.0.0.0`。如果这些测试服务不需要公网访问，应立即通过云安全组限制来源；正式切换后停止 Mailpit。

## 4. 并行启动

```bash
cp .env.example .env
chmod 600 .env
```

修改全部占位密码，并至少检查：

```dotenv
ASSISTANT_ENVIRONMENT=production
ASSISTANT_LOG_JSON=true
ASSISTANT_API_PORT=18000
ASSISTANT_MEMORY_ENABLED=true
ASSISTANT_MEMORY_EMBEDDING_MODEL=
```

启动不含公网数据库端口的基础栈：

```bash
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:18000/api/v1/health/live
curl --fail http://127.0.0.1:18000/api/v1/health/ready
```

检查日志中没有凭证、数据库连接串或异常栈泄露：

```bash
docker compose logs --tail=200 assistant-api
```

Apache AGE 与 pgvector 已内置在 PostgreSQL 16 镜像中，不再启动独立图数据库。

## 5. 入口切换

有现成 Nginx/Caddy 时，优先把一个测试子域名反代到 `127.0.0.1:18000`，完成浏览器与 HTTPS 验证。若使用本项目 Caddy，先确认 `80/443` 空闲，再运行：

```bash
docker compose --profile ingress up -d
```

切换前验收：

- `/api/v1/health/live` 和 `/ready` 连续健康；
- 服务重启后数据卷仍存在；
- 完成一次 PostgreSQL/MinIO 备份与恢复演练；
- 登录、工具权限和文档删除链路在对应功能完成后通过；
- 新入口 HTTPS、访问控制和安全响应头正常。

## 6. 停止旧服务与回滚

在旧项目的 Compose 目录中优先使用：

```bash
docker compose -p deploy stop api web mailpit
```

如果旧 Compose 文件已经不存在，可按已知容器名停止：

```bash
docker stop deploy-api-1 deploy-web-1 deploy-mailpit-1
```

停止后观察新系统至少一个完整使用周期。不要立即删除旧容器、镜像、卷、MongoDB 或 Redis。

如需回滚：

```bash
docker start deploy-api-1 deploy-web-1 deploy-mailpit-1
```

然后将域名或反向代理恢复到旧入口。回滚确认完成前，不执行 `docker compose down -v`、`docker rm` 或卷删除。
