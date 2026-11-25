#!/usr/bin/env python3
"""Archive the public Latest Records of a Binance Futures lead portfolio."""

import argparse
import csv
import gzip
import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile
from trade_report import (
    POSITION_HEADERS, TRADE_LABELS, order_timestamp, summarize_positions, write_report,
)


BASE_URL = "https://www.binance.com/bapi/futures/v1/friendly/future/copy-trade/lead-portfolio"
DEFAULT_PORTFOLIO = "5075281354358777856"
DAY_MS = 86_400_000
PAGE_SIZE = 10
MAX_PAGES = 20
FIELDS = (
    "symbol", "side", "positionSide", "type", "executedQty", "avgPrice",
    "totalPnl", "baseAsset", "quoteAsset",
)
EXCEL_HEADERS = (
    "下单时间（北京时间）", "更新时间（北京时间）", "交易对", "买卖方向", "持仓方向",
    "订单类型", "成交数量", "成交均价", "盈亏", "基础资产", "计价资产",
)
BEIJING_TIME = timezone(timedelta(hours=8))
EXCEL_EPOCH = datetime(1899, 12, 30, tzinfo=BEIJING_TIME)
RECORDS_QUERY = "SELECT record_json FROM records ORDER BY updated_at DESC, record_json"
SPREADSHEET_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
WORKBOOK_PARTS = {
    "[Content_Types].xml": """<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
        <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
        <Default Extension="xml" ContentType="application/xml"/>
        <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
        <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
        <Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
        <Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
    </Types>""",
    "_rels/.rels": f"""<Relationships xmlns="{PACKAGE_REL_NS}">
        <Relationship Id="rId1" Type="{OFFICE_REL_NS}/officeDocument" Target="xl/workbook.xml"/>
    </Relationships>""",
    "xl/workbook.xml": f"""<workbook xmlns="{SPREADSHEET_NS}" xmlns:r="{OFFICE_REL_NS}">
        <bookViews><workbookView/></bookViews>
        <sheets><sheet name="最新操作记录" sheetId="1" r:id="rId1"/>
            <sheet name="仓位与持仓时长" sheetId="2" r:id="rId3"/></sheets>
    </workbook>""",
    "xl/_rels/workbook.xml.rels": f"""<Relationships xmlns="{PACKAGE_REL_NS}">
        <Relationship Id="rId1" Type="{OFFICE_REL_NS}/worksheet" Target="worksheets/sheet1.xml"/>
        <Relationship Id="rId2" Type="{OFFICE_REL_NS}/styles" Target="styles.xml"/>
        <Relationship Id="rId3" Type="{OFFICE_REL_NS}/worksheet" Target="worksheets/sheet2.xml"/>
    </Relationships>""",
    "xl/styles.xml": f"""<styleSheet xmlns="{SPREADSHEET_NS}">
        <numFmts count="1"><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm:ss.000"/></numFmts>
        <fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>
        <fills count="2"><fill><patternFill patternType="none"/></fill>
            <fill><patternFill patternType="gray125"/></fill></fills>
        <borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
        <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
        <cellXfs count="2">
            <xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
            <xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
        </cellXfs>
        <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
    </styleSheet>""",
}


