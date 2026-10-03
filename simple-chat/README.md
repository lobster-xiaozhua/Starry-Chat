# Simple Chat

一个最小可用的流式对话服务：网页发送消息，服务端调用大模型并以 SSE 逐字返回。

## 功能列表

1. **流式对话**：`POST /api/chat` 返回 SSE 流，事件序列为 `start → delta* → done`（异常时为 `error`）。
2. **多轮上下文**：自动携带最近 40 条消息，并按 `MAX_CONTEXT_TOKENS` 预算从最旧开始裁剪，超长对话不会打挂上游。
3. **会话管理**：会话列表按最近活跃排序，支持查看历史消息与级联删除。
4. **消息持久化**：用户消息在流开始前落库，助手消息在流结束时落库；客户端中途断连也会保存已生成的部分内容。
5. **统一错误处理**：所有错误体恒为 `{"error": {"code", "message"}}`；流开始前用 HTTP 状态码，流开始后用带内 `error` 事件。

## 快速开始

```bash
# 1. 创建虚拟环境
python3 -m venv .venv && source .venv/bin/activate

# 2. 安装依赖
pip install -r requirements.txt

# 3. 复制配置并编辑 API Key
cp .env.example .env
#    编辑 .env，至少填好 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL

# 4. 启动服务（默认 http://127.0.0.1:8000）
python run.py

# 5. 浏览器打开 http://127.0.0.1:8000 开始对话
```

## 接口示例

```bash
# 新建会话并流式对话（-N 关闭 curl 缓冲，否则看不到逐字效果）
curl -N -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "用一句话介绍 FastAPI"}'

# 继续已有会话
curl -N -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"message": "再详细一点", "conversation_id": "<上一步返回的 id>"}'

# 会话列表
curl http://127.0.0.1:8000/api/chat/conversations

# 某个会话的历史消息
curl http://127.0.0.1:8000/api/chat/conversations/<id>/messages

# 删除会话（级联删除其消息）
curl -X DELETE http://127.0.0.1:8000/api/chat/conversations/<id>
```

## 项目结构

```
simple-chat/
├── app/
│   ├── main.py            # FastAPI 入口：静态挂载、CORS、全局异常处理
│   ├── config.py          # pydantic-settings 配置
│   ├── schema.py          # 请求 / 响应模型
│   ├── deps.py            # 依赖注入（DB 连接、LLM 客户端）
│   ├── db.py              # SQLite 连接、建表、取消路径同步落库
│   ├── errors.py          # AppError 与统一错误体
│   ├── chat/              # 对话领域
│   │   ├── service.py     # 会话/消息读写、上下文裁剪、标题生成
│   │   └── router.py      # /api/chat 路由与 SSE 生成器
│   ├── auth/              # v0.2 认证：本地账号 + 签名 Cookie
│   │   ├── service.py     # 注册/登录业务（scrypt 哈希、统一失败响应）
│   │   ├── session.py     # HMAC 签名会话令牌与 Cookie 属性
│   │   ├── passwords.py   # hashlib.scrypt 哈希/校验
│   │   ├── ratelimit.py   # 登录/注册独立限流桶
│   │   └── router.py      # /api/auth/* 路由
│   ├── llm/
│   │   ├── client.py      # OpenAI 异步封装与异常映射
│   │   └── tokenizer.py   # tiktoken 估算（不可用时退化为字符近似）
│   └── web/static/        # index.html + app.js（原生 JS，无构建）
├── tests/                 # pytest，打桩 LLM，不触网
├── run.py                 # 开发启动入口
├── requirements.txt
└── .env.example
```

