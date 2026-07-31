"""Broker REST API Trade Execution Module (Option 2).

This module provides a unified, keyless-to-MT5 interface for executing trades
via official Broker HTTP REST APIs (such as Bybit v5, OANDA v20, or generic
broker REST endpoints). 

Trades placed here reflect instantly on the user's Mobile MT5 / Trading app
without requiring an MT5 terminal or VPS to be hosted anywhere.
"""

import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional

import httpx

logger = logging.getLogger("broker_api")

# Broker API Configuration from Environment
BROKER_PROVIDER = os.environ.get("BROKER_PROVIDER", "").strip().lower()  # e.g., 'bybit', 'oanda', 'webhook'
BROKER_API_KEY = os.environ.get("BROKER_API_KEY", "").strip()
BROKER_API_SECRET = os.environ.get("BROKER_API_SECRET", "").strip()
BROKER_ACCOUNT_ID = os.environ.get("BROKER_ACCOUNT_ID", "").strip()
BROKER_WEBHOOK_URL = os.environ.get("BROKER_WEBHOOK_URL", "").strip()
EXNESS_SYMBOL_SUFFIX = os.environ.get("EXNESS_SYMBOL_SUFFIX", "").strip()  # e.g., 'm' for Exness Standard, 'c' for Cent, '' for Pro/Raw


def broker_execution_enabled() -> bool:
    """True if a broker REST API provider is configured with credentials."""
    if not BROKER_PROVIDER:
        return False
    if BROKER_PROVIDER in ("bybit", "oanda"):
        return bool(BROKER_API_KEY and (BROKER_API_SECRET or BROKER_ACCOUNT_ID))
    if BROKER_PROVIDER in ("webhook", "exness", "exness_webhook"):
        return bool(BROKER_WEBHOOK_URL)
    return False


def _generate_bybit_signature(
    api_key: str, secret: str, timestamp: str, payload: str
) -> str:
    """Generate HMAC SHA256 signature for Bybit v5 private REST API."""
    recv_window = "5000"
    param_str = f"{timestamp}{api_key}{recv_window}{payload}"
    return hmac.new(
        secret.encode("utf-8"), param_str.encode("utf-8"), hashlib.sha256
    ).hexdigest()


async def execute_bybit_order(
    http: httpx.AsyncClient, trade_spec: dict
) -> tuple[bool, str]:
    """Execute a limit/stop order on Bybit v5 Linear (Crypto, Gold, Silver CFD).

    Args:
        trade_spec: Dictionary containing asset, direction, order_type, entry, sl, tp, lot_size.
    Returns:
        (success, message): Tuple indicating success/failure and order ID or error message.
    """
    symbol = trade_spec.get("symbol", trade_spec["asset"].upper())
    if not symbol.endswith("USDT") and not symbol.endswith("USD"):
        symbol = f"{symbol}USDT"

    side = "Buy" if trade_spec["direction"] == "LONG" else "Sell"
    order_type = "Limit"  # Limit entry order

    qty = str(trade_spec.get("lot_size", "0.01"))
    price = str(trade_spec["entry"])
    sl = str(trade_spec["sl"])
    tp = str(trade_spec["tp"])

    url = "https://api.bybit.com/v5/order/create"
    timestamp = str(int(time.time() * 1000))

    body = {
        "category": "linear",
        "symbol": symbol,
        "side": side,
        "orderType": order_type,
        "qty": qty,
        "price": price,
        "stopLoss": sl,
        "takeProfit": tp,
        "timeInForce": "GTC",
    }
    payload_json = json.dumps(body)
    signature = _generate_bybit_signature(
        BROKER_API_KEY, BROKER_API_SECRET, timestamp, payload_json
    )

    headers = {
        "X-BAPI-API-KEY": BROKER_API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json",
    }

    try:
        response = await http.post(url, headers=headers, content=payload_json)
        data = response.json()
        if data.get("retCode") == 0:
            order_id = (data.get("result") or {}).get("orderId", "SUCCESS")
            return True, f"Bybit Order Placed (ID: {order_id})"
        return False, f"Bybit Error {data.get('retCode')}: {data.get('retMsg')}"
    except Exception as error:
        logger.exception("Bybit REST API order execution failed: %s", error)
        return False, f"Execution request failed: {error}"