class BinanceError(Exception):
    def __init__(self, message, retry_after=None, code=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.code = code


def portfolio_id(value):
    value = value.strip()
    if re.fullmatch(r"[0-9]{1,30}", value):
        return value
    try:
        url = urlsplit(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("带单组合详情页链接格式无效") from error
    match = re.fullmatch(
        r"/(?:[a-zA-Z-]+/)?copy-trading/lead-details/([0-9]{1,30})/?",
        url.path,
    )
    if url.scheme != "https" or url.netloc not in {"www.binance.com", "binance.com"} or not match:
        raise argparse.ArgumentTypeError("请输入 binance.com 带单组合详情页链接或数字组合 ID")
    market = parse_qs(url.query).get("type", [""])[0].lower()
    if market == "spot":
        raise argparse.ArgumentTypeError("此版本支持公开合约带单组合，请输入合约详情页链接")
    return match[1]


def retry_after_seconds(value):
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    try:
        return max(0, parsedate_to_datetime(value).timestamp() - time.time())
    except (ValueError, TypeError, OverflowError):
        return 0


def request_data(path, payload=None):
    headers = {"Accept": "application/json", "Accept-Encoding": "gzip", "clienttype": "web", "lang": "en"}
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = Request(BASE_URL + path, data=body, headers=headers)
    try:
        with urlopen(request, timeout=20) as response:
            body = response.read()
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                body = gzip.decompress(body)
        envelope = json.loads(body, parse_float=str)
    except HTTPError as error:
        status = error.code
        delay = retry_after_seconds(error.headers.get("Retry-After"))
        error.close()
        if status == 429 or status in {408, 500, 502, 503, 504}:
            raise BinanceError(f"币安返回 HTTP {status}", retry_after=delay) from error
        raise BinanceError(f"币安返回 HTTP {status}，采集已停止") from error
    except (URLError, TimeoutError, OSError, EOFError) as error:
        raise BinanceError(f"连接币安失败（{error}）", retry_after=0) from error
    except ValueError as error:
        raise BinanceError("币安返回了无法读取的数据，请检查网页是否可访问") from error

    if not isinstance(envelope, dict):
        raise BinanceError("币安响应格式已变化")
    if envelope.get("success") is not True:
        code = str(envelope.get("code", "未知"))
        message = envelope.get("message") or "请检查组合链接、组合状态和记录可见性"
        delay = 5 if code == "11012005" else None
        raise BinanceError(f"币安返回 {code}（{message}）", retry_after=delay, code=code)
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise BinanceError("币安响应缺少数据")
    return data


def fetch_records(pid, days, *, start=None, end=None, require_complete=False):
    detail = request_data(f"/detail?portfolioId={pid}")
    if detail.get("portfolioType") != "PUBLIC" or str(detail.get("leadPortfolioId")) != pid:
        raise BinanceError("币安未确认该 ID 对应公开合约组合，采集已停止")

    if end is None:
        end = time.time_ns() // 1_000_000
    if start is None:
        start = end - days * DAY_MS
    payload = {
        "portfolioId": pid, "startTime": start,
        "endTime": end, "pageSize": PAGE_SIZE,
    }
    seen_cursors = set()
    count = 0
    for _ in range(MAX_PAGES):
        try:
            result = request_data("/order-history", payload)
        except BinanceError as error:
            if error.code != "11012005":
                raise
            time.sleep(error.retry_after)
            result = request_data("/order-history", payload)
        page = result.get("list")
        if not isinstance(page, list) or any(not isinstance(row, dict) for row in page):
            raise BinanceError("币安最新记录的格式已变化")
        yield from page
        count += len(page)
        cursor = result.get("indexValue")
        total = result.get("total")
        if not page or cursor in (None, "", -1, "-1", 0, "0"):
            return
        if type(total) is int and count >= total:
            return
        if not isinstance(cursor, (str, int)) or str(cursor) in seen_cursors:
            if require_complete:
                raise BinanceError("币安分页游标无效，稍后重新扫描", retry_after=60)
            logging.warning("分页游标无效或重复，本轮只保存已读取的记录")
            return
        seen_cursors.add(str(cursor))
        payload["indexValue"] = cursor
        time.sleep(1)
    if require_complete:
        raise BinanceError("本轮达到分页上限，稍后重新扫描", retry_after=60)
    logging.warning("达到本轮分页上限（%d 页），本轮只保存已读取的记录", MAX_PAGES)


def utc_time(value):
    if value in (None, ""):
        return ""
    return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def fetch_positions(pid, start, raw_positions):
    page_size = 100
    for page_number in range(1, MAX_PAGES + 1):
        payload = {
            "portfolioId": pid, "pageNumber": page_number, "pageSize": page_size, "sort": "CLOSING",
        }
        try:
            result = request_data("/position-history", payload)
        except BinanceError as error:
            if error.code != "11012005":
                raise
            time.sleep(error.retry_after)
            result = request_data("/position-history", payload)
        rows = result.get("list")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise BinanceError("币安仓位历史响应格式已变化")
        raw_positions.extend(rows)
        total = result.get("total")
        if not rows or (isinstance(total, int) and len(raw_positions) >= total):
            return
        closing_times = [int(row["closed"]) for row in rows if row.get("closed")]
        if closing_times and min(closing_times) < start:
            return
        time.sleep(1)
    raise BinanceError("已到仓位历史分页上限，报告只包含已读取的仓位")


def excel_time(value):
    if value in (None, ""):
        return ""
    local_time = datetime.fromtimestamp(int(value) / 1000, BEIJING_TIME)
    return (local_time - EXCEL_EPOCH).total_seconds() / 86400


def excel_row(number, values, date_columns, numeric_columns):
    cells = []
    for column, value in zip("ABCDEFGHIJK", values):
        if value in (None, ""):
            continue
        reference = f"{column}{number}"
        value = str(value)
        if number > 1 and column in date_columns:
            cells.append(f'<c r="{reference}" s="1"><v>{value}</v></c>')
        elif column in numeric_columns and re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?(?:[Ee][+-]?[0-9]+)?", value):
            cells.append(f'<c r="{reference}"><v>{value}</v></c>')
        else:
            cells.append(f'<c r="{reference}" t="inlineStr"><is><t>{escape(value)}</t></is></c>')
    return f'<row r="{number}">{"".join(cells)}</row>'.encode("utf-8")


def write_worksheet(book, filename, headers, rows, date_columns, numeric_columns, widths):
    with book.open(filename, "w") as sheet:
        columns = "".join(
            f'<col min="{index}" max="{index}" width="{width}" customWidth="1"/>'
            for index, width in enumerate(widths, start=1)
        )
        sheet.write(f"""<worksheet xmlns="{SPREADSHEET_NS}">
            <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2"
                activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>
            <cols>{columns}</cols><sheetData>""".encode("utf-8"))
        number = 1
        sheet.write(excel_row(number, headers, date_columns, numeric_columns))
        for number, values in enumerate(rows, start=2):
            sheet.write(excel_row(number, values, date_columns, numeric_columns))
        sheet.write(f'</sheetData><autoFilter ref="A1:K{number}"/></worksheet>'.encode("utf-8"))


def excel_trades(db):
    for (row_json,) in db.execute(RECORDS_QUERY):
        row = json.loads(row_json)
        yield (
            excel_time(row.get("orderTime")), excel_time(row.get("orderUpdateTime")),
            row.get("symbol"),
            *(TRADE_LABELS.get(row.get(field), "未公开") for field in ("side", "positionSide", "type")),
            *(row.get(field, "") for field in FIELDS[4:]),
        )


def export_excel(path, db, positions=()):
    with tempfile.NamedTemporaryFile(prefix=".records-", suffix=".xlsx", dir=path.parent, delete=False) as stream:
        temp_path = Path(stream.name)
    try:
        with ZipFile(temp_path, "w", compression=ZIP_DEFLATED) as book:
            for filename, xml in WORKBOOK_PARTS.items():
                book.writestr(filename, xml)
            write_worksheet(
                book, "xl/worksheets/sheet1.xml", EXCEL_HEADERS, excel_trades(db),
                "AB", "GHI", (25, 25, *([16] * 9)),
            )
            position_rows = (
                (row["symbol"], row["direction"], row["status"], excel_time(row["opened"]),
                 excel_time(row["closed"]), row["duration_label"], row["entry"], row["exit"],
                 row["volume"], row["pnl"], row["leverage"]) for row in positions
            )
            write_worksheet(
                book, "xl/worksheets/sheet2.xml", POSITION_HEADERS, position_rows,
                "DE", "GHIJK", (16, 12, 17, 25, 30, 34, 16, 16, 16, 16, 12),
            )
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def archive(directory, records, positions=()):
    directory.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(directory / "records.sqlite3", timeout=20)) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS records (
                record_json TEXT PRIMARY KEY,
                updated_at INTEGER NOT NULL
            )
        """)
        db.execute("BEGIN IMMEDIATE")
        before = db.total_changes
        db.executemany(
            "INSERT OR IGNORE INTO records VALUES (?, ?)",
            (
                (
                    json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":")),
                    int(row.get("orderUpdateTime") or row["orderTime"]),
                )
                for row in records
            ),
        )
        added = db.total_changes - before
        db.commit()

        # Hold the write lock while exporting so concurrent runs publish in snapshot order.
        db.execute("BEGIN IMMEDIATE")
        excel_path = directory / "records.xlsx"
        try:
            export_excel(excel_path, db, positions)
        except PermissionError:
            logging.warning("暂时无法更新 Excel。若表格已打开，请关闭它，脚本会在下一轮再导出。")
        else:
            logging.info("Excel 已更新：%s", excel_path.resolve())

        csv_path = directory / "records.csv"
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8-sig", newline="",
                prefix=".records-", suffix=".csv", dir=directory, delete=False,
            ) as stream:
                temp_path = Path(stream.name)
                writer = csv.writer(stream)
                writer.writerow(("orderTimeUtc", "orderUpdateTimeUtc", *FIELDS))
                for (row_json,) in db.execute(RECORDS_QUERY):
                    row = json.loads(row_json)
                    writer.writerow((
                        utc_time(row.get("orderTime")), utc_time(row.get("orderUpdateTime")),
                        *(row.get(field, "") for field in FIELDS),
                    ))
            os.replace(temp_path, csv_path)
        except PermissionError:
            logging.warning("暂时无法更新 CSV：%s", csv_path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    return added


def recent_archived_records(directory, start, end):
    with closing(sqlite3.connect(directory / "records.sqlite3", timeout=20)) as db:
        records = (json.loads(row[0]) for row in db.execute(
            "SELECT record_json FROM records WHERE updated_at BETWEEN ? AND ? ORDER BY updated_at DESC, record_json",
            (start, end),
        ))
        return [record for record in records if order_timestamp(record) is not None]


def main():
    parser = argparse.ArgumentParser(description="自动保存币安公开合约带单组合的「最新记录」。")
    parser.add_argument(
        "portfolio", nargs="?", default=DEFAULT_PORTFOLIO, type=portfolio_id,
        help="合约带单详情页链接或数字组合 ID，默认熬鹰资本",
    )
    parser.add_argument("--once", action="store_true", help="只抓取一次")
    parser.add_argument("--interval", type=int, default=120, help="每轮间隔秒数，默认 120")
    parser.add_argument("--days", type=int, default=7, help="查询最近几天，默认 7，范围 1–30")
    parser.add_argument("--output", type=Path, default=Path("binance_records"), help="输出根目录")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval 须为正整数")
    if not 1 <= args.days <= 30:
        parser.error("--days 须在 1–30 之间")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    directory = args.output / args.portfolio
    logging.info("正在读取带单组合 %s 最近 %d 天的公开记录…", args.portfolio, args.days)
    consecutive_failures = 0
    try:
        while True:
            rows = []
            fetch_error = None
            try:
                rows.extend(fetch_records(args.portfolio, args.days))
            except BinanceError as error:
                fetch_error = error

            if fetch_error is None or rows:
                end = time.time_ns() // 1_000_000
                start = end - args.days * DAY_MS
                raw_positions = []
                notices = []
                if fetch_error is None:
                    try:
                        fetch_positions(args.portfolio, start, raw_positions)
                    except (BinanceError, TypeError, ValueError) as error:
                        notices.append("仓位历史未能完整读取，持仓统计基于已取得的资料。")
                        logging.warning("仓位历史暂不可完整读取（%s）", error)
                positions = summarize_positions(raw_positions, start, end)
                try:
                    added = archive(directory, rows, positions)
                except (OSError, sqlite3.Error, KeyError, TypeError, ValueError) as error:
                    logging.error("无法保存记录（%s）", error)
                    return 1
                if fetch_error is None:
                    try:
                        records = recent_archived_records(directory, start, end)
                        report = directory / "report.html"
                        write_report(report, args.portfolio, args.days, start, end, records, positions, notices)
                    except (OSError, sqlite3.Error, TypeError, ValueError) as error:
                        logging.error("Excel 已处理，中文报告生成失败（%s）", error)
                        if args.once:
                            return 1
                    else:
                        logging.info("中文报告已更新：%s", report.resolve())
                logging.info(
                    "本轮收到 %d 条，新增 %d 条。",
                    len(rows), added,
                )

            if fetch_error is not None:
                error = fetch_error
                if args.once or error.retry_after is None:
                    logging.error("%s", error)
                    return 1
                consecutive_failures += 1
                delay = max(args.interval, min(900, 30 * 2 ** min(consecutive_failures, 5)), error.retry_after)
                logging.warning("%s，%g 秒后重试", error, delay)
                time.sleep(delay)
                continue

            consecutive_failures = 0
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
