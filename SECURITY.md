# Security

- Webhook URLs, DingTalk/Feishu signing secrets and relay authorization headers are credentials. Keep them in ignored local configuration or mounted Secret files. Never put them in a public ConfigMap, Dockerfile, build argument, Git history, issue, or CI log.
- The image copies only application source. Runtime configuration and SQLite state are external mounts. `.dockerignore` uses an allowlist.
- TLS verification is enabled for Telegram, relay, WeCom, DingTalk and Feishu. HTTP redirects are refused. Relay credentials are not sent to Telegram or notification providers. The optional HTTP CONNECT proxy is used only for the Telegram source.
- Channel posts are untrusted content. This service forwards text and links, never executes instructions, consumes reset credits, or verifies an individual account's quota. Notifications neutralize `<@all>` mentions.
- Run one process per state database and mount durable storage. These outgoing bot requests do not use a provider idempotency key: a lost success response followed by a retry may produce a duplicate.
- Rotate exposed Webhook/relay credentials at their provider, replace the runtime file or Secret, then restart this service. Updating a file alone does not reload the current process.

For a vulnerability, use GitHub private vulnerability reporting once enabled by the repository owner. Until then, contact the owner privately. Do not include credentials or live customer data in a public issue. `scripts/check_public.py` is a basic guard, not a complete secret scanner or a review of all Git history.
