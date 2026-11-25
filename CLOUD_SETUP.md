# 熬鹰资本 · 云端 QQ 邮箱提醒

GitHub Actions 每 5 分钟计划检查一次币安公开操作记录。GitHub 繁忙时可能延迟。测试通过后，电脑关机不影响云端任务。

1. 在仓库 [Actions Secrets](https://github.com/todo88/aoying-qq-monitor/settings/secrets/actions) 页面点击 **New repository secret**，为 `QQ_EMAIL` 填写完整 QQ 邮箱地址，再为 `QQ_SMTP_CODE` 填写 QQ 邮箱生成的 SMTP 授权码。两项分开保存。
2. 在仓库 [币安带单 QQ 邮箱提醒](https://github.com/todo88/aoying-qq-monitor/actions/workflows/binance-monitor.yml) 页面点击 **Run workflow**，保持分支为 `main`，再点击 **Run workflow** 确认。
3. 确认这次运行显示绿勾，并在 QQ 邮箱收到「币安带单云端提醒测试」。首次成功的运行会保存监控基准。之后出现的新操作由定时任务推送。
4. 确认云端测试成功后，可以停止本地的监控程序。

邮箱地址和 SMTP 授权码只填入 Secrets。去重和待发送记录保存在独立的 `monitor-state` 分支，该分支只包含币安公开记录和程序状态。

如果运行失败，在 Actions 点进失败的那一轮，展开「读取币安记录并处理邮件」查看报错。[GitHub 定时任务说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
