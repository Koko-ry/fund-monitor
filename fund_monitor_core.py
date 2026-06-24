#!/usr/bin/env python3
"""4% 定投监控的共享核心逻辑。"""

from __future__ import annotations

import json
import base64
import hashlib
import hmac
import os
import re
import sys
import tempfile
import urllib.request
import uuid
from urllib.error import HTTPError
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "funds.json"
STATE_FILE = BASE_DIR / "fund_state.json"
LOG_FILE = BASE_DIR / "daily_log.jsonl"
CN_TZ = ZoneInfo("Asia/Shanghai")
SAFE_ORDER_CUTOFF = time(14, 55)


def configure_console() -> None:
    """避免 Windows 旧代码页在输出 emoji 时抛出 UnicodeEncodeError。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass


def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def log(message: str) -> None:
    print(f"[{now_cn():%Y-%m-%d %H:%M:%S}] {message}")


def load_fund_list() -> list[dict]:
    with CONFIG_FILE.open("r", encoding="utf-8") as handle:
        funds = json.load(handle)
    if not isinstance(funds, list) or not funds:
        raise ValueError("funds.json 必须是非空数组")

    required = {"fund_code", "fund_name", "etf_market", "etf_code"}
    for index, fund in enumerate(funds, start=1):
        missing = required - set(fund)
        if missing:
            raise ValueError(f"funds.json 第 {index} 项缺少字段: {sorted(missing)}")
        if fund["etf_market"] not in {"sh", "sz", "bj"}:
            raise ValueError(f"{fund['fund_code']} 的 etf_market 必须是 sh/sz/bj")
        fund.setdefault("total_shares", 10)
        fund.setdefault("trigger_pct", 4.0)
        fund.setdefault("enable_sell_signal", True)
        fund.setdefault("sell_profit_trigger_pct", 15.0)
        fund.setdefault("sell_drawdown_pct", 4.0)
    return funds


def default_state() -> dict:
    return {
        "anchor_etf_price": None,
        "anchor_date": None,
        "anchor_source": None,
        "last_buy_price": None,
        "last_buy_date": None,
        "buy_history": [],
        "sell_history": [],
        "total_shares_bought": 0,
        "total_shares_sold": 0,
        "pending_signal": None,
        "paused": False,
        "snooze_until": None,
        "skip_buy_below_price": None,
        "skip_original_trigger_price": None,
        "feedback_history": [],
        "sell_watch_high_etf_price": None,
        "pending_sell_signal": None,
        "sell_alert_paused": False,
        "sell_snooze_until": None,
        "last_status_push_date": None,
    }


def normalize_state(saved: dict | None) -> dict:
    state = default_state()
    if isinstance(saved, dict):
        state.update(saved)
    state["buy_history"] = list(state.get("buy_history") or [])
    state["sell_history"] = list(state.get("sell_history") or [])
    state["feedback_history"] = list(state.get("feedback_history") or [])
    state["total_shares_bought"] = int(state.get("total_shares_bought") or 0)
    state["total_shares_sold"] = int(state.get("total_shares_sold") or 0)
    return state


def _api_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 FundMonitor/1.0",
    }
    token = os.environ.get("STATE_API_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _load_remote_states(url: str) -> dict:
    request = urllib.request.Request(url, headers=_api_headers())
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"状态 API HTTP {exc.code}: {detail}") from exc
    states = payload.get("state") if isinstance(payload, dict) and "state" in payload else payload
    if not isinstance(states, dict):
        raise ValueError("状态 API 的 GET 响应必须是 JSON 对象或 {'state': {...}}")
    return states


def load_all_states() -> dict:
    api_url = os.environ.get("STATE_API_URL", "").strip()
    if api_url:
        states = _load_remote_states(api_url)
        _atomic_write_json(STATE_FILE, states)
        log("[状态] 已从远程 API 读取")
        return states
    if not STATE_FILE.exists():
        return {}
    with STATE_FILE.open("r", encoding="utf-8") as handle:
        states = json.load(handle)
    if not isinstance(states, dict):
        raise ValueError("fund_state.json 顶层必须是 JSON 对象")
    return states


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def save_all_states(states: dict) -> None:
    _atomic_write_json(STATE_FILE, states)
    api_url = os.environ.get("STATE_API_URL", "").strip()
    if not api_url:
        return
    body = json.dumps(states, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        api_url,
        data=body,
        headers=_api_headers(),
        method=os.environ.get("STATE_API_METHOD", "PUT").upper(),
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status >= 300:
                raise RuntimeError(f"状态 API 写入失败: HTTP {response.status}")
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"状态 API 写入 HTTP {exc.code}: {detail}") from exc
    log("[状态] 已写回远程 API")


def get_fund_state(states: dict, fund_code: str) -> dict:
    return normalize_state(states.get(fund_code))


def held_shares(state: dict) -> int:
    return max(
        0,
        int(state.get("total_shares_bought") or 0)
        - int(state.get("total_shares_sold") or 0),
    )


def held_entry_etf_prices(state: dict) -> list[float]:
    """按先进先出假设，返回当前持有份额对应的 ETF 买入锚点。"""
    history = state.get("buy_history") or []
    prices = [
        float(item["etf_price"])
        for item in history
        if item.get("etf_price") and float(item["etf_price"]) > 0
    ]
    sold = int(state.get("total_shares_sold") or 0)
    position = held_shares(state)
    return prices[sold : sold + position]


def fetch_etf_realtime(market: str, etf_code: str) -> dict:
    symbol = f"{market}{etf_code}"
    request = urllib.request.Request(
        f"https://qt.gtimg.cn/q={symbol}",
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://gu.qq.com/",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read().decode("gbk", errors="replace")

    match = re.search(r'"([^"]+)"', raw)
    if not match:
        raise ValueError(f"无法解析腾讯行情 [{symbol}]")
    fields = match.group(1).split("~")
    if len(fields) < 33 or not fields[3].strip():
        raise ValueError(f"ETF 数据字段不足或价格为空 [{symbol}]")

    return {
        "symbol": symbol,
        "name": fields[1].strip(),
        "current_price": float(fields[3]),
        "prev_close": float(fields[4]),
        "change_pct": float(fields[32]) if fields[32].strip() else 0.0,
        "update_time": fields[30].strip(),
    }


def fetch_fund_last_nav(fund_code: str) -> dict:
    url = (
        "https://api.fund.eastmoney.com/f10/lsjz"
        f"?fundCode={fund_code}&pageIndex=1&pageSize=1"
        "&startDate=&endDate=&callback=jQuery"
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": f"https://fund.eastmoney.com/f10/jjjz_{fund_code}.html",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read().decode("utf-8")
    data = json.loads(raw[raw.index("{") : raw.rindex("}") + 1])
    records = data.get("Data", {}).get("LSJZList", [])
    if not records:
        return {}
    item = records[0]
    return {
        "last_nav": float(item.get("DWJZ") or 0),
        "last_nav_date": item.get("FSRQ", ""),
    }


def quote_trade_date(update_time: str) -> date | None:
    digits = re.sub(r"\D", "", update_time or "")
    if len(digits) < 8:
        return None
    return datetime.strptime(digits[:8], "%Y%m%d").date()


def _parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def build_feedback_url(result: dict, expires_days: int = 7) -> str | None:
    base_url = os.environ.get("FEEDBACK_BASE_URL", "").strip().rstrip("/")
    secret = os.environ.get("FEEDBACK_SIGNING_SECRET", "").strip()
    is_sell = bool(result.get("should_sell"))
    signal_kind = "sell" if is_sell else "buy"
    signal_key = "pending_sell_signal" if is_sell else "pending_signal"
    pending = result.get("state", {}).get(signal_key)
    if not base_url or not secret or not pending or not pending.get("id"):
        return None

    cfg = result["fund_cfg"]
    payload = {
        "v": 1,
        "kind": signal_kind,
        "sid": pending["id"],
        "code": cfg["fund_code"],
        "name": cfg["fund_name"],
        "price": pending["etf_price"],
        "drop": pending.get("drop_pct") or pending.get("drawdown_pct"),
        "trigger": (
            float(cfg.get("sell_drawdown_pct", 4.0))
            if is_sell
            else float(cfg.get("trigger_pct", 4.0))
        ),
        "shares": pending.get("suggested_shares"),
        "profit": pending.get("profit_pct"),
        "exp": int((now_cn() + timedelta(days=expires_days)).timestamp()),
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    signature = hmac.new(secret.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256)
    sig = base64.urlsafe_b64encode(signature.digest()).decode("ascii").rstrip("=")
    return f"{base_url}/feedback?t={encoded}.{sig}"


def analyze_one(fund_cfg: dict, states: dict, current_time: datetime | None = None) -> tuple[dict, bool]:
    current_time = current_time or now_cn()
    code = fund_cfg["fund_code"]
    state = get_fund_state(states, code)
    original_state = deepcopy(state)
    result = {
        "fund_cfg": fund_cfg,
        "etf_info": {},
        "nav_info": {},
        "state": state,
        "ref_price": None,
        "ref_source": None,
        "drop_pct": 0.0,
        "should_buy": False,
        "should_sell": False,
        "average_entry_etf_price": None,
        "profit_pct": None,
        "sell_high_etf_price": None,
        "sell_drawdown_pct": None,
        "buy_suppression_reason": None,
        "skipped_reason": None,
        "error": None,
        "before_cutoff": current_time.time() <= SAFE_ORDER_CUTOFF,
    }

    try:
        etf_info = fetch_etf_realtime(fund_cfg["etf_market"], fund_cfg["etf_code"])
        result["etf_info"] = etf_info
        log(
            f"[{code}] ETF {etf_info['symbol']}: {etf_info['current_price']:.4f} "
            f"({etf_info['change_pct']:+.2f}%) [{etf_info['update_time']}]"
        )
    except Exception as exc:
        result["error"] = f"获取 ETF 行情失败: {exc}"
        return result, False

    try:
        result["nav_info"] = fetch_fund_last_nav(code)
    except Exception as exc:
        log(f"[{code}] [警告] 联接基金净值仅用于展示，获取失败: {exc}")

    trade_date = quote_trade_date(result["etf_info"].get("update_time", ""))
    if trade_date != current_time.date() and os.environ.get("ALLOW_STALE_QUOTE") != "1":
        result["skipped_reason"] = (
            f"行情日期为 {trade_date or '未知'}，今天是 {current_time.date()}；"
            "可能为非交易日或行情尚未更新"
        )
        return result, False

    ref = state.get("anchor_etf_price")
    if ref:
        result["ref_source"] = state.get("anchor_source") or "已确认买入的 ETF 锚点"
    else:
        configured_ref = fund_cfg.get("initial_etf_ref_price")
        ref = float(configured_ref or result["etf_info"]["prev_close"])
        state["anchor_etf_price"] = ref
        state["anchor_date"] = trade_date.isoformat() if trade_date else current_time.date().isoformat()
        state["anchor_source"] = (
            "配置的初始 ETF 基准" if configured_ref else "首次运行时的 ETF 昨收"
        )
        result["ref_source"] = state["anchor_source"]
        log(f"[{code}] 建立同口径 ETF 初始基准: {ref:.4f}")

    if not ref or ref <= 0:
        result["error"] = "ETF 参考基准无效"
        return result, False

    result["ref_price"] = ref
    current_price = result["etf_info"]["current_price"]
    result["drop_pct"] = (current_price / ref - 1) * 100
    position = held_shares(state)
    max_shares = int(fund_cfg.get("total_shares", 10))
    trigger_pct = float(fund_cfg.get("trigger_pct", 4.0))
    original_trigger_price = ref * (1 - trigger_pct / 100)
    skip_price = state.get("skip_buy_below_price")
    if skip_price and current_price > original_trigger_price:
        state["skip_buy_below_price"] = None
        state["skip_original_trigger_price"] = None
        skip_price = None

    eligible_price = float(skip_price or original_trigger_price)
    snooze_until = _parse_date(state.get("snooze_until"))
    if state.get("paused"):
        result["buy_suppression_reason"] = "该基金已暂停提醒"
    elif snooze_until and current_time.date() < snooze_until:
        result["buy_suppression_reason"] = f"已延后至 {snooze_until.isoformat()} 再提醒"
    elif position >= max_shares:
        result["buy_suppression_reason"] = "已达到仓位上限"
    else:
        result["should_buy"] = current_price <= eligible_price

    if result["should_buy"]:
        pending = state.get("pending_signal")
        if not pending or pending.get("anchor_etf_price") != ref:
            state["pending_signal"] = {
                "id": uuid.uuid4().hex,
                "date": current_time.date().isoformat(),
                "etf_price": current_price,
                "anchor_etf_price": ref,
                "drop_pct": round(result["drop_pct"], 4),
                "suggested_share_no": position + 1,
            }
        state["snooze_until"] = None
    elif current_price > original_trigger_price:
        state["pending_signal"] = None

    if fund_cfg.get("enable_sell_signal", True) and position > 0:
        held_prices = held_entry_etf_prices(state)
        if len(held_prices) == position:
            average_entry = sum(held_prices) / len(held_prices)
            profit_pct = (current_price / average_entry - 1) * 100
            result["average_entry_etf_price"] = average_entry
            result["profit_pct"] = profit_pct

            profit_trigger = float(fund_cfg.get("sell_profit_trigger_pct", 15.0))
            drawdown_trigger = float(fund_cfg.get("sell_drawdown_pct", 4.0))
            high = state.get("sell_watch_high_etf_price")
            if profit_pct >= profit_trigger or high:
                high = max(float(high or 0), current_price)
                state["sell_watch_high_etf_price"] = high
                drawdown_pct = (current_price / high - 1) * 100
                result["sell_high_etf_price"] = high
                result["sell_drawdown_pct"] = drawdown_pct
                sell_snooze_until = _parse_date(state.get("sell_snooze_until"))
                sell_suppressed = state.get("sell_alert_paused") or (
                    sell_snooze_until and current_time.date() < sell_snooze_until
                )
                result["should_sell"] = (
                    drawdown_pct <= -drawdown_trigger and not sell_suppressed
                )
                if result["should_sell"]:
                    result["should_buy"] = False
                    pending_sell = state.get("pending_sell_signal")
                    if (
                        not pending_sell
                        or pending_sell.get("average_entry_etf_price")
                        != round(average_entry, 6)
                    ):
                        state["pending_sell_signal"] = {
                            "id": uuid.uuid4().hex,
                            "date": current_time.date().isoformat(),
                            "etf_price": current_price,
                            "average_entry_etf_price": round(average_entry, 6),
                            "high_etf_price": high,
                            "profit_pct": round(profit_pct, 4),
                            "drawdown_pct": round(drawdown_pct, 4),
                            "suggested_shares": position,
                        }
                    state["sell_snooze_until"] = None
                elif drawdown_pct > -drawdown_trigger:
                    state["pending_sell_signal"] = None

    states[code] = state
    return result, state != original_state


def run_analysis() -> tuple[list[dict], dict]:
    funds = load_fund_list()
    states = load_all_states()
    results = []
    changed = False
    current_time = now_cn()
    for fund in funds:
        result, state_changed = analyze_one(fund, states, current_time=current_time)
        results.append(result)
        changed = changed or state_changed
    if changed:
        save_all_states(states)
    return results, states


def append_daily_log(results: list[dict]) -> None:
    timestamp = now_cn()
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        for result in results:
            fund = result["fund_cfg"]
            record = {
                "date": timestamp.date().isoformat(),
                "time": timestamp.strftime("%H:%M:%S"),
                "fund_code": fund["fund_code"],
                "etf_price": result.get("etf_info", {}).get("current_price"),
                "quote_time": result.get("etf_info", {}).get("update_time"),
                "fund_last_nav": result.get("nav_info", {}).get("last_nav"),
                "ref_etf_price": result.get("ref_price"),
                "drop_pct_vs_ref": (
                    round(result["drop_pct"], 4) if result.get("ref_price") else None
                ),
                "should_buy": result.get("should_buy", False),
                "should_sell": result.get("should_sell", False),
                "held_shares": held_shares(result["state"]),
                "pending_signal": result["state"].get("pending_signal"),
                "paused": result["state"].get("paused", False),
                "snooze_until": result["state"].get("snooze_until"),
                "skip_buy_below_price": result["state"].get("skip_buy_below_price"),
                "pending_sell_signal": result["state"].get("pending_sell_signal"),
                "sell_alert_paused": result["state"].get("sell_alert_paused", False),
                "sell_snooze_until": result["state"].get("sell_snooze_until"),
                "skipped_reason": result.get("skipped_reason"),
                "error": result.get("error"),
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def has_runtime_error(results: list[dict]) -> bool:
    return any(result.get("error") for result in results)
