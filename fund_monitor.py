#!/usr/bin/env python3
"""企业微信版 4% 定投监控。"""

import json
import os
import urllib.request

from fund_monitor_core import (
    append_daily_log,
    configure_console,
    has_runtime_error,
    held_shares,
    log,
    now_cn,
    run_analysis,
)


WECOM_WEBHOOK = os.environ.get("WECOM_WEBHOOK", "").strip()


def build_wecom_markdown(results: list[dict]) -> str:
    timestamp = now_cn()
    any_sell = any(result.get("should_sell") for result in results)
    any_buy = any(result.get("should_buy") for result in results)
    title = (
        "💰 出现待确认止盈信号"
        if any_sell
        else ("🚨 出现待确认买入信号" if any_buy else "✋ 今日无需操作")
    )
    lines = [f"## 4%定投法 · {title}", f"> {timestamp:%Y-%m-%d %H:%M}", ""]
    for result in results:
        cfg = result["fund_cfg"]
        lines.append(f"### {cfg['fund_name']}（{cfg['fund_code']}）")
        if result.get("error"):
            lines.extend([f"> ❌ {result['error']}", ""])
            continue
        if result.get("skipped_reason"):
            lines.extend([f"> 📅 {result['skipped_reason']}", ""])
            continue
        position = held_shares(result["state"])
        maximum = int(cfg.get("total_shares", 10))
        trigger = float(cfg.get("trigger_pct", 4.0))
        lines.extend(
            [
                f"> ETF实时价：**{result['etf_info']['current_price']:.4f}**",
                f"> ETF同口径基准：{result['ref_price']:.4f}",
                f"> 距基准：{result['drop_pct']:+.2f}%（触发线 ≤ -{trigger:.2f}%）",
                f"> 当前持有：{position}/{maximum} 份",
            ]
        )
        if result.get("should_sell"):
            lines.append(
                f"> 💰 **从高点回撤 {result['sell_drawdown_pct']:+.2f}%，请人工确认止盈**"
            )
        elif result["should_buy"]:
            lines.append(
                f"> 🚨 **触发第 {position + 1}/{maximum} 份提醒；尚未自动记账**"
            )
            lines.append(
                f"> 买入后执行：`python manage_state.py confirm {cfg['fund_code']} [实际净值]`"
            )
        lines.append("")
    lines.append("> 联接基金净值仅用于成交记录，不与 ETF 价格直接比较。")
    return "\n".join(lines)


def send_wecom(results: list[dict]) -> bool:
    if not WECOM_WEBHOOK:
        log("[通知] 未配置 WECOM_WEBHOOK")
        return False
    payload = json.dumps(
        {
            "msgtype": "markdown",
            "markdown": {"content": build_wecom_markdown(results)},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        WECOM_WEBHOOK,
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read().decode("utf-8"))
        if data.get("errcode") == 0:
            log("[通知] 企业微信推送成功")
            return True
        log(f"[通知] 企业微信推送失败: {data}")
    except Exception as exc:
        log(f"[通知] 企业微信推送异常: {exc}")
    return False


def main() -> int:
    configure_console()
    try:
        results, _states = run_analysis()
        append_daily_log(results)
        notification_ok = send_wecom(results)
    except Exception as exc:
        log(f"[致命错误] {exc}")
        return 1
    print(build_wecom_markdown(results))
    return 0 if notification_ok and not has_runtime_error(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
