#!/usr/bin/env python3
"""Watch public operations for 熬鹰资本 and notify on each new record."""

import argparse
import getpass
import hashlib
import json
import logging
import os
import re
import smtplib
import socket
import sqlite3
import ssl
import time
from contextlib import closing
from email.message import EmailMessage
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from binance_watch import BinanceError, DAY_MS, DEFAULT_PORTFOLIO, fetch_records, portfolio_id
from trade_report import beijing_label, order_timestamp, trade_action


PUSHPLUS_URL = "https://www.pushplus.plus/send"
SERVERCHAN_URL = "https://sctapi.ftqq.com/"
PROFILE = "https://www.binance.com/zh-CN/copy-trading/lead-details/"
OVERLAP_MS = 30 * 60_000
BATCH_SIZE = 12


class DeliveryError(Exception):
    pass


def now_ms():
    return time.time_ns() // 1_000_000


def connection_problem(error):
    if isinstance(error, URLError):
        error = error.reason
    if isinstance(error, ssl.SSLCertVerificationError):
        return "HTTPS 证书校验失败"
    if isinstance(error, ssl.SSLError):
        return "HTTPS 握手失败"
    if isinstance(error, socket.gaierror):
        return "域名解析（DNS）失败"
    if isinstance(error, TimeoutError):
        return "连接或读取响应超时"
    if isinstance(error, (ConnectionError, OSError)):
        code = getattr(error, "winerror", None) or getattr(error, "errno", None)
        return f"网络连接失败（系统错误 {code}）" if isinstance(code, int) else "网络连接失败"
    return "网络或代理连接失败"


def response_code(result):
    value = str(result.get("code")) if isinstance(result, dict) else ""
    return value if value.isascii() and value.isdecimal() else "未知"


def request_delivery(request, provider, success_code):
    try:
        with urlopen(request, timeout=15) as response:
            result = json.load(response)
    except HTTPError as error:
        status = error.code
        try:
            code = response_code(json.loads(error.read(4096)))
        except (OSError, HTTPException, ValueError):
            code = "未知"
        finally:
            error.close()
        detail = "，SendKey 无效，请在 Server 酱 Turbo 重新复制" if provider == "Server酱" and code == "40001" else ""
        raise DeliveryError(f"{provider}返回 HTTP {status}（业务码 {code}）{detail}") from None
    except (OSError, HTTPException) as error:
        raise DeliveryError(f"{provider}：{connection_problem(error)}") from None
    except ValueError:
        raise DeliveryError(f"{provider}返回了无法解析的响应，请检查是否被代理或网络登录页拦截") from None
    if not isinstance(result, dict) or str(result.get("code")) != success_code:
        raise DeliveryError(f"{provider}未接受微信推送请求（返回码 {response_code(result)}）")


def check_wechat_connection(pushplus):
    url = PUSHPLUS_URL.rsplit("/", 1)[0] + "/" if pushplus else SERVERCHAN_URL
    try:
        with urlopen(Request(url), timeout=15) as response:
            status = response.status
    except HTTPError as error:
        status = error.code
        error.close()
    except (OSError, HTTPException) as error:
        raise DeliveryError(f"{url}：{connection_problem(error)}") from None
    print(f"{url} 收到 HTTP {status} 响应。连接测试没有读取密钥或发送通知。")
    return 0 if status < 400 else 1


def send_wechat(token, title, message):
    body = json.dumps({
        "token": token, "channel": "wechat", "template": "txt",
        "title": title, "content": message,
    }, ensure_ascii=False).encode("utf-8")
    request = Request(PUSHPLUS_URL, data=body, headers={"Content-Type": "application/json"})
    request_delivery(request, "PushPlus", "200")


def send_serverchan(key, title, message):
    if not re.fullmatch(r"SCT[A-Za-z0-9_-]+", key):
        raise DeliveryError("请使用以 SCT 开头的 Server酱 Turbo SendKey")
    body = urlencode({"title": title, "desp": message}).encode("ascii")
    request = Request(f"{SERVERCHAN_URL}{key}.send", data=body)
    request_delivery(request, "Server酱", "0")


def send_qq(address, secret, title, message):
    email = EmailMessage()
    email["Subject"] = title
    email["From"] = address
    email["To"] = address
    email.set_content(message)
    try:
        with smtplib.SMTP_SSL("smtp.qq.com", 465, context=ssl.create_default_context(), timeout=15) as client:
            client.login(address, secret)
            client.send_message(email)
    except (OSError, smtplib.SMTPException, ValueError) as error:
        raise DeliveryError(f"QQ 邮箱发送失败（{type(error).__name__}）") from None


