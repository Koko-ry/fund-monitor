#!/usr/bin/env python3
"""PushPlus 版 4% 定投监控。"""

import html
import json
import os
import urllib.request

from fund_monitor_core import (
    append_daily_log,
    build_feedback_url,
    configure_console,
    has_runtime_error,
    held_shares,
    log,
    now_cn,
    run_analysis,
)


PUSHPLUS_TOKEN = os.environ.get("PUSHPLUS_TOKEN", "").strip()


def build_pushplus_html(results: list[dict]) -> tuple[str, str]:
    timestamp = now_cn()
    any_sell = any(result.get("should_sell") for result in results)
    any_buy = any(result.get("should_buy") for result in results)
    has_error = has_runtime_error(results)
    all_skipped = all(result.get("skipped_reason") for result in results)

    if any_sell:
        title = "💰 4%定投法 · 出现待确认止盈信号"
    elif any_buy:
        title = "🚨 4%定投法 · 出现待确认买入信号"
    elif has_error:
        title = "⚠️ 4%定投法 · 数据异常"
    elif all_skipped:
        title = "📅 4%定投法 · 非交易日或行情未更新"
    else:
        title = "✋ 4%定投法 · 今日无需操作"

    body = f"<h2>{html.escape(title)}</h2><p><b>{timestamp:%Y-%m-%d %H:%M}</b></p><hr/>"
    for result in results:
        cfg = result["fund_cfg"]
        state = result["state"]
        name = html.escape(cfg["fund_name"])
        code = html.escape(cfg["fund_code"])
        body += f"<h3>{name}（{code}）</h3>"

        if result.get("error"):
            body += f"<p style='color:red'>❌ {html.escape(result['error'])}</p><hr/>"
            continue
        if result.get("skipped_reason"):
            body += f"<p style='color:gray'>📅 {html.escape(result['skipped_reason'])}</p><hr/>"
            continue

        etf = result["etf_info"]
        nav = result.get("nav_info") or {}
        current = etf["current_price"]
        change = etf["change_pct"]
        position = held_shares(state)
        maximum = int(cfg.get("total_shares", 10))
        trigger = float(cfg.get("trigger_pct", 4.0))
        drop = result["drop_pct"]
        quote_color = "green" if change < 0 else "red"

        body += (
            f"<p>ETF {html.escape(etf['symbol'])} 实时价：<b>{current:.4f}</b> "
            f"<span style='color:{quote_color}'>{change:+.2f}%</span> "
            f"[{html.escape(etf.get('update_time', ''))}]</p>"
        )
        body += (
            f"<p>ETF 同口径基准：<b>{result['ref_price']:.4f}</b> "
            f"（{html.escape(result.get('ref_source') or '')}）</p>"
        )
        body += f"<p>距基准：<b>{drop:+.2f}%</b>；触发线：≤ -{trigger:.2f}%</p>"
        if nav.get("last_nav"):
            body += (
                f"<p>联接基金最近净值：{nav['last_nav']:.4f} "
                f"({html.escape(nav.get('last_nav_date', ''))})，仅展示、不参与比较</p>"
            )
        body += f"<p>当前持有：{position}/{maximum} 份；剩余：{max(0, maximum-position)} 份</p>"

        if result.get("average_entry_etf_price"):
            body += (
                f"<p>持仓 ETF 等价均价：{result['average_entry_etf_price']:.4f}；"
                f"当前收益：{result['profit_pct']:+.2f}%</p>"
            )
        if result.get("should_sell"):
            feedback_url = build_feedback_url(result)
            body += (
                f"<p style='color:red'><b>💰 触发止盈提醒：建议人工确认是否卖出当前 {position} 份</b></p>"
                f"<p>观察高点：{result['sell_high_etf_price']:.4f}；"
                f"从高点回撤：{result['sell_drawdown_pct']:+.2f}%</p>"
                "<p>机器人不会自动卖出或自动修改持仓。</p>"
            )
            if feedback_url:
                body += (
                    "<p style='margin:18px 0'>"
                    f"<a href='{html.escape(feedback_url, quote=True)}' "
                    "style='background:#d4380d;color:white;padding:12px 18px;"
                    "border-radius:8px;text-decoration:none;font-weight:bold'>"
                    "打开卖出操作面板</a></p>"
                    "<p><small>可选择：确认已卖 / 明天提醒 / 跳过本次 / 暂停止盈提醒</small></p>"
                )
        elif result.get("sell_high_etf_price"):
            body += (
                f"<p style='color:orange'>👀 已进入止盈观察：高点 "
                f"{result['sell_high_etf_price']:.4f}，当前回撤 "
                f"{result['sell_drawdown_pct']:+.2f}%</p>"
            )
        elif position >= maximum:
            body += "<p style='color:orange'>⚠️ 已达仓位上限，暂停买入</p>"
        elif result["should_buy"]:
            feedback_url = build_feedback_url(result)
            body += (
                f"<p style='color:red'><b>🚨 触发第 {position + 1}/{maximum} 份买入提醒</b></p>"
                "<p>本次只记录“待确认信号”，不会自动假定你已经买入。</p>"
            )
            if result["before_cutoff"]:
                body += "<p><b>请预留时间，在平台 15:00 截止前完成申购。</b></p>"
            else:
                body += "<p><b>当前已过 14:55 安全时间，不建议仓促下单；请人工确认交易规则。</b></p>"
            if feedback_url:
                body += (
                    "<p style='margin:18px 0'>"
                    f"<a href='{html.escape(feedback_url, quote=True)}' "
                    "style='background:#1677ff;color:white;padding:12px 18px;"
                    "border-radius:8px;text-decoration:none;font-weight:bold'>"
                    "打开操作面板</a></p>"
                    "<p><small>可选择：已买入 / 明天提醒 / 跳过本档 / 暂停基金</small></p>"
                )
            else:
                body += (
                    f"<p>买入后确认：<code>python manage_state.py confirm {code} "
                    "[实际基金净值]</code></p>"
                )
        else:
            trigger_price = result["ref_price"] * (1 - trigger / 100)
            suppression = result.get("buy_suppression_reason")
            if suppression:
                body += f"<p style='color:gray'>🔕 {html.escape(suppression)}</p>"
            elif state.get("skip_buy_below_price"):
                body += (
                    f"<p style='color:green'>✋ 已跳过上一档；下一提醒价约 "
                    f"{state['skip_buy_below_price']:.4f}</p>"
                )
            else:
                body += f"<p style='color:green'>✋ 等待；ETF 触发价约 {trigger_price:.4f}</p>"
        body += "<hr/>"

    body += (
        "<p><small>估值颜色可作为额外过滤条件，但不要用场外基金净值与 ETF 价格直接比较。</small></p>"
        "<p><small>本播报仅供策略执行记录，不构成投资建议。</small></p>"
    )
    return title, body