## 配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `APP_ENV` | `development` | `production` 时强制校验 `LLM_API_KEY`，且不对外暴露异常详情 |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/chat.db` | 数据库文件路径 |
| `LLM_BASE_URL` | `http://127.0.0.1:3000/v1` | OpenAI 兼容端点 |
| `LLM_API_KEY` | 空 | 开发环境可留空；生产必填 |
| `LLM_MODEL` | `sensenova-6.8-flash-lite` | 对话模型 |
| `LLM_MAX_TOKENS` | `8192` | 单次回答上限 |
| `LLM_TEMPERATURE` | `0.7` | 采样温度 |
| `LLM_MAX_RETRIES` | `3` | 上游可恢复错误（429/5xx/超时）的重试次数，指数递增退避 |
| `MAX_CONTEXT_TOKENS` | `8192` | 上下文 token **总预算**（含系统提示与回复预留），非单次回答的 `max_tokens`；超出从最旧丢弃 |
| `CORS_ORIGINS` | `*` | 逗号分隔的允许来源；**生产环境切勿设为 `*`** |
| `LOG_LEVEL` | `INFO` | 日志级别（开发可设 DEBUG） |
| `RATE_LIMIT_ENABLED` | 由环境推导 | 限流开关；不设置时：生产=开、开发=关。可显式设为 true/false |
| `PROXY_HEADERS` | `true` | 是否信任反向代理的 X-Forwarded-* 头（仅生产生效） |
| `FORWARDED_ALLOW_IPS` | `*` | 允许设置转发头的代理 IP（生产建议收敛为具体网段） |
| `TRUSTED_PROXIES` | `[]` | 受信反向代理 IP 列表；**仅当非空**时才采信 `X-Forwarded-For` 取真实客户端 IP（防伪造 XFF 绕过限流） |
| `WORKERS` | 由 CPU 核心数推导 | 生产 uvicorn worker 数；不设置则取 CPU 核心数 |
| `MAX_BODY_BYTES` | `1048576` | 请求体大小上限（1MB），超出返回 413 |
| `AUTH_ENABLED` | 由环境推导 | v0.2 认证开关；不设置时：生产=开、开发=关。关闭时保留 `X-User-Id` 兼容路径 |
| `SESSION_SECRET` | 空 | 会话 Cookie 的 HMAC 签名密钥；**生产 + 认证开启时强制 >= 32 字符**，否则启动失败。轮换即全员登出 |
| `SESSION_TTL_SECONDS` | `604800` | 会话有效期（7 天） |
| `SESSION_COOKIE_NAME` | `sc_session` | 会话 Cookie 名 |
| `SESSION_COOKIE_SECURE` | 由环境推导 | Cookie `Secure` 属性；不设置时生产=开（需 HTTPS），纯 HTTP 部署显式设 `false` |
| `AUTH_RATE_LIMIT_ENABLED` | `true` | 登录/注册独立限流桶开关 |
| `AUTH_RATE_LIMIT_MAX` | `10` | 登录/注册窗口内失败次数上限，超出 429 |
| `AUTH_RATE_LIMIT_WINDOW_SECONDS` | `60` | 认证限流窗口（秒） |
| `SCRYPT_N` / `SCRYPT_R` / `SCRYPT_P` | `16384` / `8` / `1` | 密码哈希成本参数（标准库 `hashlib.scrypt`；N 必须为 2 的幂） |

> 说明：`RATE_LIMIT_ENABLED` / `WORKERS` 留空时按运行环境自动推导，无需手动设置；仅在需要覆盖默认行为时才显式赋值。

## 测试

```bash
# 安装测试依赖（含并行执行插件 pytest-xdist）
pip install -r requirements-dev.txt

# 推荐：多进程并行执行（自动按 CPU 数），本地约减半耗时
pytest -n auto -q

# 最快反馈：仅跑单测，跳过较慢的集成测试
pytest -m "not integration" -n auto -q

# 仅跑集成/端到端
pytest -m integration -n auto -q

# 覆盖率报告（可选，会略微拖慢）
pytest -n auto --cov=app --cov-report=term-missing

# 或用 Makefile
make test
```

测试通过 `ASGITransport` 直接调用应用，并将 LLM 打桩为固定分片，**不需要网络和 API Key**。
为提速，整测试会话共用一个临时 SQLite 文件（`lifespan` 与建表只跑一次），每个用例在自身连接内
**截断所有表**以获得与“每用例全新库”等价的隔离，互不污染；应用层状态（LLM 桩、认证开关、
限流桶、指标）仍由 autouse 夹具逐用例复位。

覆盖率目标：`app/chat/service.py >= 80%`、`app/chat/router.py >= 70%`、`app/llm/client.py >= 60%`。

## 已知边界与演进路线

本服务按 `docs/ROADMAP.md` 的版本格递进式扩展能力。每个新能力默认关闭（`*_ENABLED` 配置开关），回滚 = 关开关。当前已落地的能力格：

