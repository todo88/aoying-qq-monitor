const WORKFLOW = "https://api.github.com/repos/todo88/aoying-qq-monitor/actions/workflows/binance-monitor.yml";
const MONITOR_TITLE = "熬鹰监控｜补查";
const MINUTE = 60_000;
const RECENT_SUCCESS_MS = 4 * MINUTE;
const ACTIVE_RUN_MS = 5 * MINUTE;
const ACTIVE_STATUSES = new Set(["queued", "in_progress", "requested", "waiting", "pending"]);

function isMonitorRun(run) {
  return run.head_branch === "main" &&
    (run.event === "schedule" || (run.event === "workflow_dispatch" && run.display_title === MONITOR_TITLE));
}

function recent(timestamp, now, limit) {
  const age = now - Date.parse(timestamp);
  return age >= 0 && age < limit;
}

export default {
  async scheduled(_event, env) {
    const token = env.GH_ACTIONS_TOKEN?.trim();
    if (!token) {
      throw new Error("缺少 Cloudflare Worker Secret GH_ACTIONS_TOKEN");
    }

    const headers = {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${token}`,
      "User-Agent": "aoying-qq-watchdog",
      "X-GitHub-Api-Version": "2022-11-28",
    };
    const listResponse = await fetch(`${WORKFLOW}/runs?branch=main&per_page=15`, {
      headers,
      signal: AbortSignal.timeout(15_000),
    });
    if (!listResponse.ok) {
      throw new Error(`检查 GitHub 运行记录失败（HTTP ${listResponse.status}）`);
    }
    const { workflow_runs: runs } = await listResponse.json();
    if (!Array.isArray(runs)) {
      throw new Error("GitHub 未返回有效的工作流列表");
    }

    const now = Date.now();
    const monitorRuns = runs.filter(isMonitorRun);
    if (monitorRuns.some(run =>
      run.conclusion === "success" && recent(run.updated_at, now, RECENT_SUCCESS_MS)
    )) {
      console.log("已有近期完成的监控，无需补查");
      return;
    }
    if (monitorRuns.some(run =>
      ACTIVE_STATUSES.has(run.status) && recent(run.run_started_at || run.created_at, now, ACTIVE_RUN_MS)
    )) {
      console.log("已有监控正在排队或运行，无需重复触发");
      return;
    }

    const dispatchResponse = await fetch(`${WORKFLOW}/dispatches`, {
      method: "POST",
      headers: { ...headers, "Content-Type": "application/json" },
      body: JSON.stringify({ ref: "main", inputs: { mode: "monitor" } }),
      signal: AbortSignal.timeout(15_000),
    });
    if (!dispatchResponse.ok) {
      throw new Error(`GitHub 补查触发失败（HTTP ${dispatchResponse.status}）`);
    }
    console.log("已触发 GitHub 补查，邮件结果以工作流日志为准");
  },
};
