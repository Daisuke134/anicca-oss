"""Pinned, paper-only Alpaca CLI observation boundary."""

from __future__ import annotations

import json
import hashlib
import os
import re
import stat
import subprocess
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from risk_day import reconcile as reconcile_risk_day

from risk_policy import parse_instant


CLI_VERSION = "0.0.14"
PAPER_ENDPOINT = "https://paper-api.alpaca.markets/v2"
LIVE_ENDPOINT = "https://api.alpaca.markets/v2"
MAX_CREDENTIAL_BYTES = 1_048_576
MAX_OUTPUT_BYTES = 64 * 1024
CLI_OPERATIONS = frozenset({
    "account_activity", "account_get", "api_GET", "asset_get", "clock_get", "data_crypto",
    "data_latest-quotes", "data_latest-trade", "data_option", "order_list",
    "order_submit", "position_list",
})
SAFE_ERROR_CODES = frozenset({
    "alpaca_allocator_risk_invalid", "alpaca_allocator_shape_invalid",
    "alpaca_crypto_history_invalid",
    "alpaca_cli_json_invalid", "alpaca_cli_output_too_large", "alpaca_cli_unavailable",
    "alpaca_cli_version_unpinned", "alpaca_credential_record_invalid",
    "alpaca_live_credentials_unavailable", "alpaca_paper_credentials_unavailable",
    "alpaca_shadow_credentials_unavailable", "credential_document_invalid",
    "credential_file_too_large", "credential_path_invalid", "credential_permissions_invalid",
    "investment_mode_invalid",
})


def _selected_mode(mode: str | None = None) -> str:
    value = mode if mode is not None else os.environ.get("LIFE_MANAGER_INVESTMENT_MODE")
    if value not in {"paper", "shadow", "live"}:
        raise ValueError("investment_mode_invalid")
    return value


def _credentials(path: Path, mode: str | None = None) -> dict[str, str]:
    mode = _selected_mode(mode)
    parent, info = path.parent, path.lstat()
    if path.is_symlink() or parent.is_symlink():
        raise ValueError("credential_path_invalid")
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("credential_path_invalid")
    if stat.S_IMODE(info.st_mode) != 0o600 or stat.S_IMODE(parent.stat().st_mode) != 0o700:
        raise ValueError("credential_permissions_invalid")
    if info.st_size > MAX_CREDENTIAL_BYTES:
        raise ValueError("credential_file_too_large")
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("credential_document_invalid")
    rows = [row for row in document.get("credentials", [])
            if isinstance(row, dict) and row.get("service") == "app.alpaca.markets"]
    if len(rows) != 1:
        raise ValueError("alpaca_credential_record_invalid")
    row = rows[0]
    endpoint_key = "paper_endpoint" if mode == "paper" else "live_endpoint"
    endpoint = PAPER_ENDPOINT if mode == "paper" else LIVE_ENDPOINT
    if row.get(endpoint_key) != endpoint:
        raise ValueError(f"alpaca_{mode}_credentials_unavailable")
    fields = ("api_key", "api_secret") if mode == "paper" else ("live_api_key", "live_api_secret")
    values = {"api_key": row.get(fields[0]), "api_secret": row.get(fields[1])}
    if any(not isinstance(value, str) or not value or len(value) > 8192
           for value in values.values()):
        raise ValueError(f"alpaca_{mode}_credentials_unavailable")
    return values  # type: ignore[return-value]


def _context(credentials_path: Path, cli_path: Path, mode: str | None = None) -> dict[str, str]:
    mode = _selected_mode(mode)
    if not cli_path.is_file() or not os.access(cli_path, os.X_OK):
        raise ValueError("alpaca_cli_unavailable")
    private = _credentials(credentials_path, mode)
    env = {
        **os.environ,
        "ALPACA_API_KEY": private["api_key"],
        "ALPACA_SECRET_KEY": private["api_secret"],
        "ALPACA_LIVE_TRADE": "false" if mode == "paper" else "true",
    }
    version = subprocess.run(
        [str(cli_path), "version"], env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=10, check=False,
    )
    if version.returncode != 0 or version.stdout.strip() != CLI_VERSION:
        raise ValueError("alpaca_cli_version_unpinned")
    return env