- **v0.2 多用户与认证**：本地账号 + scrypt + 签名 HttpOnly Cookie；`AUTH_ENABLED`。
- **v0.3 多模型路由**：纯规则路由（不调用模型做路由）+ fallback + 每用户日 token 预算；`ROUTING_ENABLED` / `BUDGET_ENABLED`。
- **v0.4 工作记忆**：每 N 轮摘要（禁向量数据库，摘要只压缩滑出窗口的旧消息）；`MEMORY_ENABLED`。
- **v0.5 工具调用**：`calculate` / `read_file` / `web_search` 三工具，沙盒执行（CPU 1s / 内存 128MB / 禁网）；`TOOLS_ENABLED`（子系统已就绪，主聊天流集成进行中）。
- **v0.6 RAG**：SQLite FTS5 + BM25 检索（禁向量数据库）；`RAG_ENABLED`（子系统已就绪，主聊天流集成进行中）。

后续版本格（v0.7 Agent / v0.8 多模态）见 `docs/ROADMAP.md`。仍有意不做：OAuth/SSO/RBAC、JWT refresh、向量数据库（v0.6 前）、`eval`/`exec` 实现工具、用生成器+长轮询承载 Agent、Kafka/Redis/RabbitMQ、用大模型做路由。

## 认证（v0.2）

- 端点：`POST /api/auth/register`、`POST /api/auth/login`、`POST /api/auth/logout`、`GET /api/auth/me`。
- 口令：标准库 `hashlib.scrypt` + 每用户随机 salt，只存哈希；响应体与日志绝不包含 `password_hash` 或明文密码。
- 会话：HMAC-SHA256 签名 Cookie（无 session 表），`HttpOnly; SameSite=Strict; Path=/`，生产自动加 `Secure`。
- 隔离：认证开启时 chat / conversation 一律按 Cookie 中的已验证 user_id 归属；未登录或被篡改 → 401 `UNAUTHORIZED`；访问他人会话统一 404（不泄露存在性）。
- 防爆破：登录/注册失败按 IP + 用户名进独立滑动窗口桶（默认 10 次/60s），超限 429；失败文案统一，且未知用户也执行一次 scrypt，避免账号枚举与时序探测。
- 兼容路径：`AUTH_ENABLED=false` 时保留 v0.1 的 `X-User-Id` 头；生产默认开启认证。
---

# 生产部署

本仓库已补充一组生产就绪能力，部署时请重点注意反向代理与进程管理，否则流式响应会出现卡顿数秒后一次性喷出的现象。

## 1. 配置分层（development / production）

run.py 与 app.config 按 APP_ENV 自动切换行为：

| 维度 | development（默认） | production |
|---|---|---|
| 热重载 | reload=True，监听 127.0.0.1:8000 | reload=False，监听 0.0.0.0（或经代理） |
| CORS | CORS_ORIGINS=* | 必须设为具体前端域名，禁止 * |
| 日志级别 | LOG_LEVEL=DEBUG | LOG_LEVEL=INFO |
| 限流 | 关闭 | 开启（内存滑动窗口，单实例） |
| 代理头 | 不信任 | proxy_headers=True + forwarded_allow_ips（信任反代转发的 X-Forwarded-*） |
| Worker | 单进程 | workers = CPU 核心数（多 worker 时内存状态按进程隔离，见限流说明） |

生产启动（推荐显式指定环境）：

    APP_ENV=production python run.py
    # 或自定义端口 / worker：
    APP_ENV=production HOST=0.0.0.0 PORT=8000 WORKERS=4 python run.py

不要在生产使用 --reload。

## 2. 反向代理（Nginx 关键项）

流式 SSE 必须关闭代理缓冲，否则 Nginx 会等响应完整才下发，前端表现为卡 5 秒后一次性喷出：

    upstream simple_chat {
        server 127.0.0.1:8000;
    }

    server {
        listen 80;
        server_name chat.example.com;

        # 限制请求体（双保险，应用层另有 1MB 校验）
        client_max_body_size 1m;

        location / {
            proxy_pass http://simple_chat;

            # 关键：关闭缓冲，保证 SSE 逐字下发
            proxy_buffering off;
            proxy_cache off;
            proxy_read_timeout 300s;
            proxy_send_timeout 300s;

            proxy_set_header Host $host;
            proxy_set_header X-Real-IP $remote_addr;
            proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header X-Forwarded-Proto $scheme;

            proxy_set_header Connection "";
            proxy_http_version 1.1;
            chunked_transfer_encoding on;
        }
    }

