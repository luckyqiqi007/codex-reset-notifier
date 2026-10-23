# 多渠道通知实现计划

> For agentic workers: Use superpowers:executing-plans to implement the following tasks with tests and review.

**Goal:** 支持企业微信、钉钉、飞书，可同时投递。

**Architecture:** 保留 Telegram 采集与 SQLite Store。新增 destinations.py 统一适配消息、加签与应答；各目标使用独立状态库，企业微信沿用 state.db。所有目标仅通过配置选择，凭据仍位于环境变量、被忽略的配置或 Secret 文件。

**Tech Stack:** Python 3.12+ 标准库、unittest、SQLite、现有 Docker/Kustomize。

**Spec:** 本文定义此次范围；公开频道与通知的现有行为见 ../../design.md。

## 约束与复核重点

- 不改变现有企业微信配置、首次基线、重启去重和编辑通知契约。
- 钉钉与飞书分别按其签名算法生成请求；不记录 Webhook/secret 或原始响应正文。
- 只读测试不联网、不发送；`--send-test` 明确发送到当前选择的渠道，可限制一个目标。
- 单渠道失败只重试该渠道；新增渠道首次启动不补历史；关闭或移除渠道保留数据库。
- 机器人请求直连且验证 TLS，不携带 Telegram relay 授权，不接受重定向或任意 API 主机。
- 用户已明确不需要普通微信通知，仅支持企业微信、钉钉、飞书。
- 一个渠道一个目标，同类多个机器人实例不在本次范围。

## Task 1: 配置与平台适配

Files: configuration.py, destinations.py, tests/test_destinations.py, tests/test_configuration.py

- [x] 增加 `service.notification_channels` 和环境变量 `NOTIFICATION_CHANNELS`，默认 `["wecom"]`。
- [x] 新增 dingtalk/feishu 的 webhook、secret，均支持 `_file`。
- [x] 先写离线失败测试，覆盖固定签名向量、应答错误、URL 限制、凭据隔离及兼容配置，再实现适配器。

## Task 2: 独立持久投递

Files: notifier.py, tests/test_notifier.py, tests/test_multichannel.py

- [x] 企业微信继续使用 state.db，其余使用 state-{provider}.db。
- [x] 一次抓取，各渠道独立 observe/send/ack/retry；新增渠道初始化自己的基线。
- [x] 增加逐渠道健康计数；发送过程中刷新 heartbeat。
- [x] 测试部分成功、重启、增加/关闭渠道、旧队列继续发送、更正与首次基线。

## Task 3: 部署与交付

Files: config.example.toml, Dockerfile, .dockerignore, README.md, docs/, SECURITY.md, scripts/check_public.py

- [x] 更新配置、Docker 白名单、Secret 文件说明与平台接入文档。
- [x] 完成单测、容器离线测试、Kustomize/lint、敏感信息检查和独立复核。
- [x] 将已验证文件更新到本地项目；真实配置和生产集群不变更。