def set_up_sender(mail_only, mail_backup, pushplus=False):
    provider, env_key = ("PushPlus", "PUSHPLUS_TOKEN") if pushplus else ("Server酱 Turbo", "SERVERCHAN_SENDKEY")
    token = "" if mail_only else os.environ.get(env_key, "").strip()
    if not mail_only and not token:
        token = getpass.getpass(f"请在本机粘贴 {provider} 密钥（输入时不显示），然后按回车：").strip()
    if not mail_only and not token:
        raise DeliveryError(f"未填写 {provider} 密钥")
    address = secret = ""
    if mail_only or mail_backup:
        address = os.environ.get("QQ_EMAIL", "").strip() or input("你的 QQ 邮箱地址：").strip()
        if not address.lower().endswith("@qq.com") or any(char in address for char in "\r\n"):
            raise DeliveryError("请输入以 @qq.com 结尾的邮箱地址")
        secret = os.environ.get("QQ_SMTP_CODE", "").strip() or getpass.getpass("QQ 邮箱 SMTP 授权码（输入时不显示）：").strip()
        if not secret:
            raise DeliveryError("未填写 QQ 邮箱 SMTP 授权码")

    def send(title, message):
        if token:
            try:
                send = send_wechat if pushplus else send_serverchan
                send(token, title, message)
            except DeliveryError as error:
                if not address:
                    raise
                logging.warning("%s，改用 QQ 邮箱", error)
            else:
                return "微信服务已受理"
        send_qq(address, secret, title, message)
        return "QQ 邮箱已受理"

    return send


