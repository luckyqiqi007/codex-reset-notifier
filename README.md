# Codex Reset Notifier

将 Telegram 公开频道中的 Codex 额度重置消息转发到企业微信群。Python 3.12+，仅使用标准库；支持配置文件、Docker Compose 和 Kubernetes。

Forward public Telegram channel announcements to a WeCom group webhook, with persistent deduplication and a retry queue. No Telegram account, bot token, or OpenAI API key is required.

本项目与 OpenAI、Telegram、企业微信及频道运营者无隶属关系。默认来源是第三方公开频道 [`codexreset`](https://t.me/s/codexreset)。转发公告不等于确认个人账号额度已到账，也不预测下一次重置，不自动使用或兑换额度。

## 工作方式

```text
Telegram 公开频道 ──直连 / HTTP CONNECT 代理 / 固定 HTTPS relay──┐
                                                               ↓
                                                   解析 → SQLite 去重/队列
                                                               ↓
                                                       企业微信机器人
```

- 每轮完成后等待 60 秒；首次成功抓取只建立基线，不刷屏补发历史。
- 默认处理 48 小时内的额度/重置相关消息；已通知消息的更正也会转发。
- SQLite 保存游标、内容摘要和投递状态，重启后继续去重。必须单副本运行。
- 失败最多重试 8 次，退避 30 秒到 15 分钟；超过 48 小时不再投递。
- 保留原文、时间和来源链接；不执行频道文字中的指令。
- 无入站端口。健康检查和日志用于检查采集与投递情况。

## 本地快速开始

需要 Python 3.12 或更新版本，以及能访问 `t.me` 和 `qyapi.weixin.qq.com` 的运行环境。

```sh
cp config.example.toml config.toml
chmod 600 config.toml
```

编辑 `config.toml`，将企业微信机器人的 Webhook 填入 `[wecom]`：

```toml
[wecom]
webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_WEBHOOK_KEY"
```

先做离线配置检查，再读取一次真实来源（不会发送，也不会创建数据库）：

```sh
python3 notifier.py --config config.toml --check-config
python3 notifier.py --config config.toml --dry-run
```

需要验证群内收件时显式执行以下命令。它会发送 **一条真实的验收测试消息**，即使 `delivery_enabled = false`：

```sh
python3 notifier.py --config config.toml --send-test SETUP-001
```

确认群内收到后，将 `[service]` 的 `delivery_enabled` 改为 `true`，再启动：

```sh
python3 notifier.py --config config.toml
```

修改配置后需要重启进程。不要同时启动两个共享同一状态库的实例。`delivery_enabled = false` 的常驻进程仍会更新基线，期间观察到的消息不会在开启发送后补发；只想检查来源时用 `--dry-run`。

## 配置与密钥

Webhook **不写入源码或镜像**。`config.example.toml` 是可提交模板，`config.toml` 是被 Git 忽略的本地文件。镜像仅复制两个 Python 源码文件；`.dockerignore` 采用白名单，配置、数据库和 `.git` 不进入构建上下文。

也可以让配置引用独立 Secret 文件。删除 `webhook_url` 行后，改成：

```toml
[wecom]
webhook_url_file = "secrets/wecom-webhook-url"
```

文件中只放 Webhook 原文，不加引号。`secrets/` 同样被 Git 忽略。保护配置和密钥文件的权限；容器运行用户为 UID/GID `10001`，挂载的文件必须可由该用户读取。

优先级：**环境变量 → TOML 中的值或指定文件 → 兼容的 Secret 挂载 → 默认值**。同层的明文值与对应 `_file` 不能同时设置。配置中的相对路径以配置文件目录为基准；环境变量中的相对路径以进程工作目录为基准。显式空字符串可禁用旧 Secret 的回退读取。

| TOML 项 | 环境变量 | 默认值 / 说明 |
| --- | --- | --- |
| `service.delivery_enabled` | `DELIVERY_ENABLED` | `false`；仅接受 true/false |
| `service.poll_interval_seconds` | `POLL_INTERVAL_SECONDS` | `60`，允许 30–120 秒 |
| `service.state_dir` | `STATE_DIR` | `data`；镜像中为 `/data` |
| `service.secrets_dir` | `SECRETS_DIR` | `/run/notifier-secrets` |
| `telegram.channel` | `TELEGRAM_CHANNEL` | `codexreset` |
| `telegram.proxy_url` / `proxy_url_file` | `TELEGRAM_PROXY_URL` / `TELEGRAM_PROXY_URL_FILE` | 可选 HTTP CONNECT 代理 |
| `telegram.relay_url` | `TELEGRAM_RELAY_URL` | 可选固定频道 HTTPS relay |
| `telegram.relay_authorization` / `relay_authorization_file` | `TELEGRAM_RELAY_AUTHORIZATION` / `TELEGRAM_RELAY_AUTHORIZATION_FILE` | `Basic …`，不放 URL 查询参数 |
| `wecom.webhook_url` / `webhook_url_file` | `WECOM_WEBHOOK_URL` / `WECOM_WEBHOOK_URL_FILE` | 必须是企业微信机器人 HTTPS 地址 |

`--config FILE` 也可用 `NOTIFIER_CONFIG=FILE` 指定。兼容旧 Secret 文件名：`wecom-webhook-url`、`relay-authorization`、`telegram-proxy-url`。`--check-config` 会拒绝未知字段、错误类型及明显无效地址；不会打印 Webhook、授权头或代理密码。

## Docker Compose

先完成本地 `config.toml` 配置，再执行：

```sh
chmod 640 config.toml
export CONFIG_GID="$(id -g)"
docker compose build
docker compose run --rm notifier --check-config
docker compose run --rm notifier --dry-run
docker compose up -d
docker compose logs --tail=30 notifier
```

Compose 只读挂载 `config.toml`，用命名卷 `notifier-data` 保存去重状态。`CONFIG_GID` 将宿主机文件所属组加入非 root 容器，使 `640` 配置可读；配置文件应属于当前用户的主组。可以把这个非敏感变量保存到被忽略的 `.env`，用于后续 Compose 重建。修改配置后执行 `docker compose restart notifier`。停止服务用 `docker compose down`，不要加 `-v`，否则会删除状态卷。

如果采用独立 Secret 文件，需要在 Compose 中另外增加只读挂载，例如 `./secrets:/run/notifier-secrets:ro`，并在配置中使用 `/run/notifier-secrets/wecom-webhook-url`。容器内的 `127.0.0.1` 不是宿主机；代理地址应使用容器可达的地址。企业微信请求始终直连，不继承来源代理或全局代理变量。

## Kubernetes / ArgoCD / HTTPS relay

- [Kubernetes 部署说明](docs/kubernetes.md)：外部 Secret、PVC、探针、资源限制及 NetworkPolicy。
- [固定频道 HTTPS relay](docs/relay.md)：在可访问 Telegram 的主机上添加带鉴权的固定入口。
- [架构与故障行为](docs/design.md)：去重、重试、首次基线和回退边界。

这些是通用示例，镜像地址、GitHub 仓库和存储类需要按自己的环境配置。仓库不包含任何生产 Secret、私钥、公司域名、集群地址或私有镜像。

## 验证与贡献

```sh
make test
make check-config
make check-public
```

测试全部离线，不调用真实机器人或 Telegram。GitHub Actions 覆盖 Python 3.12/3.13/3.14、离线配置检查、基础敏感文件扫描和容器构建。尚未提供公共预构建镜像，请自行构建。

提交前检查 `git status --short` 和 `git diff --cached`，确认没有真实配置、状态库或凭据。`scripts/check_public.py` 只是基础防误提交检查，不能替代人工复核或完整 Secret 扫描。安全问题见 [SECURITY.md](SECURITY.md)。

## 已知边界

- 依赖 Telegram 公共页面结构；页面变化、频道不可访问或网络故障会导致抓取失败。
- 每轮最多扫描 10 页。无法覆盖游标或编辑窗口时明确报错，不默默跳过；超过 48 小时的历史消息或编辑不再通知。
- Webhook 不提供幂等键，成功响应丢失后的重试可能造成重复消息。
- readiness 检查最近抓取与投递错误；Docker 的 unhealthy 状态本身不会自动重启容器，需要监控告警。Kubernetes liveness 只检测主循环活性。
- SQLite 只支持本项目的单进程部署，历史记录目前不会自动清理。保留持久卷并观察磁盘使用量。

许可证：[MIT](LICENSE)。