要点：
- proxy_buffering off; + proxy_cache off;：禁用缓冲/缓存。
- proxy_read_timeout 300s;：长流式回复需要足够大的读取超时。
- client_max_body_size 1m;：与应用的 MAX_BODY_BYTES 一致，双重防护。
- 若走 HTTPS，记得 X-Forwarded-Proto $scheme，应用层 proxy_headers=True 才会据此正确解析。

## 3. 进程管理（不要 nohup）

使用 systemd 或 supervisor 托管，便于崩溃自启与日志归集。不要用 nohup ... &（无监督、无自启）。

systemd 示例 /etc/systemd/system/simple-chat.service：

    [Unit]
    Description=Simple Chat API
    After=network.target

    [Service]
    User=app
    WorkingDirectory=/opt/simple-chat
    EnvironmentFile=/opt/simple-chat/.env
    ExecStart=/opt/simple-chat/.venv/bin/python run.py
    Restart=always
    RestartSec=3
    Environment=APP_ENV=production

    [Install]
    WantedBy=multi-user.target

    sudo systemctl daemon-reload
    sudo systemctl enable --now simple-chat
    journalctl -u simple-chat -f   # 日志为结构化 JSON，可直接接入 Loki / ELK

supervisor 等价配置（节选）：

    [program:simple-chat]
    command=/opt/simple-chat/.venv/bin/python run.py
    directory=/opt/simple-chat
    environment=APP_ENV="production"
    autostart=true
    autorestart=true
    redirect_stderr=true
    stdout_logfile=/var/log/simple-chat.out.log

## 4. 健康检查与就绪探针

- GET /healthz -> 200 {"status":"ok","version":"0.1.0","ts":"..."}。不查数据库，用于存活探针（liveness）。
- GET /readyz -> 检查 ① 数据库可达 ② LLM_API_KEY 非空 ③ 模型轻量可达（一次 max_tokens=1 调用，超时 5s）。
  - 全部通过：200 {"status":"ok","checks":{"db":"ok","llm_key":"ok","model":"ok"}}
  - 任一失败：503 {"status":"unavailable","reason":"...","error":"...","checks":{...}}

K8s 示例：

    livenessProbe:
      httpGet: { path: /healthz, port: 8000 }
      initialDelaySeconds: 5
      periodSeconds: 10
    readinessProbe:
      httpGet: { path: /readyz, port: 8000 }
      initialDelaySeconds: 10
      periodSeconds: 15

## 5. 依赖漏洞扫描

    # 安装：pip install pip-audit
    make audit
    # 或：pip-audit -r requirements.txt

## 6. 限流（单实例，内存实现）

- 策略：以 user_id + IP 为键的 60s 滑动窗口，上限 30 次。
- 超限：429，并返回 Retry-After: 60、X-RateLimit-Limit: 30、X-RateLimit-Remaining: 0。
- 白名单：localhost / 127.0.0.1 不限制（开发本机调试友好）。
- 单实例约束：计数器保存在进程内存。多实例 / 多 worker 部署时，每个进程各自计数，限流不再全局准确。
  生产若需多副本，请将 app/middleware/rate_limit.py 中的内存结构替换为 Redis + Lua（原子化滑动窗口），否则只能做到每实例 30 次/60s 的弱限流。

## 7. 客户端断连与计费释放

- 前端通过 AbortController（关闭标签页 / 点停止）中断请求；服务端在生成器内捕获 asyncio.CancelledError，
  调用 OpenAI 流的 aclose() 释放上游连接，并记一条 client_disconnected 结构化日志，避免继续计费与悬挂连接。
- 取消路径下无法 await，部分已生成内容由 app/db.py:persist_partial_sync 同步落库，保证用户看到多少、库里存多少。

---

# 安全清单

