# 企业微信、钉钉与飞书

支持这三种群机器人，可同时启用。每种渠道目前对应一个机器人；普通微信、个人微信群和微信公众号不在本项目范围内。

## 选择渠道

```toml
[service]
notification_channels = ["wecom", "dingtalk", "feishu"]
delivery_enabled = false
```

也可用 `NOTIFICATION_CHANNELS=wecom,dingtalk,feishu`。只需要飞书时设置 `["feishu"]`，无需企业微信 Webhook。不配置该项时默认 `["wecom"]`，原配置继续有效。全局 `delivery_enabled` 决定是否正式发送。

下面 URL 都是占位值；不要把真实配置加入 Git。明文值和 `_file` 只能二选一；使用文件时删除对应的明文行。未显式提供值/文件时，程序会尝试读取 `SECRETS_DIR` 下约定的文件。

| 渠道 | TOML 字段 | 环境变量 | 默认 Secret 文件名 |
| --- | --- | --- | --- |
| 企业微信 | `wecom.webhook_url` / `webhook_url_file` | `WECOM_WEBHOOK_URL` / `_FILE` | `wecom-webhook-url` |
| 钉钉 | `dingtalk.webhook_url` / `webhook_url_file` | `DINGTALK_WEBHOOK_URL` / `_FILE` | `dingtalk-webhook-url` |
| 钉钉加签 | `dingtalk.secret` / `secret_file` | `DINGTALK_SECRET` / `_FILE` | `dingtalk-secret` |
| 飞书 | `feishu.webhook_url` / `webhook_url_file` | `FEISHU_WEBHOOK_URL` / `_FILE` | `feishu-webhook-url` |
| 飞书加签 | `feishu.secret` / `secret_file` | `FEISHU_SECRET` / `_FILE` | `feishu-secret` |

表中 `_FILE` 表示完整变量名追加 `_FILE`，例如 `DINGTALK_SECRET_FILE`。显式指定的文件必须存在；不使用的渠道不要留下指向缺失文件的配置。所有配置在启动时读取，变更后需重启。

## 企业微信

```toml
[wecom]
webhook_url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_WEBHOOK_KEY"
```

保持原有 Markdown 格式，来源链接可点击。旧 `state.db` 继续使用，不需要迁移或清空。

## 钉钉

在目标群创建自定义机器人，取得 Webhook。若开启“加签”，同时配置机器人生成的 secret：

```toml
[dingtalk]
webhook_url = "https://oapi.dingtalk.com/robot/send?access_token=YOUR_ACCESS_TOKEN"
secret_file = "secrets/dingtalk-secret"
```

签名按每次发送时间重新生成，使用毫秒时间戳，通过 URL 的 `timestamp` 和 `sign` 参数提交。配置中的 Webhook 只保留 `access_token`，不要保存旧签名参数。

未开启加签时可以不设置 secret，但仍需按平台配置关键词或 IP 白名单；通知标题包含 `Codex`，可用作机器人关键词。发送 Markdown，不设置 @所有人。

平台说明：[自定义机器人安全设置](https://open.dingtalk.com/document/robots/customize-robot-security-settings)、[机器人接入](https://open.dingtalk.com/document/orgapp/custom-robot-access)。

## 飞书

在目标群添加自定义机器人，取得 Webhook。若开启签名校验，配置对应 secret：

```toml
[feishu]
webhook_url = "https://open.feishu.cn/open-apis/bot/v2/hook/YOUR_HOOK_ID"
secret_file = "secrets/feishu-secret"
```

飞书签名使用秒级时间戳，`timestamp` 和 `sign` 位于 JSON 请求体，算法与钉钉不同。通知使用文本消息，保留正文、时间和来源 URL，并中和正文中的 @ 标签。当前固定支持 `open.feishu.cn`，没有隐式将请求转到其他主机。

平台说明：[自定义机器人使用指南](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。

## 验证与运维

```sh
# 离线检查，不联网、不发送，也不输出凭据。
python3 notifier.py --config config.toml --check-config

# 以下两条命令会真实发送测试消息，请按需选择一种。
# 仅测试已选中的飞书。
python3 notifier.py --config config.toml --send-test FEISHU-001 --destination feishu
# 向所有选中渠道各发送一条。
python3 notifier.py --config config.toml --send-test ALL-001
```

`--send-test` 会尝试所有目标并输出每渠道结果，有任一失败则退出码为 1。API 接收成功仍需结合群内实际收件确认。测试消息不改数据库游标，也不会进入自动重试队列；重新执行会再次发送。

正式发送时，只有失败渠道进入自己的退避重试，成功渠道不会因此重复发送。各渠道数据库分别为 `state.db`、`state-dingtalk.db`、`state-feishu.db`；它们都位于 `state_dir`，需要一起持久保存。

新增渠道首次只建立基线；移除渠道保留数据库，再次启用后继续原游标。如果旧渠道落后超过分页上限，只有它报告 `catchup_limit_reached`，正常渠道继续处理；不要为清除错误直接删除全部数据库。确认可放弃缺口后，停进程并备份该渠道单独数据库再重新建立基线。

`health.json` 的 `destinations` 提供逐渠道投递计数，`source_errors` 提供逐渠道抓取错误，顶层计数为各渠道之和。同一条消息投递给三个渠道会计为三次投递。任一渠道持续异常会使 readiness 失败，其他渠道仍可正常发送。

每个目标仍有 48 小时有效窗口、最多 8 次尝试和指数退避。接口限流、机器人关键词/白名单配置及服务端时钟偏差都可能导致投递失败；请保持系统时间准确。已有成功响应丢失时仍可能重复，不能保证严格 exactly-once。