def send_pushplus(results: list[dict]) -> bool:
    if not PUSHPLUS_TOKEN:
        log("[通知] 未配置 PUSHPLUS_TOKEN")
        return False
    title, content = build_pushplus_html(results)
    payload = json.dumps(
        {
            "token": PUSHPLUS_TOKEN,
            "title": title,
            "content": content,
            "template": "html",
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://www.pushplus.plus/send",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            response_data = json.loads(response.read().decode("utf-8"))
        if str(response_data.get("code")) == "200":
            log("[通知] PushPlus 推送成功")
            return True
        log(f"[通知] PushPlus 推送失败: {response_data}")
    except Exception as exc:
        log(f"[通知] PushPlus 推送异常: {exc}")
    return False


def main() -> int:
    configure_console()
    log("=== 4% 定投法监控启动 ===")
    try:
        results, _states = run_analysis()
        append_daily_log(results)
        notification_ok = send_pushplus(results)
    except Exception as exc:
        log(f"[致命错误] {exc}")
        return 1

    for result in results:
        cfg = result["fund_cfg"]
        if result.get("error"):
            print(f"{cfg['fund_name']}: {result['error']}")
        elif result.get("skipped_reason"):
            print(f"{cfg['fund_name']}: {result['skipped_reason']}")
        else:
            if result.get("should_sell"):
                flag = "💰 待确认止盈"
            elif result["should_buy"]:
                flag = "🚨 待确认买入"
            else:
                flag = "✋ 等待"
            print(
                f"{cfg['fund_name']} ({cfg['fund_code']}): "
                f"ETF {result['etf_info']['current_price']:.4f}, "
                f"距基准 {result['drop_pct']:+.2f}% {flag}"
            )

    log("=== 监控完成 ===")
    return 0 if notification_ok and not has_runtime_error(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