async def execute_oanda_order(
    http: httpx.AsyncClient, trade_spec: dict
) -> tuple[bool, str]:
    """Execute a limit/stop order on OANDA v20 REST API (Forex, Metals, Indices).

    Args:
        trade_spec: Dictionary containing asset, direction, entry, sl, tp, lot_size.
    Returns:
        (success, message): Tuple indicating success/failure and order ID or error message.
    """
    account_id = BROKER_ACCOUNT_ID
    url = f"https://api-fxtrade.oanda.com/v3/accounts/{account_id}/orders"
    headers = {
        "Authorization": f"Bearer {BROKER_API_KEY}",
        "Content-Type": "application/json",
    }

    units = int(float(trade_spec.get("lot_size", 1000)))
    if trade_spec["direction"] == "SHORT":
        units = -units

    body = {
        "order": {
            "type": "LIMIT",
            "instrument": trade_spec["asset"].upper(),
            "units": str(units),
            "price": str(trade_spec["entry"]),
            "takeProfitOnFill": {"price": str(trade_spec["tp"])},
            "stopLossOnFill": {"price": str(trade_spec["sl"])},
            "timeInForce": "GTC",
        }
    }

    try:
        response = await http.post(url, headers=headers, json=body)
        data = response.json()
        if response.status_code in (200, 201):
            order_id = data.get("orderCreateTransaction", {}).get("id", "SUCCESS")
            return True, f"OANDA Order Placed (ID: {order_id})"
        error_msg = data.get("errorMessage") or str(data)
        return False, f"OANDA Error: {error_msg}"
    except Exception as error:
        logger.exception("OANDA REST API order execution failed: %s", error)
        return False, f"Execution request failed: {error}"


async def execute_webhook_order(
    http: httpx.AsyncClient, trade_spec: dict
) -> tuple[bool, str]:
    """Forward the order specification to a generic broker HTTP Webhook / REST endpoint."""
    if not BROKER_WEBHOOK_URL:
        return False, "BROKER_WEBHOOK_URL is not configured"

    try:
        response = await http.post(
            BROKER_WEBHOOK_URL,
            json=trade_spec,
            headers={"Content-Type": "application/json"},
        )
        if response.status_code in (200, 201, 202, 204):
            return True, f"Webhook Order Forwarded (HTTP {response.status_code})"
        return False, f"Webhook Error HTTP {response.status_code}: {response.text[:100]}"
    except Exception as error:
        logger.exception("Webhook order forwarding failed: %s", error)
        return False, f"Webhook request failed: {error}"


async def execute_exness_order(
    http: httpx.AsyncClient, trade_spec: dict
) -> tuple[bool, str]:
    """Execute/forward an order to an Exness Webhook / REST Terminal endpoint.

    Supports Exness MT5 symbol suffixes (e.g. 'm' for Standard accounts, 'c' for Cent accounts).
    """
    if not BROKER_WEBHOOK_URL:
        return False, "BROKER_WEBHOOK_URL is not configured for Exness"

    symbol = trade_spec.get("symbol", trade_spec["asset"]).strip().upper()
    if EXNESS_SYMBOL_SUFFIX and not symbol.endswith(EXNESS_SYMBOL_SUFFIX.upper()) and not symbol.endswith(EXNESS_SYMBOL_SUFFIX.lower()):
        symbol = f"{symbol}{EXNESS_SYMBOL_SUFFIX}"

    action = "BUY" if trade_spec["direction"] == "LONG" else "SELL"

    payload = {
        "broker": "exness",
        "symbol": symbol,
        "action": action,
        "order_type": "LIMIT",
        "price": trade_spec["entry"],
        "sl": trade_spec["sl"],
        "tp": trade_spec["tp"],
        "volume": trade_spec.get("lot_size", 0.01),
        "comment": "TelegramBot_AI_Signal",
    }

    headers = {"Content-Type": "application/json"}
    if BROKER_API_KEY:
        headers["Authorization"] = f"Bearer {BROKER_API_KEY}"
        headers["X-API-KEY"] = BROKER_API_KEY

    try:
        response = await http.post(BROKER_WEBHOOK_URL, json=payload, headers=headers)
        if response.status_code in (200, 201, 202, 204):
            return True, f"Exness Order Sent ({symbol} {action} @ {trade_spec['entry']})"
        return False, f"Exness Webhook Error HTTP {response.status_code}: {response.text[:100]}"
    except Exception as error:
        logger.exception("Exness order execution failed: %s", error)
        return False, f"Exness request failed: {error}"


async def execute_broker_order(trade_spec: dict) -> tuple[bool, str]:
    """Route an order to the configured broker REST API provider.

    Returns:
        (success, message): Success flag and human-readable confirmation or error.
    """
    if not broker_execution_enabled():
        return False, "Broker REST API execution is disabled (set BROKER_PROVIDER in .env)"

    async with httpx.AsyncClient(timeout=15) as http:
        if BROKER_PROVIDER == "bybit":
            return await execute_bybit_order(http, trade_spec)
        if BROKER_PROVIDER == "oanda":
            return await execute_oanda_order(http, trade_spec)
        if BROKER_PROVIDER in ("exness", "exness_webhook"):
            return await execute_exness_order(http, trade_spec)
        if BROKER_PROVIDER == "webhook":
            return await execute_webhook_order(http, trade_spec)

        return False, f"Unsupported BROKER_PROVIDER: {BROKER_PROVIDER}"
