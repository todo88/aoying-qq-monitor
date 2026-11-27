# 用 Cloudflare 补触发 QQ 邮箱监控

这个 Worker 每 5 分钟检查现有 GitHub 工作流。最近 4 分钟内有成功检查，或最近 5 分钟有检查排队或运行时，会跳过本轮补查。否则触发 GitHub 的 `monitor` 模式。币安采集、邮件和去重继续由原来的工作流处理。

## 一次性连接

1. 用 `todo88` 登录 GitHub，打开[创建细粒度 GitHub 凭证](https://github.com/settings/personal-access-tokens/new)。
   - **Token name** 可填 `aoying-cloudflare`，到期时间按需要选择。
   - **Resource owner** 选择 `todo88`。
   - **Repository access → Only select repositories** 只勾选 `aoying-qq-monitor`。
   - **Repository permissions → Actions** 选择 **Read and write**。GitHub 自动添加的 **Metadata: Read** 保留即可。
   - 点 **Generate token** 并复制凭证。凭证只粘贴到 Cloudflare 的 Secret 中。
2. 打开 [Cloudflare 控制台](https://dash.cloudflare.com/)，进入 **Workers & Pages → Create application → Import a repository**。选择 GitHub，授权时只选 `todo88/aoying-qq-monitor`。
   - 分支选择 `main`，**Root directory** 填 `cloudflare-watchdog`。
   - **Deploy command** 保持 `npx wrangler deploy`，部署到 Workers 免费方案。
   - Worker 名称是 `aoying-qq-watchdog`。部署配置会建立每 5 分钟一次的定时触发器。
3. 部署完成后，在该 Worker 的 **Settings → Variables and Secrets → Add** 添加 **Secret**。名字填 `GH_ACTIONS_TOKEN`，值粘贴第一步的 GitHub 凭证。原来的 QQ 邮箱信息继续留在 GitHub。
4. 在 Worker 的 **Triggers** 或 **Settings → Triggers** 确认有 `*/5 * * * *`。定时触发器的变更可能需要最多 15 分钟传播。之后检查 Worker 的 **Observability → Logs**，再到 [GitHub Actions](https://github.com/todo88/aoying-qq-monitor/actions/workflows/binance-monitor.yml) 找「熬鹰监控｜补查」的绿勾。只有发现 GitHub 漏跑时，才会出现补查运行。

Worker 显示「已有近期完成的监控」时，可在 GitHub Actions 核对「熬鹰监控｜GitHub 定时」；显示「已触发 GitHub 补查」时，以对应 GitHub 运行的绿勾为准。试运行期间可保留本地黑窗口。

## 每周检查

打开 [GitHub Actions](https://github.com/todo88/aoying-qq-monitor/actions/workflows/binance-monitor.yml)，看是否有近期的自动检查或补查。需要即时补查时，点 **Run workflow**，把运行方式选为 `monitor`。GitHub 凭证到期后，重新生成上述权限的凭证，并在 Cloudflare 更新 `GH_ACTIONS_TOKEN`。GitHub 和 QQ 邮箱服务状态也会影响提醒是否及时。

[Cloudflare Git 部署配置](https://developers.cloudflare.com/workers/ci-cd/builds/configuration/) · [Cloudflare 定时器](https://developers.cloudflare.com/workers/configuration/cron-triggers/) · [GitHub 触发权限](https://docs.github.com/en/rest/actions/workflows#create-a-workflow-dispatch-event)