def prepare_state(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS monitor (
            portfolio TEXT PRIMARY KEY,
            first_scan INTEGER NOT NULL,
            latest_scan INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS operations (
            portfolio TEXT NOT NULL,
            fingerprint TEXT NOT NULL,
            updated INTEGER NOT NULL,
            trade TEXT NOT NULL,
            pending INTEGER NOT NULL,
            PRIMARY KEY (portfolio, fingerprint)
        );
        CREATE INDEX IF NOT EXISTS pending_operations
            ON operations (portfolio, pending, updated);
    """)


def snapshot(db, pid, end):
    state = db.execute("SELECT latest_scan FROM monitor WHERE portfolio = ?", (pid,)).fetchone()
    start = max(end - 7 * DAY_MS, state[0] - OVERLAP_MS) if state else end - DAY_MS
    if start >= end:
        raise BinanceError("本机时间早于上次扫描，请检查系统的日期时间", retry_after=60)
    if state and state[0] < end - 7 * DAY_MS:
        logging.warning("监控已中断超过七天，本轮补查最近七天")
    return read_window(pid, start, end)


def read_window(pid, start, end):
    rows = list(fetch_records(pid, 7, start=start, end=end, require_complete=True))
    # Busy contracts can hit the website's result cap. Split the time window to recover the earlier rows.
    if len(rows) < 100:
        return rows
    if end - start < 60_000:
        raise BinanceError("短时间成交过多，币安公开记录可能达到上限", retry_after=60)
    middle = (start + end) // 2
    return read_window(pid, start, middle) + read_window(pid, middle + 1, end)


def save_snapshot(db, pid, end, records):
    with db:
        db.execute("BEGIN IMMEDIATE")
        state = db.execute(
            "SELECT first_scan, latest_scan FROM monitor WHERE portfolio = ?", (pid,),
        ).fetchone()
        first_scan = state[0] if state else end
        for row in records:
            occurred = order_timestamp(row)
            if occurred is None:
                raise BinanceError("公开操作记录缺少有效时间，保留上次状态", retry_after=60)
            payload = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            fingerprint = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            db.execute(
                "INSERT OR IGNORE INTO operations VALUES (?, ?, ?, ?, ?)",
                (pid, fingerprint, occurred, payload, int(state is not None and occurred >= first_scan)),
            )
        db.execute("""
            INSERT INTO monitor VALUES (?, ?, ?)
            ON CONFLICT(portfolio) DO UPDATE SET latest_scan = MAX(latest_scan, excluded.latest_scan)
        """, (pid, first_scan, end))
    return state is None


def action_label(row):
    action = trade_action(row)
    if action == "未公开开平方向":
        side = {"BUY": "买入", "SELL": "卖出"}.get(row.get("side"), "方向未公开")
        return f"{side}（开平待确认）"
    return action


def message_text(rows, pid):
    chunks = []
    for row in rows:
        symbol = row.get("symbol") or "交易对未公开"
        price = row.get("avgPrice")
        qty = row.get("executedQty")
        chunks.append(
            f"{symbol}｜{action_label(row)}\n"
            f"成交均价 {price if price is not None else '未公开'}｜数量 {qty if qty is not None else '未公开'}\n"
            f"{beijing_label(order_timestamp(row))}（北京时间）"
        )
    return "\n\n".join(chunks) + "\n\n币安带单详情\n" + PROFILE + pid


def send_pending(db, pid, send):
    with db:
        # Keep batches exclusive so a second running copy cannot claim the same queued events.
        db.execute("BEGIN IMMEDIATE")
        pending = db.execute("""
            SELECT fingerprint, trade FROM operations
            WHERE portfolio = ? AND pending = 1 ORDER BY updated, fingerprint LIMIT ?
        """, (pid, BATCH_SIZE)).fetchall()
        if not pending:
            return
        rows = [json.loads(payload) for _, payload in pending]
        name = "熬鹰资本" if pid == DEFAULT_PORTFOLIO else "币安带单"
        first = rows[0]
        detail = (
            f"{first.get('symbol') or '新操作'} {action_label(first)}"
            if len(rows) == 1 else f"{len(rows)} 条新操作"
        )
        result = send(f"{name}｜{detail}", message_text(rows, pid))
        db.executemany(
            "UPDATE operations SET pending = 0 WHERE portfolio = ? AND fingerprint = ?",
            ((pid, fingerprint) for fingerprint, _ in pending),
        )
    logging.info("%s，共 %d 条操作", result, len(rows))


def main():
    parser = argparse.ArgumentParser(description="监控熬鹰资本的币安公开操作记录，推送到微信或 QQ 邮箱。")
    parser.add_argument("--portfolio", default=DEFAULT_PORTFOLIO, type=portfolio_id, help="可选的其他公开合约组合链接或 ID")
    parser.add_argument("--interval", type=int, default=60, help="检查间隔秒数，默认 60")
    parser.add_argument("--state", type=Path, default=Path(".binance_alert.sqlite3"), help="本机去重与待发消息数据库")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="只验证公开数据，不发消息")
    action.add_argument("--test", action="store_true", help="只发一条测试通知，不读取币安")
    action.add_argument("--diagnose", action="store_true", help="测试微信服务的 HTTPS 连接，无需密钥")
    channel = parser.add_mutually_exclusive_group()
    channel.add_argument("--qq", action="store_true", help="只用 QQ 邮箱")
    channel.add_argument("--qq-backup", action="store_true", help="微信推送请求失败时改用 QQ 邮箱")
    parser.add_argument("--pushplus", action="store_true", help="微信改用 PushPlus，默认使用 Server酱 Turbo")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval 须为正整数")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if args.diagnose:
            return check_wechat_connection(args.pushplus)
        if args.check:
            rows = list(fetch_records(args.portfolio, 7, require_complete=True))
            logging.info("组合 %s 的公开操作记录可读取。最近七天返回 %d 条。", args.portfolio, len(rows))
            if rows:
                print(message_text(rows[:1], args.portfolio))
            return 0

        send = set_up_sender(args.qq, args.qq_backup, args.pushplus)
        if args.test:
            print(send("币安带单提醒测试", "如果你看到了这条消息，提醒渠道已连通。"))
            return 0

        args.state.parent.mkdir(parents=True, exist_ok=True)
        logging.info("开始监控 %s，默认每轮结束后等待 %d 秒。按 Ctrl+C 停止。", args.portfolio, args.interval)
        with closing(sqlite3.connect(args.state, timeout=45)) as db:
            prepare_state(db)
            send_failures = 0
            next_delivery = 0
            while True:
                delay = args.interval
                try:
                    end = now_ms()
                    rows = snapshot(db, args.portfolio, end)
                    initialized = save_snapshot(db, args.portfolio, end, rows)
                    if initialized:
                        logging.info("首次扫描完成，已建立基准。后续新操作会推送。")
                except BinanceError as error:
                    delay = max(delay, error.retry_after or 0)
                    logging.warning("扫描暂未完成：%s", error)
                if time.monotonic() >= next_delivery:
                    try:
                        send_pending(db, args.portfolio, send)
                    except DeliveryError as error:
                        send_failures += 1
                        next_delivery = time.monotonic() + max(
                            args.interval, min(900, 60 * 2 ** min(send_failures - 1, 4)),
                        )
                        logging.warning("%s，已保留待发通知，稍后重试", error)
                    else:
                        send_failures = 0
                time.sleep(delay)
    except KeyboardInterrupt:
        return 0
    except (BinanceError, DeliveryError, sqlite3.Error, EOFError, OSError) as error:
        logging.error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
