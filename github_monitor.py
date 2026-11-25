"""Run one polling cycle and save notification state to a dedicated branch."""

import argparse
import logging
import os
import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

from binance_alert import (
    DEFAULT_PORTFOLIO, DAY_MS, now_ms, prepare_state, save_snapshot, send_pending,
    set_up_sender, snapshot,
)


STATE_BRANCH = "monitor-state"
STATE_FILENAME = "monitor.sqlite3"
CHECKPOINT_MS = 60 * 60_000
RETENTION_MS = 14 * DAY_MS


def git(directory, *args, **options):
    return subprocess.run(("git", "-C", str(directory), *args), check=True, **options)


def prepare_worktree(repository, worktree):
    branch_ref = f"refs/heads/{STATE_BRANCH}"
    probe = subprocess.run(
        ("git", "-C", str(repository), "ls-remote", "--exit-code", "origin", branch_ref),
        stdout=subprocess.DEVNULL,
    )
    if probe.returncode == 0:
        git(repository, "fetch", "--no-tags", "origin", branch_ref)
        git(repository, "worktree", "add", "--detach", str(worktree), "FETCH_HEAD")
    elif probe.returncode == 2:
        git(repository, "worktree", "add", "--detach", str(worktree), "HEAD")
        git(worktree, "switch", "--orphan", STATE_BRANCH)
    else:
        probe.check_returncode()


def persist_state(repository, worktree):
    git(worktree, "add", "--", STATE_FILENAME)
    diff = subprocess.run(("git", "-C", str(worktree), "diff", "--cached", "--quiet"))
    if diff.returncode == 0:
        return
    if diff.returncode != 1:
        diff.check_returncode()
    git(
        worktree, "-c", "user.name=github-actions[bot]",
        "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-m", "Update Binance monitor state",
    )
    revision = git(worktree, "rev-parse", "HEAD", capture_output=True, text=True).stdout.strip()
    for attempt in range(3):
        try:
            # The checkout action scopes its Git credentials to the main checkout.
            git(repository, "push", "origin", f"{revision}:refs/heads/{STATE_BRANCH}")
            return
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(3)


def run_cycle(db, pid, mode, send, persist):
    prepare_state(db)
    state = db.execute("SELECT latest_scan FROM monitor WHERE portfolio = ?", (pid,)).fetchone()
    if mode == "monitor" and state is None:
        logging.info("等待首次手动测试。测试通过之前，定时任务保持待命。")
        return

    end = now_ms()
    records = snapshot(db, pid, end)
    if mode == "test":
        logging.info("币安组合 %s 读取成功，本轮取得 %d 条公开记录。", pid, len(records))
        print(send("币安带单云端提醒测试", "你已收到 GitHub Actions 云端测试邮件。后续提醒将由云端任务发出。"))
        if state is None:
            save_snapshot(db, pid, end, records)
            persist()
            logging.info("云端监控基准已保存，下次定时运行起检查新记录。")
        return

    before = db.total_changes
    save_snapshot(db, pid, end, records)
    changed = db.total_changes > before + 1
    if changed or end - state[0] >= CHECKPOINT_MS:
        with db:
            db.execute(
                "DELETE FROM operations WHERE portfolio = ? AND pending = 0 AND updated < ?",
                (pid, end - RETENTION_MS),
            )
        # Publish pending records before any SMTP delivery.
        persist()

    for _ in range(5):
        before = db.total_changes
        send_pending(db, pid, send)
        if db.total_changes == before:
            break
        persist()
    logging.info("监控检查完成。")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("test", "monitor"), required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if not all(os.environ.get(key, "").strip() for key in ("QQ_EMAIL", "QQ_SMTP_CODE")):
        if args.mode == "test":
            parser.error("先在 GitHub Actions Secrets 中设置 QQ_EMAIL 和 QQ_SMTP_CODE")
        logging.info("等待邮箱配置。")
        return
    email_sender = set_up_sender(True, False)
    repository = Path(__file__).resolve().parent
    prepare_worktree(repository, args.worktree)
    with closing(sqlite3.connect(args.worktree / STATE_FILENAME, timeout=45)) as db:
        run_cycle(
            db, DEFAULT_PORTFOLIO, args.mode, email_sender,
            lambda: persist_state(repository, args.worktree),
        )


if __name__ == "__main__":
    main()
