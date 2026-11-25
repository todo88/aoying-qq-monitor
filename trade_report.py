"""Build the Chinese trade-review report from public USDⓈ-M futures data."""

import gzip
import json
import logging
import math
import os
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BEIJING_TIME = timezone(timedelta(hours=8))
DAY_MS = 86_400_000
MARKET_URL = "https://www.binance.com/fapi/v1/klines"
TRADE_LABELS = {
    "BUY": "买入", "SELL": "卖出", "LONG": "多仓", "SHORT": "空仓",
    "BOTH": "单向持仓", "MARKET": "市价单", "LIMIT": "限价单",
    "STOP": "止损限价单", "STOP_MARKET": "止损市价单",
    "TAKE_PROFIT": "止盈限价单", "TAKE_PROFIT_MARKET": "止盈市价单",
    "TRAILING_STOP_MARKET": "追踪止损市价单",
}
POSITION_STATUS = {"All Closed": "已全部平仓", "Partially Closed": "部分平仓", "Open": "持仓中"}
POSITION_HEADERS = (
    "交易对", "持仓方向", "状态", "开仓时间（北京时间）", "全部平仓时间（北京时间）",
    "持仓时长", "开仓均价", "平仓均价", "已平仓数量", "平仓盈亏", "杠杆倍数",
)
ACTIONS = {
    ("LONG", "BUY"): "开多／加多", ("LONG", "SELL"): "减多／平多",
    ("SHORT", "SELL"): "开空／加空", ("SHORT", "BUY"): "减空／平空",
}


def timestamp(value):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def order_timestamp(row):
    return timestamp(row.get("orderUpdateTime")) or timestamp(row.get("orderTime"))


def beijing_label(value):
    return datetime.fromtimestamp(value / 1000, BEIJING_TIME).strftime("%Y-%m-%d %H:%M:%S") if value else ""


def duration_label(milliseconds):
    if milliseconds is None:
        return "未公开"
    seconds = milliseconds // 1000
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return (f"{days}天 " if days else "") + (f"{hours}小时 " if days or hours else "") + f"{minutes}分 {seconds}秒"


def trade_action(row):
    return ACTIONS.get((row.get("positionSide"), row.get("side")), "未公开开平方向")


def summarize_positions(raw_positions, start, end):
    positions = {}
    for row in raw_positions:
        opened = timestamp(row.get("opened"))
        closed = timestamp(row.get("closed"))
        updated = timestamp(row.get("updateTime"))
        if not any(start <= value <= end for value in (opened, closed, updated) if value):
            continue
        status = row.get("status")
        confirmed_close = closed if status == "All Closed" else None
        reference = confirmed_close
        in_progress = status in {"Partially Closed", "Open"} and not closed
        if in_progress:
            reference = end
        duration = reference - opened if opened and reference and reference >= opened else None
        direction = str(row.get("side") or "").upper()
        key = (row.get("positionId") or row.get("id"), row.get("index"), row.get("symbol"))
        positions[key] = {
            "symbol": row.get("symbol") or "未公开",
            "direction": TRADE_LABELS.get(direction, "未公开"),
            "status": POSITION_STATUS.get(status, "未公开"),
            "opened": opened,
            "closed": confirmed_close,
            "duration": duration,
            "duration_label": duration_label(duration) + ("（截至报告时间）" if in_progress and duration is not None else ""),
            "entry": row.get("avgCost"),
            "exit": row.get("avgClosePrice"),
            "volume": row.get("closedVolume"),
            "pnl": row.get("closingPnl"),
            "leverage": row.get("leverage"),
            "sort_time": confirmed_close or updated or opened or 0,
        }
    return sorted(positions.values(), key=lambda row: row["sort_time"], reverse=True)


def get_candles(symbol, interval, start, end):
    query = urlencode({
        "symbol": symbol, "interval": interval, "startTime": start, "endTime": end, "limit": 500,
    })
    request = Request(f"{MARKET_URL}?{query}", headers={"Accept": "application/json", "Accept-Encoding": "gzip"})
    with urlopen(request, timeout=12) as response:
        body = response.read()
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
    data = json.loads(body)
    if not isinstance(data, list):
        raise ValueError("币安没有返回 K 线数组")
    candles = []
    for row in data:
        opened = int(row[0])
        prices = [float(value) for value in row[1:5]]
        if len(prices) == 4 and all(math.isfinite(value) for value in prices):
            candles.append([opened, *prices])
    return sorted(candles)


def chart_data(records, days, start, end):
    if days <= 3:
        interval, label, step = "15m", "15 分钟", 900_000
    elif days <= 10:
        interval, label, step = "30m", "30 分钟", 1_800_000
    else:
        interval, label, step = "2h", "2 小时", 7_200_000
    groups = {}
    for row in records:
        symbol = row.get("symbol")
        value = order_timestamp(row)
        if not isinstance(symbol, str) or not re.fullmatch(r"[^\s/&?]{1,40}", symbol) or not value:
            continue
        groups.setdefault(symbol, {"trades": [], "candles": []})["trades"].append({
            "time": value, "side": row.get("side"), "action": trade_action(row),
            "price": row.get("avgPrice"), "quantity": row.get("executedQty"), "pnl": row.get("totalPnl"),
            "direction": TRADE_LABELS.get(row.get("positionSide"), "未公开"),
            "type": TRADE_LABELS.get(row.get("type"), "未公开"),
        })

    def load(symbol):
        try:
            return get_candles(symbol, interval, start // step * step, end)
        except (URLError, OSError, EOFError, ValueError, TypeError, IndexError) as error:
            logging.warning("%s 的 K 线暂不可用（%s）", symbol, error)
            return []

    if groups:
        with ThreadPoolExecutor(max_workers=4) as executor:
            for symbol, candles in zip(groups, executor.map(load, groups)):
                groups[symbol]["candles"] = candles
    return {"label": label, "step": step, "groups": groups}


def write_report(path, portfolio, days, start, end, records, positions, notices):
    chart = chart_data(records, days, start, end)
    name = "熬鹰资本" if portfolio == "5075281354358777856" else f"组合 {portfolio}"
    completed = [item for item in positions if item["closed"] and item["duration"] is not None]
    durations = [item["duration"] for item in completed]
    data = {
        "name": name, "portfolio": portfolio, "start": start, "end": end,
        "tradeCount": len(records), "closedCount": len(completed),
        "medianDuration": duration_label(int(median(durations))) if durations else "暂无",
        "positions": positions, "notices": notices, **chart,
    }
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    template = Path(__file__).with_name("report_template.html").read_text(encoding="utf-8")
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".report-", suffix=".html", delete=False,
    ) as stream:
        temp_path = Path(stream.name)
        stream.write(template.replace("__REPORT_DATA__", payload, 1))
    try:
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
