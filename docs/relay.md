# 可选固定频道 HTTPS relay

只有运行环境不能直接访问 Telegram 时才需要。也可以使用支持 HTTP CONNECT 的来源代理；代理与 relay 不能同时启用。relay 固定读取 `codexreset`，不能把入口当作任意 URL 代理。

在能够访问 Telegram、已有有效 HTTPS 域名的 NGINX 主机上操作：

1. 审阅 `deploy/nginx/codex-reset-feed.conf`，将它 include 到自己的现有 HTTPS `server` 块。该文件只是 location 片段，不替换现有 server 或代理配置。按操作系统修改 CA bundle 路径。
2. 创建仅供 NGINX 读取的密码文件。例如安装发行版提供的 `htpasswd` 工具后执行 `htpasswd -cB /etc/nginx/auth/codex-reset.htpasswd codex-reset`，交互输入独立强密码。不要在已有文件上重复使用 `-c`，它会覆盖文件。设置 NGINX worker 可读、其他用户不可读的权限。
3. 备份原配置，执行 `nginx -t`，成功后才 reload。检查原站点和原代理入口无回归。
4. 在客户端将授权头保存为受保护的 `secrets/relay-authorization` 文件（内容是 `Basic ` 加上 `用户名:密码` 的 Base64）。这不是加密，必须按密码管理；不要提交文件、写入 URL、截图或日志。
5. 配置如下；企业微信 Webhook 仍仅在通知服务端使用，不传给 relay：

   ```toml
   [telegram]
   channel = "codexreset"
   relay_url = "https://relay.example.com/codex-reset-feed"
   relay_authorization_file = "secrets/relay-authorization"
   ```

入口只接受固定路径、GET/HEAD，以及空查询或数值 `before` 参数。上游固定 `https://t.me/s/codexreset`，开启 TLS 验证，并清除 Authorization、Cookie、Referer 和请求正文。外层 HTTPS 证书须由你的既有证书方案续期。

验收至少覆盖：未鉴权/错误密码返回 401；正确鉴权能够抓取；分页有效；任意 URL 参数被拒绝；POST 被拒绝；原入口正常。不要用 `curl -u 用户:密码` 或 `-H 'Authorization: …'` 把真实凭据放进命令参数；使用本地受保护配置文件或本项目的 `--dry-run`。

回退时先停通知服务，确认原 vhost 在备份后没有他人修改，移除新增 include；再次通过 `nginx -t` 后 reload。不要重启或删除其他代理进程。
