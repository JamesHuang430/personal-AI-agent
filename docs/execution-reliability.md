# 执行可靠性与升级

本次升级需要应用代码、数据库迁移和 worker 一起发布。

## 部署

生产环境使用 `deploy/deploy.sh`：脚本会执行 Alembic 迁移，并启动新增的 `worker` 服务。
首次切换应安排维护窗口，先停止旧版 API 的新任务入口，避免旧版进程继续运行进程内任务。
不要让旧版 API 与新版 worker 同时消费旧任务。

手工部署顺序：

```sh
docker compose build assistant-api
docker compose up -d --wait postgres redis
docker compose run --rm --no-deps assistant-api alembic upgrade head
docker compose up -d assistant-api admin-api worker
```

本地开发时，API 之外还需运行：

```sh
python -m assistant_app.worker
```

worker 与 API 必须使用相同数据库、Redis、密钥和 `/data/generated` 文件卷。
队列使用现有 PostgreSQL，无需增加生产依赖。每个 worker 进程同时处理两个任务。

## 恢复语义

- 业务资源与队列记录同一事务提交。worker 使用 `FOR UPDATE SKIP LOCKED` 领取任务。
- 租约为 90 秒，20 秒续期一次。失去租约会取消执行；旧 owner 无法确认新 owner 的任务。
- 进程崩溃后允许最多三次领取；超出后转失败。业务失败记录保留，可在界面明确继续导演制作。
- 导演恢复复用已完成文本方案和镜头。失败的合成步骤使用已有视频、配音重做。
- 已保存视频供应商任务 ID 时只继续查询和下载，不重复创建供应商任务。
- 供应商不支持可验证的幂等请求时，无法保证“提交成功但响应未保存”的自动恢复。
  此时任务转为失败并提示核对供应商记录，避免盲目重复计费；音乐和付费配音同样保守处理。
- 迁移会接管旧的 queued/processing 任务，导演子任务由导演父任务恢复。
  旧 processing/failed 媒体任务会标记为曾经提交；没有供应商 ID 的旧任务不会自动重新收费。

## 接口与前端

- 聊天视频工具只创建 `awaiting_confirmation` 草稿；必须查看提示词和参数后点击确认。
  `POST /api/v1/videos/{id}/confirm` 校验用户归属、参数摘要及 24 小时有效期。重复确认不会重复排队。
- 聊天前端为每次逻辑请求保存 `Idempotency-Key`。相同请求可取回已完成结果；同键不同参数返回 409。
  失败请求保留执行状态及已知产物，不会自动重做副作用。同一账号同时只运行一个聊天请求。
- 消息保存附件、引用、文件、媒体和导演卡片。旧消息没有这些字段时仍可正常显示文本。
- 导演列表返回摘要和镜头 ID；完整 Agent 交付物通过单项目详情接口读取。
- 模型限流按真实 HTTP 请求执行，涵盖聊天多轮、记忆、embedding 和 Pi Runtime。
- 媒体质检结果的 `scope=technical_integrity` 只表示音视频轨、时长、字幕文本等技术检查。
  对白准确性、字幕实际同步及人物一致性仍需人工复核。

## 日志

认证、内部桥接、日志详情和媒体下载不采集 HTTP 正文；只保留允许的元信息。
其他接口仅采集不超过 32 KiB 的完整 JSON。超限正文整体省略，二进制只计数，不缓冲。
密码、会话 Cookie、授权码及共享密钥会脱敏，单条序列化日志也有限额。
后台读取旧日志时也会应用脱敏，但不会自动改写数据库中的历史记录。
升级前已有日志可能包含凭证，应按实际留存要求清理或脱敏，
并撤销仍有效的泄露会话；不要把历史日志当作已经处理。

## 验证

```sh
python -m pytest
python -m ruff check assistant_app tests migrations
cd pi_runtime && npm test
```

前端 DOM 回归：在项目根目录执行 `npm --prefix tests/web ci` 和
`npm --prefix tests/web test`，覆盖参数确认、下载链接和历史卡片恢复。

数据库回归使用 `TEST_POSTGRES_URL` 指向隔离测试 PostgreSQL，运行
`python -m pytest tests/test_postgres_execution.py`。每个测试只创建并删除自身的随机 schema。
可用 PGlite 验证 SQL、迁移、幂等和租约状态，但它的连接复用实现不能代替真实 PostgreSQL
多进程并发、强制终止和生产负载测试。测试不会调用付费模型。