| 项 | 状态 | 说明 |
|---|---|---|
| API Key 不进 Git | 已完成 | .env + .gitignore（.env 已忽略，仅提交 .env.example） |
| 生产 CORS 不为 * | 已完成 | 生产 CORS_ORIGINS 必须填具体域名；配置分层已强制区分 |
| 用户输入进 SQL 必须参数化 | 已完成 | 全部 SQL 均用 ? 占位符，无 f-string 拼 SQL |
| XSS：前端 innerHTML 前转义 / DOMPurify | 已完成 | 用户输入走 textContent；Markdown 经 DOMPurify.sanitize（app/web/static/app.js） |
| 会话归属校验（user_id 匹配）防 ID 遍历 | 已完成 | 会话绑定 user_id，非本人访问统一 404 |
| 用户口令只存慢哈希 | 已完成 | v0.2：`hashlib.scrypt`（随机 salt，参数见 `SCRYPT_*`）；密码不落明文、不进日志 |
| 会话 Cookie 防伪造 / XSS 窃取 | 已完成 | HMAC-SHA256 签名 + 常量时间验签；`HttpOnly; SameSite=Strict; Secure`（生产） |
| 登录防爆破 | 已完成 | v0.2：IP + 用户名独立限流桶（默认 10 次/60s → 429），统一失败响应 |
| 会话密钥生产强制非空 | 已完成 | 生产 + 认证开启时 `SESSION_SECRET` < 32 字符会在启动期直接报错 |
| 请求体大小限制（1MB） | 已完成 | BodySizeLimitMiddleware + Nginx client_max_body_size 双保险 |
| 依赖漏洞扫描 | 已完成 | make audit（pip-audit） |
| 结构化日志不泄露密钥 / 全文 | 已完成 | API Key 显示为 sk-***<后4位>，Authorization 置 ***；从不记录 message.content |
| 生产禁用 --reload | 已完成 | run.py 生产分支 reload=False，由 systemd/supervisor 托管 |
| 反向代理关闭缓冲 | 需运维 | 见上文 Nginx 配置 |
| 多实例限流 / 并发锁一致性 | 需评估 | 当前为单实例内存实现；多副本需换 Redis+Lua / 外部锁 |

注：v0.2 起本地账号认证已落地（scrypt 密码哈希 + 签名 HttpOnly Cookie）；`AUTH_ENABLED=false`
仅用于回滚到 `X-User-Id` 兼容路径，该路径下 user_id 仍可被客户端伪造，生产环境请保持认证开启。

# 发布

## 端到端验收

服务启动后，在仓库根目录执行：

```bash
bash scripts/e2e.sh
```

脚本依赖 `curl` 和 `jq`，默认访问 `http://127.0.0.1:8000`，也可通过 `BASE` 指定地址：

```bash
BASE=https://chat.example.com bash scripts/e2e.sh
```

验收覆盖健康检查、流式 SSE、SSE `done` 事件中的会话 ID、多轮对话、会话列表与删除、生产限流（第 31 次起返回 429）以及空消息 422。限流步骤使用 `X-Forwarded-For` 与 `X-User-Id` 隔离测试流量；请仅在受控的验收环境运行。

认证链路验收（服务需以 `AUTH_ENABLED=true` 启动）：

```bash
bash scripts/e2e_auth.sh
```

覆盖注册/登录/登出、Cookie 属性（HttpOnly + SameSite=Strict）、伪造 Cookie 401、
A/B 会话隔离（B 访问 A 的会话返回 404）与登录失败限流 429。

## 成本模型

单次对话成本 = `(prompt_tokens + completion_tokens) * 模型单价`

上下文裁剪直接决定成本：上下文越长，每次调用越贵。MVP 建议：

- `MAX_CONTEXT_TOKENS = 8192`
- `MAX_RESPONSE_TOKENS = 1024`

月成本估算 = `日均对话数 × 平均轮次 × 平均 token 数 × 单价 × 30`

## 成本（/api/admin/cost）

按用户聚合的 token 用量与成本账单（PR-3 改动 2）：

```bash
# 未配置 ADMIN_API_KEY 时端点返回 501（local-first：不强制启用）
curl 'http://localhost:8000/api/admin/cost?from=2026-09-25&to=2026-10-02' \
     -H "X-Admin-Key: $ADMIN_API_KEY"
```

返回 `by_user`（每用户 prompt/completion tokens、cost_usd、requests）与 `total`，
直接 SQL SUM + GROUP BY 聚合，结果缓存 60s。时间范围默认最近 7 天，最大 90 天。

