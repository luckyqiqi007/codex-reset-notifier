# Kubernetes 与 ArgoCD

示例使用独立 namespace `codex-reset-notifier`、单副本 Recreate Deployment、1Gi PVC、非 root 容器和外部 Secret。不创建 Service 或 Ingress。

执行前确认 `kubectl config current-context`。以下命令会在你选定的集群创建资源，请先审阅渲染结果。

1. 构建并推送到自己的镜像仓库，替换 `YOUR_USER`：

   ```sh
   docker build -t ghcr.io/YOUR_USER/codex-reset-notifier:0.1.0 .
   docker push ghcr.io/YOUR_USER/codex-reset-notifier:0.1.0
   ```

   将 `deploy/kubernetes/deployment.yaml` 的镜像修改为自己的已发布版本。私有镜像需额外配置 `imagePullSecrets`。

2. 在本地 `secrets/wecom-webhook-url` 写入机器人 Webhook 原文。目录与文件分别使用 `700` 和 `600` 权限。然后从文件创建 Secret，避免真实值出现在命令参数或 YAML 中：

   ```sh
   kubectl apply -f deploy/kubernetes/namespace.yaml
   kubectl -n codex-reset-notifier create secret generic codex-reset-notifier-secrets \
     --from-file=wecom-webhook-url=secrets/wecom-webhook-url
   ```

   若使用 relay，在创建 Secret 时同时加 `--from-file=relay-authorization=secrets/relay-authorization`，并设置非敏感的 `telegram.relay_url`。不要把授权头写入 ConfigMap。

   仅使用钉钉/飞书时，将上述 `--from-file` 换成对应文件：`dingtalk-webhook-url`、`dingtalk-secret`（加签可选）、`feishu-webhook-url`、`feishu-secret`（加签可选）。可把多个渠道的键放在同一个 Secret 中。Webhook 与签名密钥都不能写入 ConfigMap。

3. 检查 `deploy/kubernetes/config.example.toml`，默认关闭发送，`notification_channels` 默认 `["wecom"]`。按需选择钉钉、飞书或多个渠道；程序自动从 Secret 挂载目录读取对应文件。PVC 使用集群默认存储类；若无默认值，需设置可用 `storageClassName`。注意存储费用和 PV 回收策略。

   NetworkPolicy 允许 `kube-system` 中标记为 `k8s-app=kube-dns` 的 DNS，以及公网 IPv4 TCP/443，拒绝入站。依赖 CNI 实际执行策略；若使用 NodeLocal DNS、自定义 DNS、内网代理或非 443 端口，需精确补充规则，不能直接删除隔离策略。

4. 渲染、校验、应用：

   ```sh
   kubectl kustomize deploy/kubernetes > /tmp/codex-reset-notifier.yaml
   kubectl apply --dry-run=server -f /tmp/codex-reset-notifier.yaml
   kubectl diff -f /tmp/codex-reset-notifier.yaml
   kubectl apply -f /tmp/codex-reset-notifier.yaml
   kubectl -n codex-reset-notifier get deployment,pod,pvc
   kubectl -n codex-reset-notifier logs deployment/codex-reset-notifier --tail=30
   ```

5. 如需真实群收件验收，以下命令明确发送一条测试消息：

   ```sh
   kubectl -n codex-reset-notifier exec deployment/codex-reset-notifier -- \
     python /app/notifier.py --send-test K8S-SETUP-001
   ```

   确认群内收到后，把配置中的 `delivery_enabled` 改为 `true` 并重新应用。Kustomize ConfigMap 内容哈希会触发新 Pod；SQLite 基线保留。Secret 轮换后还需显式重启本服务。

## ArgoCD

仓库发布到 GitHub 后，修改 `deploy/argocd/application.example.yaml` 的 `repoURL`、`targetRevision`、项目与集群目标，再创建 Application。默认手动同步，无自动 prune 和级联删除 finalizer。

首次接管已有资源前检查 diff；保留同名 PVC 和外部 Secret，不使用 force/prune。检查 Synced/Healthy 后，还要验证实际抓取、投递、持久卷和群内收件。Secret 不存在时 Pod 不会启动，ArgoCD 不负责创建真实凭据。

## 回退

停止发送可先将副本数改为 0，或将配置的发送开关改为 false 后部署。保留 PVC 与 Secret，避免清空去重状态。仅撤回 ArgoCD 管理时使用非级联删除 Application；不要删除 namespace 或 PVC。版本回退通过 Git revert 和同步完成。
