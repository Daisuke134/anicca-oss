"""Pinned, paper-only Alpaca CLI observation boundary."""

from __future__ import annotations

import json
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
        "data", "crypto", "latest-quotes", "--symbols", "BTC/USD,ETH/USD", "--quiet", "--jq",
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
        allocated = sum((abs(Decimal(str(row["market_value"]))) for row in positions), Decimal("0"))
        unrealized = sum((Decimal(str(row["unrealized_pl"])) for row in positions), Decimal("0"))
        if any(not isinstance(row, dict) or row.get("activity_type") not in {"CSD", "CSW"}
               for row in cash_activities):
            raise ValueError
        cash_flow = sum((Decimal(str(row["net_amount"])) for row in cash_activities), Decimal("0"))
        values = (equity, allocated, unrealized, cash_flow)
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
    return {"account": account, "clock": clock, "crypto": crypto,
            "open_orders": orders, "option_quotes": options, "positions": len(positions), "risk": risk,
            "qqq_asset": qqq_asset, "qqq_quote": qqq_quote, "spy": spy}


def submit_order(
    *, credentials_path: Path, cli_path: Path, client_order_id: str,
    order: dict[str, Any], mode: str | None = None,
) -> dict[str, Any]:
    """Submit one already-gated paper order through the pinned CLI."""
    if not re.fullmatch(r"lm-ai-[0-9a-f]{24}", client_order_id):
        raise ValueError("client_order_id_invalid")
    mode = _selected_mode(mode)
    if mode != "paper":
        raise ValueError("investment_mode_effect_forbidden")
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