价格按模型配置（未配置则 cost_usd=0，不报错）：

```bash
# .env —— 注意 pydantic-settings 对 dict 只接受 JSON 格式
MODEL_PRICE={"sensenova-6.8-flash-lite": {"prompt": 0.15, "completion": 0.60}}
ADMIN_API_KEY=change-me
```

**防止单用户刷爆账单**：

1. 限流（见【部署 → 限流】）：生产环境默认 30 次/60s（user_id+IP 维度），
   可按 `RATE_LIMIT` 收紧；对重点用户可在反向代理层（`docs/nginx.conf` 的
   `limit_req`）再叠一层 IP 级漏桶。
2. 每日盯 `cost_usd` 增量：`/api/admin/cost?from=<今天>` 的 `total.cost_usd`
   就是当日累计账单，接告警超阈值即告警。
3. `MAX_CONTEXT_TOKENS`（默认 8192）直接决定单次请求的 prompt 成本上限。

## 上线前

- [ ] 全部测试通过，覆盖率达标
- [ ] `.env` 已按生产值配置，API Key 已轮换
- [ ] `CORS_ORIGINS` 已设为前端域名（非 `*`）
- [ ] 数据库文件目录已存在且权限 700，属主为运行用户
- [ ] 已配置 logrotate 或日志大小上限（避免 SQLite 日志撑满磁盘）
- [ ] 已配置 systemd 重启策略（`Restart=on-failure`, `MaxRestart=5`）
- [ ] 已压测：wrk / k6 模拟 50 并发流式请求，观察：
  - 内存增长是否收敛（无泄漏）
  - 单请求 P95 延迟 < 3s（首 token 延迟）
  - SQLite 无 `database is locked` 错误
- [ ] 已验证 Nginx 关闭 `proxy_buffering`
- [ ] 已验证客户端断开时服务端 LLM 请求被取消（看日志 `client_disconnected`）
- [ ] 已备份数据库（cron + `sqlite3 .backup`）

## 观测（/metrics）

> **没有首 token 延迟指标就不要做性能优化。** 一切“优化”以
> `chat_first_token_seconds` 的 P95 变化为唯一裁判：改前抓基线，改后对同口径分位数。

`GET /metrics` 输出 Prometheus text format（纯手写实现，无 prometheus_client
依赖），指标清单：

| 指标 | 类型 | 说明 |
|---|---|---|
| `chat_requests_total` | counter | 对话请求总数（model / error_code / status） |
| `chat_first_token_seconds` | histogram | 【最关键】首 token 延迟 |
| `chat_duration_seconds` | histogram | 完整流耗时（p50/p95/p99） |
| `chat_tokens_total` | counter | token 用量（prompt / completion） |
| `chat_active_streams` | gauge | 当前活跃流数 |
| `conversation_messages_total` | counter | 落库消息数（按 role） |
| `llm_retries_total` | counter | 上游重试次数 |
| `db_query_seconds` | histogram | DB 查询耗时（按 op） |
| `locks_contended_total` | counter | 409 会话并发冲突次数 |
| `context_truncated_total` | counter | 上下文截断次数 |

PromQL 片段与最小面板配置见 `docs/observability.md`；Grafana 面板截图占位：

![Grafana 面板截图占位](docs/assets/grafana-dashboard.png)

压测用 `scripts/load_test.py`（`pip install -r requirements-dev.txt` 后运行）：

```bash
python scripts/load_test.py --concurrency 50 --duration 600 --url http://localhost:8000
```

- [ ] 指标：请求 QPS、P50/P95/P99 延迟、流式首 token 延迟、token 用量/用户/天、错误率（按 `error_code` 分）、活跃流数
- [ ] 告警：5xx 率 > 1%、P95 首 token > 2s、LLM 不可用连续 3 次、磁盘剩余 < 20%、API Key 余额不足
- [ ] 仪表盘：单图看“请求量 vs 错误率 vs 成本”

## 回滚

- [ ] git tag 版本（`v0.1.0`）
- [ ] 数据库 schema 向后兼容（只加字段，不改含义）
- [ ] 回滚命令：`systemctl stop simple-chat && git checkout v0.0.1 && systemctl start simple-chat`
- [ ] 数据库回滚：SQLite 无迁移工具，靠备份文件还原。