def _run(cli: Path, args: list[str], env: dict[str, str]) -> Any:
    operation = "_".join(args[:2])
    if operation not in CLI_OPERATIONS:
        operation = "unknown"
    try:
        result = subprocess.run(
            [str(cli), *args], env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=30, check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError(f"alpaca_cli_timeout:{operation}") from error
    if result.returncode != 0:
        raise ValueError(f"alpaca_cli_failed:{operation}")
    if len(result.stdout) > MAX_OUTPUT_BYTES:
        raise ValueError("alpaca_cli_output_too_large")
    try:
        return json.loads(result.stdout.decode("utf-8").strip())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("alpaca_cli_json_invalid") from error


def observe(*, credentials_path: Path, cli_path: Path, symbol: str = "SPY") -> dict[str, Any]:
    if symbol != "SPY":
        raise ValueError("unsupported_observation_symbol")
    mode = _selected_mode()
    env = _context(credentials_path, cli_path, mode)

    account = _run(cli_path, [
        "account", "get", "--quiet", "--jq",
        "{status:.status,cash:.cash,equity:.equity,last_equity:.last_equity,options_level:.options_trading_level}",
    ], env)
    clock = _run(cli_path, [
        "clock", "get", "--quiet", "--jq",
        "{is_open:.is_open,observed_at:.timestamp,next_open:.next_open,next_close:.next_close}",
    ], env)
    positions = _run(cli_path, [
        "position", "list", "--quiet", "--jq",
        "[.[]|{symbol,qty,side,avg_entry_price,current_price,market_value,unrealized_pl}]",
    ], env)
    orders = _run(cli_path, [
        "order", "list", "--quiet", "--status", "all", "--limit", "500", "--jq", "length",
    ], env)
    activities = _run(cli_path, [
        "account", "activity", "list", "--quiet", "--jq", "length",
    ], env)
    trade = _run(cli_path, [
        "data", "latest-trade", "--symbol", symbol, "--quiet", "--jq",
        "{symbol:.symbol,price:.trade.p,timestamp:.trade.t}",
    ], env)
    options = _run(cli_path, [
        "data", "option", "chain", "--underlying-symbol", symbol, "--limit", "100", "--quiet", "--jq",
        "(.snapshots|length)",
    ], env)
    if not isinstance(account, dict) or not isinstance(clock, dict):
        raise ValueError("alpaca_cli_shape_invalid")
    if not isinstance(positions, list) or not isinstance(orders, int):
        raise ValueError("alpaca_cli_shape_invalid")
    if not isinstance(activities, int) or not isinstance(trade, dict) or not isinstance(options, int):
        raise ValueError("alpaca_cli_shape_invalid")
    return {
        "account": account,
        "activities_count": activities,
        "cli_version": CLI_VERSION,
        "clock": clock,
        "observed_symbol": symbol,
        "open_and_closed_orders_count": orders,
        "option_contracts_count": options,
        "mode": mode,
        "paper": mode == "paper",
        "positions": positions,
        "trade": trade,
    }


def find_order_by_client_id(
    *, credentials_path: Path, cli_path: Path, client_order_id: str,
) -> dict[str, Any] | None:
    if not re.fullmatch(r"lm-ai-[0-9a-f]{24}", client_order_id):
        raise ValueError("client_order_id_invalid")
    env = _context(credentials_path, cli_path)
    query = (
        f"first(.[]|select(.client_order_id=={json.dumps(client_order_id)})) // "
        "{found:false}|if .found==false then . else "
        "{found:true,client_order_id:.client_order_id,status:.status,"
        "filled_qty:.filled_qty,filled_avg_price:.filled_avg_price,submitted_at:.submitted_at} end"
    )
    result = _run(cli_path, [
        "order", "list", "--quiet", "--status", "all", "--limit", "500", "--jq", query,
    ], env)
    if result == {"found": False}:
        return None
    if not isinstance(result, dict) or result.get("found") is not True:
        raise ValueError("alpaca_order_readback_invalid")
    return result


def read_campaign_snapshot(
    *, credentials_path: Path, cli_path: Path, symbols: tuple[str, str],
) -> dict[str, Any]:
    mode = _selected_mode()
    env = _context(credentials_path, cli_path, mode)
    account = _run(cli_path, [
        "account", "get", "--quiet", "--jq",
        "{cash:.cash,equity:.equity,last_equity:.last_equity}",
    ], env)
    positions = _run(cli_path, [
        "position", "list", "--quiet", "--jq",
        "[.[]|{symbol,qty,side,avg_entry_price,current_price,market_value,unrealized_pl}]",
    ], env)
    fills = _run(cli_path, [
        "account", "activity", "list", "--activity-types", "FILL", "--page-size", "100",
        "--direction", "asc", "--quiet", "--jq",
        "[.[]|{order_id,symbol,side:(if .side==\"sell_short\" then \"sell\" else .side end),qty,price,transaction_time}]",
    ], env)
    clock = _run(cli_path, [
        "clock", "get", "--quiet", "--jq", "{is_open:.is_open,observed_at:.timestamp}",
    ], env)
    option_query = (
        ".snapshots|to_entries|map({symbol:.key,bid:.value.latestQuote.bp,"
        "ask:.value.latestQuote.ap,quote_at:.value.latestQuote.t})"
    )
    options = _run(cli_path, [
        "data", "option", "snapshot", "--symbols", ",".join(symbols),
        "--limit", "2", "--quiet", "--jq", option_query,
    ], env)
    if not isinstance(account, dict) or not isinstance(clock, dict):
        raise ValueError("alpaca_campaign_shape_invalid")
    if not isinstance(positions, list) or not isinstance(fills, list) or not isinstance(options, list):
        raise ValueError("alpaca_campaign_shape_invalid")
    return {
        "account": account,
        "clock": clock,
        "fills": [fill for fill in fills if fill.get("symbol") in symbols],
        "options": options,
        "mode": mode,
        "paper": mode == "paper",
        "positions": [position for position in positions if position.get("symbol") in symbols],
        "unexpected_positions": [position.get("symbol") for position in positions
                                 if position.get("symbol") not in symbols],
    }


def read_allocator_snapshot(
    *, credentials_path: Path, cli_path: Path, risk_day_path: Path,
) -> dict[str, Any]:
    """Read only the official fields needed to offer trade candidates."""
    env = _context(credentials_path, cli_path)
    account = _run(cli_path, [
        "account", "get", "--quiet", "--jq",
        "{cash:.cash,equity:.equity,last_equity:.last_equity}",
    ], env)
    clock = _run(cli_path, [
        "clock", "get", "--quiet", "--jq", "{is_open:.is_open,timestamp:.timestamp}",
    ], env)
    if not isinstance(account, dict) or not isinstance(clock, dict):
        raise ValueError("alpaca_allocator_shape_invalid")
    try:
        observed = parse_instant(clock["timestamp"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("alpaca_allocator_risk_invalid") from error
    ny_day = observed.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    ny_zone = ZoneInfo("America/New_York")
    day_start = datetime.combine(observed.astimezone(ny_zone).date(), time.min, ny_zone)
    day_end = day_start + timedelta(days=1)
    cash_activities = _run(cli_path, [
        "account", "activity", "list", "--activity-types", "CSD,CSW",
        "--after", day_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "--until", day_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "--direction", "asc", "--quiet", "--jq",
        "[.[]|{activity_type,date,net_amount}]",
    ], env)
    crypto_transfers = _run(cli_path, [
        "api", "GET", "/v2/wallets/transfers", "--quiet", "--jq",
        "[.[]|{id,asset,usd_value,direction,status}]",
    ], env)
    trade_activities = _run(cli_path, [
        "account", "activity", "list", "--activity-types", "FILL",
        "--after", day_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "--until", day_end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "--direction", "asc", "--quiet", "--jq", "[.[]|.id]",
    ], env)
    positions = _run(cli_path, ["position", "list", "--quiet", "--jq",
        "[.[]|{symbol,market_value,unrealized_pl}]"], env)
    orders = _run(cli_path, [
        "order", "list", "--quiet", "--status", "open", "--limit", "500", "--jq", "length",
    ], env)
    spy = _run(cli_path, [
        "data", "latest-trade", "--symbol", "SPY", "--quiet", "--jq",
        "{price:.trade.p,timestamp:.trade.t}",
    ], env)
    crypto = _run(cli_path, [
        "data", "crypto", "latest-quotes", "--symbols", "BTC/USDC,ETH/USDC", "--quiet", "--jq",
        ".quotes|to_entries|map({symbol:.key,bid:.value.bp,ask:.value.ap,quote_at:.value.t})",
    ], env)
    qqq_asset = _run(cli_path, [
        "asset", "get", "--symbol-or-asset-id", "QQQ", "--quiet", "--jq",
        "{symbol,tradable,status,overnight_tradable,overnight_halted,fractionable}",
    ], env)
    qqq_quote = _run(cli_path, [
        "data", "latest-quotes", "--symbols", "QQQ", "--quiet", "--jq",
        ".quotes.QQQ|{bid:.bp,ask:.ap,quote_at:.t}",
    ], env)
    price = float(spy["price"])
    option_query = (
        ".snapshots|to_entries|map({symbol:.key,bid:.value.latestQuote.bp,"
        "ask:.value.latestQuote.ap,quote_at:.value.latestQuote.t})"
    )
    options = _run(cli_path, [
        "data", "option", "chain", "--underlying-symbol", "SPY",
        "--expiration-date-gte", str(date.today() + timedelta(days=7)),
        "--expiration-date-lte", str(date.today() + timedelta(days=45)),
        "--strike-price-gte", f"{price * .97:.2f}",
        "--strike-price-lte", f"{price * 1.03:.2f}",
        "--type", "call", "--limit", "100", "--quiet", "--jq", option_query,
    ], env)
    if (not isinstance(positions, list) or isinstance(orders, bool)
            or not isinstance(orders, int) or orders < 0):
        raise ValueError("alpaca_allocator_shape_invalid")
    if (not isinstance(cash_activities, list) or not isinstance(crypto_transfers, list)
            or not isinstance(trade_activities, list)
            or not isinstance(crypto, list) or not isinstance(options, list)):
        raise ValueError("alpaca_allocator_shape_invalid")
    try:
        equity = Decimal(str(account["equity"]))
        funding_positions = [row for row in positions if row.get("symbol") == "USDCUSD"]
        risk_positions = [row for row in positions if row.get("symbol") != "USDCUSD"]
        allocated = sum((abs(Decimal(str(row["market_value"]))) for row in risk_positions), Decimal("0"))
        available_cash = Decimal(str(account["cash"])) + sum(
            (Decimal(str(row["market_value"])) for row in funding_positions), Decimal("0"))
        unrealized = sum((Decimal(str(row["unrealized_pl"])) for row in positions), Decimal("0"))
        if any(not isinstance(row, dict) or row.get("activity_type") not in {"CSD", "CSW"}
               for row in cash_activities):
            raise ValueError
        cash_flow = sum((Decimal(str(row["net_amount"])) for row in cash_activities), Decimal("0"))
        values = (equity, allocated, available_cash, unrealized, cash_flow)
        if any(not value.is_finite() for value in values):
            raise ValueError
        daily = reconcile_risk_day(risk_day_path, observed_at=observed, equity=equity,
                                   bank_cash_flow=cash_flow, transfers=crypto_transfers,
                                   trade_activity_ids=trade_activities,
                                   official_unrealized=unrealized)
        risk = {"allocated_capital_usd": str(allocated), **daily,
                "unrealized_pnl_usd": str(unrealized),
                "observed_at": clock["timestamp"],
                "ny_day": ny_day}
    except (KeyError, InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("alpaca_allocator_risk_invalid") from error
    return {"account": account, "available_cash_usd": str(available_cash),
            "clock": clock, "crypto": crypto,
            "open_orders": orders, "option_quotes": options, "positions": len(risk_positions), "risk": risk,
            "qqq_asset": qqq_asset, "qqq_quote": qqq_quote, "spy": spy}


def read_crypto_history(*, credentials_path: Path, cli_path: Path,
                        observed_at: str) -> dict[str, list[dict[str, Any]]]:
    """Read a bounded four-hour OHLC window for model judgment."""
    observed = parse_instant(observed_at)
    window_start = observed - timedelta(hours=4)
    start = window_start.isoformat().replace("+00:00", "Z")
    end = observed.isoformat().replace("+00:00", "Z")
    env = _context(credentials_path, cli_path)
    rows = _run(cli_path, ["data", "crypto", "bars", "--symbols",
        "BTC/USDC,ETH/USDC", "--start", start, "--end", end,
        "--timeframe", "5Min", "--limit", "1000", "--sort", "asc", "--quiet",
        "--jq", ".bars|to_entries|map({symbol:.key,bars:(.value|map({t,o,h,l,c}))})"], env)
    if not isinstance(rows, list):
        raise ValueError("alpaca_crypto_history_invalid")
    result: dict[str, list[dict[str, Any]]] = {}
    try:
        for row in rows:
            symbol, bars = row["symbol"], row["bars"]
            if symbol not in {"BTC/USDC", "ETH/USDC"} or symbol in result \
                    or not isinstance(bars, list) or len(bars) > 48:
                raise ValueError
            normalized = []
            previous_timestamp = None
            for bar in bars:
                timestamp = parse_instant(bar["t"])
                values = [Decimal(str(bar[key])) for key in ("o", "h", "l", "c")]
                if (timestamp < window_start or timestamp > observed
                        or (previous_timestamp is not None
                            and (timestamp <= previous_timestamp
                                 or (timestamp - previous_timestamp).total_seconds() % 300 != 0))
                        or any(not value.is_finite() or value <= 0 for value in values)
                        or values[1] < max(values[0], values[2], values[3])
                        or values[2] > min(values[0], values[1], values[3])):
                    raise ValueError
                normalized.append({"t": bar["t"], "o": str(values[0]), "h": str(values[1]),
                                   "l": str(values[2]), "c": str(values[3])})
                previous_timestamp = timestamp
            result[symbol] = normalized
        btc = result.get("BTC/USDC")
        if not btc or len(btc) < 6 or observed - parse_instant(btc[-1]["t"]) > timedelta(minutes=15):
            raise ValueError
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError("alpaca_crypto_history_invalid") from error
    return result


def read_live_performance_snapshot(
    *, credentials_path: Path, cli_path: Path, period_start: str,
    buy_client_order_id: str, sell_client_order_id: str,
) -> dict[str, Any]:
    """Rebuild the one completed live round trip from official broker records."""
    if (buy_client_order_id == sell_client_order_id
            or any(not re.fullmatch(r"lm-ai-[0-9a-f]{24}", value)
                   for value in (buy_client_order_id, sell_client_order_id))):
        raise ValueError("live_performance_order_ids_invalid")
    start = parse_instant(period_start)
    env = _context(credentials_path, cli_path, "live")
    clients = json.dumps([buy_client_order_id, sell_client_order_id])
    account = _run(cli_path, ["account", "get", "--quiet", "--jq", "{cash,equity}"], env)
    clock = _run(cli_path, ["clock", "get", "--quiet", "--jq", "{timestamp}"], env)
    positions = _run(cli_path, ["position", "list", "--quiet", "--jq",
        "[.[]|{symbol,qty,market_value,unrealized_pl,current_price}]"], env)
    transfers = _run(cli_path, ["api", "GET", "/v2/wallets/transfers", "--quiet", "--jq",
        "[.[]|{id,asset,amount,usd_value,direction,status,created_at}]"], env)
    orders = _run(cli_path, ["order", "list", "--quiet", "--status", "all", "--limit", "500",
        "--jq", f"[.[]|select(.client_order_id as $id|{clients}|index($id))|"
        "{id,client_order_id,status,symbol,side,filled_qty}]"], env)
    order_ids = json.dumps([row.get("id") for row in orders]) if isinstance(orders, list) else "[]"
    fills = _run(cli_path, ["account", "activity", "list", "--activity-types", "FILL",
        "--page-size", "100", "--direction", "asc", "--quiet", "--jq",
        f"[.[]|select(.order_id as $id|{order_ids}|index($id))|"
        "{id,activity_type,order_id,symbol,side,qty,price,transaction_time}]"], env)
    fees = _run(cli_path, ["account", "activity", "list", "--activity-types", "CFEE",
        "--page-size", "100", "--direction", "asc", "--quiet", "--jq",
        f"[.[]|select(.order_id as $id|{order_ids}|index($id))|"
        "{id,activity_type,order_id,symbol,qty,price,date}]"], env)

    def quotes(begin: datetime, end: datetime) -> list[dict[str, Any]]:
        value = _run(cli_path, ["data", "crypto", "quotes", "--symbols", "BTC/USDC",
            "--start", begin.isoformat().replace("+00:00", "Z"),
            "--end", end.isoformat().replace("+00:00", "Z"), "--limit", "1000",
            "--sort", "asc", "--quiet", "--jq",
            '.quotes["BTC/USDC"]|map({t,bp,ap})'], env)
        if not isinstance(value, list) or not value:
            raise ValueError("live_performance_quote_missing")
        return value

    try:
        if (not isinstance(account, dict) or not isinstance(clock, dict)
                or not isinstance(positions, list) or not isinstance(transfers, list)
                or not isinstance(orders, list) or not isinstance(fills, list)
                or not isinstance(fees, list) or len(orders) != 2 or len(fills) != 2
                or len(fees) != 2 or len(positions) != 1):
            raise ValueError
        by_client = {row["client_order_id"]: row for row in orders}
        buy_order, sell_order = by_client[buy_client_order_id], by_client[sell_client_order_id]
        if (buy_order.get("side") != "buy" or sell_order.get("side") != "sell"
                or any(row.get("status") != "filled" or row.get("symbol") not in
                       {"BTC/USDC", "BTCUSDC"} for row in orders)):
            raise ValueError
        by_order = {row["order_id"]: row for row in fills}
        buy_fill, sell_fill = by_order[buy_order["id"]], by_order[sell_order["id"]]
        expected_order_ids = {buy_order["id"], sell_order["id"]}
        if (set(by_order) != expected_order_ids or buy_fill.get("side") != "buy"
                or sell_fill.get("side") != "sell"
                or any(row.get("activity_type") != "FILL" for row in fills)
                or {row.get("order_id") for row in fees} != expected_order_ids
                or any(row.get("activity_type") != "CFEE" for row in fees)):
            raise ValueError
        complete = [row for row in transfers if row.get("asset") == "USDC"
                    and row.get("direction") == "INCOMING" and row.get("status") == "COMPLETE"]
        if len(complete) != 1 or positions[0].get("symbol") != "USDCUSD":
            raise ValueError
        transfer, position = complete[0], positions[0]
        observed = parse_instant(clock["timestamp"])
        if start > observed or parse_instant(transfer["created_at"]) > start:
            raise ValueError
        buy_time, sell_time = (parse_instant(row["transaction_time"])
                               for row in (buy_fill, sell_fill))
        if not start <= buy_time < sell_time <= observed:
            raise ValueError
        start_quotes = quotes(start, start + timedelta(seconds=30))
        fill_quotes = [quotes(moment - timedelta(seconds=2), moment + timedelta(seconds=2))
                       for moment in (buy_time, sell_time)]
        latest = _run(cli_path, ["data", "crypto", "latest-quotes", "--symbols", "BTC/USDC",
            "--quiet", "--jq", '.quotes["BTC/USDC"]|{t,bp,ap}'], env)
        latest_age = observed - parse_instant(latest["t"]) if isinstance(latest, dict) else None
        if latest_age is None or not timedelta(0) <= latest_age <= timedelta(minutes=15):
            raise ValueError

        def number(value: Any) -> Decimal:
            result = Decimal(str(value))
            if not result.is_finite():
                raise ValueError
            return result

        def nearest(rows: list[dict[str, Any]], moment: datetime) -> dict[str, Any]:
            row = min(rows, key=lambda item: abs((parse_instant(item["t"]) - moment).total_seconds()))
            bid, ask = number(row["bp"]), number(row["ap"])
            if (abs((parse_instant(row["t"]) - moment).total_seconds()) > 1
                    or bid <= 0 or ask < bid):
                raise ValueError
            return row

        buy_ref, sell_ref = nearest(fill_quotes[0], buy_time), nearest(fill_quotes[1], sell_time)
        buy_qty, sell_qty = number(buy_fill["qty"]), number(sell_fill["qty"])
        buy_price, sell_price = number(buy_fill["price"]), number(sell_fill["price"])
        if (buy_qty <= 0 or sell_qty <= 0 or buy_price <= 0 or sell_price <= 0
                or buy_qty != number(buy_order["filled_qty"])
                or sell_qty != number(sell_order["filled_qty"])
                or any(number(row["qty"]) >= 0 or number(row["price"]) <= 0 for row in fees)):
            raise ValueError
        slippage = max(Decimal("0"), buy_price - number(buy_ref["ap"])) * buy_qty
        slippage += max(Decimal("0"), number(sell_ref["bp"]) - sell_price) * sell_qty
        fee_total = sum((abs(number(row["qty"])) * number(row["price"]) for row in fees), Decimal("0"))
        transfer_amount, transfer_usd = number(transfer["amount"]), number(transfer["usd_value"])
        position_qty, position_value = number(position["qty"]), number(position["market_value"])
        realised_usdc = position_qty - transfer_amount
        realised_usd = realised_usdc * number(position["current_price"])
        unrealised = number(position["unrealized_pl"])
        ending_nav = number(account["cash"]) + position_value
        if (transfer_amount <= 0 or transfer_usd <= 0
                or abs(number(account["equity"]) - ending_nav) > Decimal("0.01")
                or abs((realised_usd + unrealised) - (ending_nav - transfer_usd)) > Decimal("0.01")):
            raise ValueError
        start_quote = start_quotes[0]
        start_quote_time = parse_instant(start_quote["t"])
        if not start <= start_quote_time <= start + timedelta(seconds=30):
            raise ValueError
        benchmark_start = (number(start_quote["bp"]) + number(start_quote["ap"])) / 2
        benchmark_end = (number(latest["bp"]) + number(latest["ap"])) / 2
        source_ids = [transfer["id"], buy_order["id"], sell_order["id"],
                      *(row["id"] for row in fills), *(row["id"] for row in fees),
                      f"BTC/USDC@{start_quote['t']}", f"BTC/USDC@{latest['t']}"]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError("live_performance_receipts_invalid") from error
    return {
        "benchmark_end_price_usd": str(benchmark_end),
        "benchmark_start_price_usd": str(benchmark_start),
        "completed_round_trips": 1,
        "ending_nav_usd": str(ending_nav),
        "fees_usd": str(fee_total),
        "gross_exposure_usd": str(buy_qty * buy_price),
        "observed_at": clock["timestamp"],
        "owner_cash_flow_usd": str(transfer_usd),
        "peak_adjusted_nav_usd": "0",
        "period_start": period_start,
        "realized_pnl_usd": str(realised_usd),
        "schema_version": 1,
        "slippage_usd": str(slippage),
        "source_receipt_ids": source_ids,
        "starting_nav_usd": "0",
        "unrealized_pnl_usd": str(unrealised),
    }


def submit_order(
    *, credentials_path: Path, cli_path: Path, client_order_id: str,
    order: dict[str, Any], mode: str | None = None,
) -> dict[str, Any]:
    """Submit one already-gated paper order or tightly bounded live crypto order."""
    if not re.fullmatch(r"lm-ai-[0-9a-f]{24}", client_order_id):
        raise ValueError("client_order_id_invalid")
    mode = _selected_mode(mode)
    if mode == "shadow":
        raise ValueError("investment_mode_effect_forbidden")
    if mode == "live":
        expected = {"asset_class", "side", "symbol", "time_in_force", "type"}
        if order.get("symbol") != "BTC/USDC" or order.get("asset_class") != "crypto" \
                or order.get("type") != "market" or order.get("time_in_force") != "gtc":
            raise ValueError("unsupported_live_order_shape")
        if order.get("side") == "buy":
            expected.add("notional_usd")
            try:
                amount = Decimal(str(order.get("notional_usd")))
                valid = amount.is_finite() and Decimal("0") < amount <= Decimal("10")
            except InvalidOperation:
                valid = False
            if set(order) != expected or not valid:
                raise ValueError("unsupported_live_order_shape")
            args = ["order", "submit", "--quiet", "--symbol", "BTC/USDC",
                    "--notional", str(order["notional_usd"]), "--side", "buy", "--type", "market",
                    "--time-in-force", "gtc", "--client-order-id", client_order_id]
        elif order.get("side") == "sell":
            expected.add("qty")
            try:
                qty = Decimal(str(order.get("qty")))
                valid = qty.is_finite() and qty > 0 and qty.as_tuple().exponent >= -9
            except InvalidOperation:
                valid = False
            if set(order) != expected or not valid:
                raise ValueError("unsupported_live_order_shape")
            args = ["order", "submit", "--quiet", "--symbol", "BTC/USDC", "--qty", str(order["qty"]),
                    "--side", "sell", "--type", "market", "--time-in-force", "gtc",
                    "--client-order-id", client_order_id]
        else:
            raise ValueError("unsupported_live_order_shape")
        env = _context(credentials_path, cli_path, mode)
        result = _run(cli_path, [*args, "--jq",
            "{client_order_id,status,submitted_at,symbol,notional,qty,side,type,time_in_force}"], env)
        if not isinstance(result, dict) or result.get("client_order_id") != client_order_id:
            raise ValueError("alpaca_submit_readback_invalid")
        return result
    env = _context(credentials_path, cli_path, mode)
    if order.get("asset_class") == "crypto" and order.get("symbol") in {"BTC/USD", "ETH/USD"}:
        args = ["order", "submit", "--quiet", "--symbol", order["symbol"],
                "--notional", order["notional_usd"], "--side", "buy", "--type", "market",
                "--time-in-force", "gtc", "--client-order-id", client_order_id]
    elif order.get("asset_class") in {"option_spread", "option_spread_close"}:
        closing = order["asset_class"] == "option_spread_close"
        legs = json.dumps([
            {"symbol": order["long_symbol"], "ratio_qty": "1",
             "position_intent": "sell_to_close" if closing else "buy_to_open"},
            {"symbol": order["short_symbol"], "ratio_qty": "1",
             "position_intent": "buy_to_close" if closing else "sell_to_open"},
        ], separators=(",", ":"))
        args = ["order", "submit", "--quiet", "--order-class", "mleg", "--qty", "1",
                "--type", "limit", "--limit-price", order["limit_price"],
                "--time-in-force", "day", "--legs", legs,
                "--client-order-id", client_order_id]
    else:
        raise ValueError("unsupported_order_shape")
    result = _run(cli_path, [*args, "--jq",
        "{client_order_id,status,submitted_at,symbol,notional}"], env)
    if not isinstance(result, dict) or result.get("client_order_id") != client_order_id:
        raise ValueError("alpaca_submit_readback_invalid")
    return result


def submit_live_canary(*, credentials_path: Path, cli_path: Path,
                       client_order_id: str, order: dict[str, Any]) -> dict[str, Any]:
    """Submit the single frozen L09 live canary; no scheduled loop calls this function."""
    expected = {"asset_class": "crypto", "notional_usd": "2.00", "side": "buy",
                "symbol": "BTC/USDC", "time_in_force": "gtc", "type": "market"}
    if order != expected or not re.fullmatch(r"lm-ai-[0-9a-f]{24}", client_order_id):
        raise ValueError("live_canary_shape_invalid")
    if _selected_mode() != "live":
        raise ValueError("investment_mode_effect_forbidden")
    env = _context(credentials_path, cli_path, "live")
    result = _run(cli_path, [
        "order", "submit", "--quiet", "--symbol", "BTC/USDC", "--notional", "2.00",
        "--side", "buy", "--type", "market", "--time-in-force", "gtc",
        "--client-order-id", client_order_id, "--jq",
        "{id,client_order_id,status,submitted_at,symbol,notional,side,type,time_in_force}",
    ], env)
    if (not isinstance(result, dict) or result.get("client_order_id") != client_order_id
            or result.get("symbol") not in {"BTC/USDC", "BTCUSDC"}
            or result.get("notional") != "2"
            or result.get("side") != "buy" or result.get("type") != "market"
            or result.get("time_in_force") != "gtc"):
        raise ValueError("live_canary_ack_invalid")
    return result


def read_live_canary(*, credentials_path: Path, cli_path: Path,
                     client_order_id: str) -> dict[str, Any]:
    """Verify the frozen canary from official order, FILL, and position records."""
    env = _context(credentials_path, cli_path, "live")
    query = (
        f"first(.[]|select(.client_order_id=={json.dumps(client_order_id)})) // "
        "{found:false}|if .found==false then . else "
        "{found:true,id,client_order_id,status,filled_qty,filled_avg_price,symbol,side,notional,type,time_in_force} end"
    )
    order = _run(cli_path, [
        "order", "list", "--quiet", "--status", "all", "--limit", "500", "--jq", query,
    ], env)
    if order == {"found": False}:
        return {"status": "absent", "verified": False}
    if not isinstance(order, dict) or order.get("found") is not True:
        raise ValueError("live_canary_order_invalid")
    if (order.get("client_order_id") != client_order_id
            or order.get("symbol") not in {"BTC/USDC", "BTCUSDC"}
            or order.get("side") != "buy" or order.get("notional") != "2"
            or order.get("type") != "market" or order.get("time_in_force") != "gtc"):
        raise ValueError("live_canary_order_mismatch")
    status = order.get("status")
    try:
        filled_qty = Decimal(str(order.get("filled_qty") or "0"))
    except InvalidOperation as error:
        raise ValueError("live_canary_order_invalid") from error
    if status in {"canceled", "expired", "rejected"} and filled_qty == 0:
        return {"status": "terminal_failure", "verified": False, "order": order}
    if status in {"canceled", "expired", "rejected"}:
        return {"status": "partial_terminal", "verified": False, "order": order}
    if status != "filled":
        return {"status": "pending", "verified": False, "order": order}
    fills = _run(cli_path, [
        "account", "activity", "list", "--activity-types", "FILL", "--order-id", str(order["id"]),
        "--page-size", "100",
        "--direction", "desc", "--quiet", "--jq",
        f"[.[]|select(.order_id=={json.dumps(order.get('id'))})|"
        "{order_id,symbol,side,qty,price,transaction_time}]",
    ], env)
    positions = _run(cli_path, [
        "position", "list", "--quiet", "--jq",
        "[.[]|select(.symbol==\"BTCUSD\" or .symbol==\"BTCUSDC\" or .symbol==\"BTC/USDC\")|"
        "{symbol,qty,market_value,unrealized_pl}]",
    ], env)
    try:
        if not isinstance(fills, list) or not fills or not isinstance(positions, list) \
                or len(positions) != 1:
            raise ValueError
        fill_total = sum((Decimal(str(fill["qty"])) for fill in fills), Decimal("0"))
        position_qty = Decimal(str(positions[0]["qty"]))
        if (any(fill.get("order_id") != order["id"] or fill.get("side") != "buy"
                or fill.get("symbol") not in {"BTC/USDC", "BTCUSDC"} for fill in fills)
                or fill_total != filled_qty
                or position_qty > filled_qty or position_qty < filled_qty * Decimal("0.99")
                or filled_qty <= 0):
            raise ValueError
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError("live_canary_fill_mismatch") from error
    return {"status": "verified", "verified": True, "order": order,
            "fills": fills, "position": positions[0]}


def read_live_close_snapshot(*, credentials_path: Path, cli_path: Path) -> dict[str, Any]:
    """Read the narrow official state needed to close the L09 BTC holding."""
    env = _context(credentials_path, cli_path, "live")
    account = _run(cli_path, ["account", "get", "--quiet", "--jq",
        "{status,trading_blocked,transfers_blocked,account_blocked,crypto_status}"], env)
    orders = _run(cli_path, ["order", "list", "--quiet", "--status", "open", "--limit", "500",
        "--jq", "[.[]|{id,client_order_id,status,symbol,side}]"], env)
    positions = _run(cli_path, ["position", "list", "--quiet", "--jq",
        "[.[]|{symbol,qty,market_value,unrealized_pl}]"], env)
    asset = _run(cli_path, ["asset", "get", "--symbol-or-asset-id", "BTC/USDC", "--quiet", "--jq",
        "{symbol,status,tradable}"], env)
    quote = _run(cli_path, ["data", "crypto", "latest-quotes", "--symbols", "BTC/USDC",
        "--quiet", "--jq", ".quotes[\"BTC/USDC\"]|{bp,ap,t}"], env)
    fees = _run(cli_path, ["account", "activity", "list", "--activity-types", "CFEE",
        "--direction", "asc", "--quiet", "--jq",
        "[.[]|{activity_type,order_id,symbol,qty,price,date}]"], env)
    bank_flows = _run(cli_path, ["account", "activity", "list", "--activity-types", "CSD,CSW",
        "--direction", "asc", "--quiet", "--jq",
        "[.[]|{activity_type,id,date,net_amount}]"], env)
    transfers = _run(cli_path, ["api", "GET", "/v2/wallets/transfers", "--quiet", "--jq",
        "[.[]|{id,asset,usd_value,direction,status}]"], env)
    if not isinstance(account, dict) or not isinstance(orders, list) \
            or not isinstance(positions, list) or not isinstance(asset, dict) \
            or not isinstance(quote, dict) or not isinstance(fees, list) \
            or not isinstance(bank_flows, list) or not isinstance(transfers, list):
        raise ValueError("live_close_snapshot_invalid")
    return {"account": account, "open_orders": orders, "positions": positions,
            "asset": asset, "quote": quote, "fees": fees,
            "external_flow_fingerprint": _digest_rows([*bank_flows, *transfers])}


def _digest_rows(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def submit_live_close(*, credentials_path: Path, cli_path: Path, client_order_id: str,
                      frozen_qty: str, order: dict[str, Any]) -> dict[str, Any]:
    """Submit only the frozen full-size BTC/USDC close; scheduled code never calls this."""
    expected = {"asset_class": "crypto", "qty": frozen_qty, "side": "sell",
                "symbol": "BTC/USDC", "time_in_force": "gtc", "type": "market"}
    try:
        valid_qty = Decimal(frozen_qty) > 0 and Decimal(frozen_qty).as_tuple().exponent >= -9
    except InvalidOperation:
        valid_qty = False
    if order != expected or not valid_qty \
            or not re.fullmatch(r"lm-ai-[0-9a-f]{24}", client_order_id):
        raise ValueError("live_close_shape_invalid")
    if _selected_mode() != "live":
        raise ValueError("investment_mode_effect_forbidden")
    env = _context(credentials_path, cli_path, "live")
    result = _run(cli_path, ["order", "submit", "--quiet", "--symbol", "BTC/USDC",
        "--qty", frozen_qty, "--side", "sell", "--type", "market", "--time-in-force", "gtc",
        "--client-order-id", client_order_id, "--jq",
        "{id,client_order_id,status,submitted_at,symbol,qty,side,type,time_in_force}"], env)
    if (not isinstance(result, dict) or result.get("client_order_id") != client_order_id
            or result.get("symbol") not in {"BTC/USDC", "BTCUSDC"}
            or Decimal(str(result.get("qty"))) != Decimal(frozen_qty)
            or result.get("side") != "sell" or result.get("type") != "market"
            or result.get("time_in_force") != "gtc"):
        raise ValueError("live_close_ack_invalid")
    return result


def read_live_close(*, credentials_path: Path, cli_path: Path, client_order_id: str,
                    frozen_qty: str, pre_usdc_qty: str, buy_gross_cost_usdc: str,
                    external_flow_fingerprint: str) -> dict[str, Any]:
    """Verify one close from its official order, all FILLs, and post-close positions."""
    env = _context(credentials_path, cli_path, "live")
    query = (f"first(.[]|select(.client_order_id=={json.dumps(client_order_id)})) // "
             "{found:false}|if .found==false then . else "
             "{found:true,id,client_order_id,status,filled_qty,symbol,side,qty,type,time_in_force} end")
    order = _run(cli_path, ["order", "list", "--quiet", "--status", "all", "--limit", "500",
                            "--jq", query], env)
    if order == {"found": False}:
        return {"status": "absent", "verified": False}
    try:
        expected_qty = Decimal(frozen_qty)
        filled_qty = Decimal(str(order.get("filled_qty") or "0"))
        if (order.get("found") is not True or order.get("client_order_id") != client_order_id
                or order.get("symbol") not in {"BTC/USDC", "BTCUSDC"}
                or order.get("side") != "sell" or Decimal(str(order.get("qty"))) != expected_qty
                or order.get("type") != "market" or order.get("time_in_force") != "gtc"):
            raise ValueError
    except (AttributeError, InvalidOperation, TypeError, ValueError) as error:
        raise ValueError("live_close_order_mismatch") from error
    if order.get("status") in {"canceled", "expired", "rejected"} and filled_qty == 0:
        return {"status": "terminal_failure", "verified": False, "order": order}
    if order.get("status") in {"canceled", "expired", "rejected"}:
        return {"status": "partial_terminal", "verified": False, "order": order}
    if order.get("status") != "filled":
        return {"status": "pending", "verified": False, "order": order}
    fills = _run(cli_path, ["account", "activity", "list", "--activity-types", "FILL",
        "--order-id", str(order["id"]), "--page-size", "100", "--direction", "desc", "--quiet",
        "--jq", f"[.[]|select(.order_id=={json.dumps(order['id'])})|"
        "{order_id,symbol,side,qty,price,transaction_time}]"], env)
    positions = _run(cli_path, ["position", "list", "--quiet", "--jq",
        "[.[]|{symbol,qty,market_value}]"], env)
    open_orders = _run(cli_path, ["order", "list", "--quiet", "--status", "open", "--limit", "500",
        "--jq", "length"], env)
    fees = _run(cli_path, ["account", "activity", "list", "--activity-types", "CFEE",
        "--direction", "asc", "--quiet", "--jq",
        f"[.[]|select(.order_id=={json.dumps(order['id'])})|"
        "{activity_type,order_id,symbol,qty,price,date}]"], env)
    bank_flows = _run(cli_path, ["account", "activity", "list", "--activity-types", "CSD,CSW",
        "--direction", "asc", "--quiet", "--jq",
        "[.[]|{activity_type,id,date,net_amount}]"], env)
    transfers = _run(cli_path, ["api", "GET", "/v2/wallets/transfers", "--quiet", "--jq",
        "[.[]|{id,asset,usd_value,direction,status}]"], env)
    try:
        fill_total = sum((Decimal(str(fill["qty"])) for fill in fills), Decimal("0"))
        btc = [row for row in positions if row.get("symbol") in {"BTCUSD", "BTCUSDC", "BTC/USDC"}]
        usdc = [row for row in positions if row.get("symbol") == "USDCUSD"]
        post_usdc = Decimal(str(usdc[0]["qty"]))
        if (not fills or any(fill.get("order_id") != order["id"] or fill.get("side") != "sell"
                or fill.get("symbol") not in {"BTC/USDC", "BTCUSDC"} for fill in fills)
                or fill_total != filled_qty or filled_qty != expected_qty or btc or len(usdc) != 1
                or len(positions) != 1 or post_usdc <= Decimal(pre_usdc_qty) or open_orders != 0
                or _digest_rows([*bank_flows, *transfers]) != external_flow_fingerprint):
            raise ValueError
        gross = sum((Decimal(str(fill["qty"])) * Decimal(str(fill["price"])) for fill in fills),
                    Decimal("0"))
        usdc_delta = post_usdc - Decimal(pre_usdc_qty)
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError("live_close_fill_mismatch") from error
    preliminary = {"order": order, "fills": fills, "post_usdc_qty": str(post_usdc),
                   "gross_proceeds_usdc": str(gross), "official_usdc_delta": str(usdc_delta)}
    if not fees:
        return {"status": "fee_pending", "verified": False, **preliminary}
    try:
        if len(fees) != 1 or fees[0].get("symbol") != "USDCUSD" \
                or fees[0].get("order_id") != order["id"]:
            raise ValueError
        sell_fee = -Decimal(str(fees[0]["qty"]))
        discrepancy = gross - usdc_delta
        if (sell_fee < 0 or sell_fee > gross * Decimal("0.01")
                or abs(discrepancy - sell_fee) > Decimal("0.00001")):
            raise ValueError
        realised = usdc_delta - Decimal(buy_gross_cost_usdc)
    except (InvalidOperation, KeyError, TypeError, ValueError) as error:
        raise ValueError("live_close_fee_mismatch") from error
    return {"status": "verified", "verified": True, **preliminary,
            "post_usdc_qty": str(post_usdc), "gross_proceeds_usdc": str(gross),
            "sell_fee_usdc": str(sell_fee), "realised_net_pnl_usdc": str(realised),
            "fee_activity": fees[0]}
