#!/usr/bin/env python3
"""
4% 定投法状态管理工具

用法:
  python manage_state.py status
  python manage_state.py confirm <基金代码> [实际联接基金净值]
  python manage_state.py buy     <基金代码> <当时ETF价格> [实际联接基金净值]
  python manage_state.py unbuy   <基金代码>
  python manage_state.py sell    <基金代码> <份数> <实际卖出净值>
  python manage_state.py setref  <基金代码> <ETF基准价格>
  python manage_state.py remind  <基金代码>
  python manage_state.py skip    <基金代码>
  python manage_state.py pause   <基金代码>
  python manage_state.py resume  <基金代码>
  python manage_state.py resume-sell <基金代码>
  python manage_state.py reset   <基金代码>

说明:
  - 触发基准必须使用 ETF 价格；联接基金净值只用于成交记录。
  - confirm 会采用机器人最近生成的 pending_signal.etf_price 作为新基准。
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta

from fund_monitor_core import (
    LOG_FILE,
    configure_console,
    default_state,
    get_fund_state,
    held_shares,
    load_all_states,
    now_cn,
    save_all_states,
)


def append_action(record: dict) -> None:
    record.setdefault("date", now_cn().date().isoformat())
    record.setdefault("time", now_cn().strftime("%H:%M:%S"))
    with LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def positive_float(raw: str, label: str) -> float:
    value = float(raw)
    if value <= 0:
        raise ValueError(f"{label} 必须大于 0")
    return value


def positive_int(raw: str, label: str) -> int:
    value = int(raw)
    if value <= 0:
        raise ValueError(f"{label} 必须是正整数")
    return value


def cmd_status(states: dict) -> None:
    if not states:
        print("暂无任何基金记录。")
        return
    print("=" * 64)
    print("基金持仓与 ETF 触发基准")
    print("=" * 64)
    for code, saved in states.items():
        state = get_fund_state(states, code)
        anchor = state.get("anchor_etf_price")
        print(f"\n基金: {code}")
        print(
            f"当前持有: {held_shares(state)} 份 | 累计买入: {state['total_shares_bought']} "
            f"| 累计卖出: {state['total_shares_sold']}"
        )
        if anchor:
            print(
                f"ETF 基准: {anchor:.4f} | 日期: {state.get('anchor_date') or '—'} "
                f"| 来源: {state.get('anchor_source') or '—'}"
            )
        else:
            print("ETF 基准: 未设置（下次运行会用 ETF 昨收建立同口径基准）")
        print(
            f"提醒状态: {'已暂停' if state.get('paused') else '运行中'}"
            + (f" | 延后至 {state.get('snooze_until')}" if state.get("snooze_until") else "")
        )
        print(
            f"止盈提醒: {'已暂停' if state.get('sell_alert_paused') else '运行中'}"
            + (
                f" | 延后至 {state.get('sell_snooze_until')}"
                if state.get("sell_snooze_until")
                else ""
            )
        )
        pending = state.get("pending_signal")
        if pending:
            print(
                f"待确认信号: {pending.get('date')} ETF@{pending.get('etf_price'):.4f} "
                f"跌幅 {pending.get('drop_pct'):+.2f}%"
            )
        pending_sell = state.get("pending_sell_signal")
        if pending_sell:
            print(
                f"待确认卖出信号: {pending_sell.get('date')} "
                f"ETF@{pending_sell.get('etf_price'):.4f} "
                f"从高点回撤 {pending_sell.get('drawdown_pct'):+.2f}%"
            )
        for buy in state.get("buy_history", []):
            fund_nav = buy.get("fund_nav")
            suffix = f" | 联接基金净值 {fund_nav:.4f}" if fund_nav else ""
            print(
                f"  买入 #{buy.get('share_no', '?')}: {buy.get('date')} "
                f"ETF锚点 {buy.get('etf_price', 0):.4f}{suffix}"
            )
        for sell in state.get("sell_history", []):
            print(
                f"  卖出: {sell.get('date')} -{sell.get('shares')}份 "
                f"@{sell.get('fund_nav', 0):.4f}"
            )


def record_buy(
    states: dict,
    code: str,
    etf_price: float,
    fund_nav: float | None,
    source: str,
) -> None:
    state = get_fund_state(states, code)
    state["total_shares_bought"] += 1
    today = now_cn().date().isoformat()
    record = {
        "date": today,
        "etf_price": etf_price,
        "fund_nav": fund_nav,
        "share_no": state["total_shares_bought"],
        "source": source,
    }
    state["buy_history"].append(record)
    state["anchor_etf_price"] = etf_price
    state["anchor_date"] = today
    state["anchor_source"] = "已确认买入时的 ETF 价格"
    state["last_buy_date"] = today
    state["last_buy_price"] = fund_nav
    state["pending_signal"] = None
    state["paused"] = False
    state["snooze_until"] = None
    state["skip_buy_below_price"] = None
    state["skip_original_trigger_price"] = None
    state["sell_watch_high_etf_price"] = None
    state["pending_sell_signal"] = None
    state["sell_alert_paused"] = False
    state["sell_snooze_until"] = None
    states[code] = state
    save_all_states(states)
    append_action({"action": "buy", "fund": code, **record})
    print(f"[OK] {code} 已确认第 {state['total_shares_bought']} 次买入")
    print(f"     新 ETF 基准: {etf_price:.4f} | 下次 4% 触发价: {etf_price * 0.96:.4f}")
    if fund_nav:
        print(f"     实际联接基金净值: {fund_nav:.4f}")


def cmd_confirm(states: dict, code: str, fund_nav_raw: str | None) -> None:
    state = get_fund_state(states, code)
    pending = state.get("pending_signal")
    if not pending or not pending.get("etf_price"):
        raise ValueError(f"{code} 没有待确认买入信号")
    fund_nav = positive_float(fund_nav_raw, "联接基金净值") if fund_nav_raw else None
    record_buy(states, code, float(pending["etf_price"]), fund_nav, "confirmed_signal")


def cmd_buy(states: dict, code: str, etf_raw: str, fund_nav_raw: str | None) -> None:
    etf_price = positive_float(etf_raw, "ETF 价格")
    fund_nav = positive_float(fund_nav_raw, "联接基金净值") if fund_nav_raw else None
    record_buy(states, code, etf_price, fund_nav, "manual")


def cmd_unbuy(states: dict, code: str) -> None:
    state = get_fund_state(states, code)
    history = state["buy_history"]
    if not history:
        raise ValueError(f"{code} 没有买入记录可撤销")
    if state["total_shares_bought"] - 1 < state["total_shares_sold"]:
        raise ValueError("最后一笔买入对应的份额已被卖出，不能直接撤销")

    removed = history.pop()
    state["total_shares_bought"] -= 1
    if history:
        previous = history[-1]
        state["anchor_etf_price"] = previous["etf_price"]
        state["anchor_date"] = previous["date"]
        state["anchor_source"] = "撤销后回退至上一笔已确认买入"
        state["last_buy_date"] = previous["date"]
        state["last_buy_price"] = previous.get("fund_nav")
    else:
        state["anchor_etf_price"] = None
        state["anchor_date"] = None
        state["anchor_source"] = None
        state["last_buy_date"] = None
        state["last_buy_price"] = None
    state["sell_watch_high_etf_price"] = None
    state["pending_sell_signal"] = None
    state["sell_snooze_until"] = None
    states[code] = state
    save_all_states(states)
    append_action({"action": "unbuy", "fund": code, "removed": removed})
    print(f"[OK] 已撤销 {code} 最后一笔买入")


def cmd_sell(states: dict, code: str, shares_raw: str, nav_raw: str) -> None:
    shares = positive_int(shares_raw, "卖出份数")
    fund_nav = positive_float(nav_raw, "卖出净值")
    state = get_fund_state(states, code)
    position = held_shares(state)
    if shares > position:
        raise ValueError(f"卖出份数 {shares} 超过当前持有 {position}")

    today = now_cn().date().isoformat()
    state["total_shares_sold"] += shares
    state["sell_history"].append(
        {"date": today, "shares": shares, "fund_nav": fund_nav}
    )
    state["sell_watch_high_etf_price"] = None
    state["pending_sell_signal"] = None
    if held_shares(state) == 0:
        state["anchor_etf_price"] = None
        state["anchor_date"] = None
        state["anchor_source"] = None
        state["pending_signal"] = None
    states[code] = state
    save_all_states(states)
    append_action(
        {"action": "sell", "fund": code, "shares": shares, "fund_nav": fund_nav}
    )
    print(f"[OK] {code} 卖出 {shares} 份，当前持有 {held_shares(state)} 份")
    if held_shares(state) == 0:
        print("     仓位已清空；ETF 基准已清除，下轮将重新建立基准")


def cmd_setref(states: dict, code: str, etf_raw: str) -> None:
    etf_price = positive_float(etf_raw, "ETF 基准价格")
    state = get_fund_state(states, code)
    old = state.get("anchor_etf_price")
    state["anchor_etf_price"] = etf_price
    state["anchor_date"] = now_cn().date().isoformat()
    state["anchor_source"] = "手动设置"
    state["pending_signal"] = None
    states[code] = state
    save_all_states(states)
    append_action(
        {"action": "setref", "fund": code, "old_etf_price": old, "etf_price": etf_price}
    )
    print(f"[OK] {code} ETF 基准: {old} -> {etf_price:.4f}")
    print(f"     下次 4% 触发价: {etf_price * 0.96:.4f}")


def cmd_remind(states: dict, code: str) -> None:
    state = get_fund_state(states, code)
    state["snooze_until"] = (now_cn().date() + timedelta(days=1)).isoformat()
    state["pending_signal"] = None
    states[code] = state
    save_all_states(states)
    append_action({"action": "remind", "fund": code, "until": state["snooze_until"]})
    print(f"[OK] {code} 将在 {state['snooze_until']} 起重新提醒")


def cmd_skip(states: dict, code: str) -> None:
    state = get_fund_state(states, code)
    pending = state.get("pending_signal")
    if not pending or not pending.get("etf_price"):
        raise ValueError(f"{code} 没有可跳过的待确认买入信号")
    current_price = float(pending["etf_price"])
    state["skip_buy_below_price"] = round(current_price * 0.96, 6)
    state["skip_original_trigger_price"] = round(
        float(pending["anchor_etf_price"]) * 0.96, 6
    )
    state["pending_signal"] = None
    state["snooze_until"] = None
    states[code] = state
    save_all_states(states)
    append_action(
        {
            "action": "skip",
            "fund": code,
            "skipped_etf_price": current_price,
            "next_alert_price": state["skip_buy_below_price"],
        }
    )
    print(f"[OK] {code} 已跳过本档；下一提醒价约 {state['skip_buy_below_price']:.4f}")


def cmd_pause(states: dict, code: str) -> None:
    state = get_fund_state(states, code)
    state["paused"] = True
    state["pending_signal"] = None
    states[code] = state
    save_all_states(states)
    append_action({"action": "pause", "fund": code})
    print(f"[OK] {code} 已暂停提醒")


def cmd_resume(states: dict, code: str) -> None:
    state = get_fund_state(states, code)
    state["paused"] = False
    state["snooze_until"] = None
    states[code] = state
    save_all_states(states)
    append_action({"action": "resume", "fund": code})
    print(f"[OK] {code} 已恢复提醒")


def cmd_resume_sell(states: dict, code: str) -> None:
    state = get_fund_state(states, code)
    state["sell_alert_paused"] = False
    state["sell_snooze_until"] = None
    states[code] = state
    save_all_states(states)
    append_action({"action": "resume_sell", "fund": code})
    print(f"[OK] {code} 已恢复止盈提醒")


def cmd_reset(states: dict, code: str) -> None:
    if input(f"确认重置 {code} 的全部记录？输入 YES: ").strip() != "YES":
        print("已取消。")
        return
    states[code] = default_state()
    save_all_states(states)
    append_action({"action": "reset", "fund": code})
    print(f"[OK] {code} 已重置")


def main() -> int:
    configure_console()
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 0
    try:
        states = load_all_states()
        command = args[0].lower()
        if command == "status":
            cmd_status(states)
        elif command == "confirm" and len(args) >= 2:
            cmd_confirm(states, args[1], args[2] if len(args) >= 3 else None)
        elif command == "buy" and len(args) >= 3:
            cmd_buy(states, args[1], args[2], args[3] if len(args) >= 4 else None)
        elif command == "unbuy" and len(args) >= 2:
            cmd_unbuy(states, args[1])
        elif command == "sell" and len(args) >= 4:
            cmd_sell(states, args[1], args[2], args[3])
        elif command == "setref" and len(args) >= 3:
            cmd_setref(states, args[1], args[2])
        elif command == "remind" and len(args) >= 2:
            cmd_remind(states, args[1])
        elif command == "skip" and len(args) >= 2:
            cmd_skip(states, args[1])
        elif command == "pause" and len(args) >= 2:
            cmd_pause(states, args[1])
        elif command == "resume" and len(args) >= 2:
            cmd_resume(states, args[1])
        elif command == "resume-sell" and len(args) >= 2:
            cmd_resume_sell(states, args[1])
        elif command == "reset" and len(args) >= 2:
            cmd_reset(states, args[1])
        else:
            print(__doc__)
            return 2
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"[错误] {exc}")
        return 2
    except Exception as exc:
        print(f"[错误] 状态操作失败: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
