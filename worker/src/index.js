const STATE_KEY = "fund_state";
const MAX_BODY_BYTES = 512 * 1024;

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function htmlResponse(value, status = 200) {
  return new Response(value, {
    status,
    headers: {
      "content-type": "text/html; charset=utf-8",
      "cache-control": "no-store",
      "content-security-policy":
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
      "x-content-type-options": "nosniff",
      "referrer-policy": "no-referrer",
    },
  });
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function base64UrlToBytes(value) {
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = normalized + "=".repeat((4 - (normalized.length % 4)) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

function bytesToText(bytes) {
  return new TextDecoder().decode(bytes);
}

function constantTimeEqual(left, right) {
  if (left.length !== right.length) return false;
  let diff = 0;
  for (let index = 0; index < left.length; index += 1) {
    diff |= left[index] ^ right[index];
  }
  return diff === 0;
}

async function verifyFeedbackToken(token, secret) {
  if (!token || !secret || !token.includes(".")) {
    throw new Error("无效的操作链接");
  }
  const [encoded, signature] = token.split(".", 2);
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const expected = new Uint8Array(
    await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(encoded)),
  );
  const supplied = base64UrlToBytes(signature);
  if (!constantTimeEqual(expected, supplied)) {
    throw new Error("操作链接签名无效");
  }
  const payload = JSON.parse(bytesToText(base64UrlToBytes(encoded)));
  if (!payload.exp || Date.now() / 1000 > Number(payload.exp)) {
    throw new Error("操作链接已过期");
  }
  if (!payload.sid || !payload.code) {
    throw new Error("操作链接内容不完整");
  }
  return payload;
}

function chinaDate(offsetDays = 0) {
  const instant = new Date(Date.now() + offsetDays * 86400000);
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(instant);
}

function chinaTime() {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    hour12: false,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date());
}

async function loadStates(env) {
  const raw = await env.STATE_KV.get(STATE_KEY);
  if (!raw) return {};
  const states = JSON.parse(raw);
  return states && typeof states === "object" && !Array.isArray(states) ? states : {};
}

async function saveStates(env, states) {
  await env.STATE_KV.put(STATE_KEY, JSON.stringify(states));
}

function authorized(request, env) {
  const expected = env.STATE_API_TOKEN;
  return Boolean(expected) && request.headers.get("authorization") === `Bearer ${expected}`;
}

function defaultState(saved = {}) {
  return {
    anchor_etf_price: null,
    anchor_date: null,
    anchor_source: null,
    last_buy_price: null,
    last_buy_date: null,
    buy_history: [],
    sell_history: [],
    total_shares_bought: 0,
    total_shares_sold: 0,
    pending_signal: null,
    paused: false,
    snooze_until: null,
    skip_buy_below_price: null,
    skip_original_trigger_price: null,
    feedback_history: [],
    sell_watch_high_etf_price: null,
    pending_sell_signal: null,
    sell_alert_paused: false,
    sell_snooze_until: null,
    ...saved,
  };
}

function feedbackPage(payload, state, token, message = "") {
  const isSell = payload.kind === "sell";
  const pending = isSell ? state?.pending_sell_signal : state?.pending_signal;
  const current = pending && pending.id === payload.sid;
  const confirmText = isSell
    ? "只有点击“确认已卖出”才会减少持仓。"
    : "只有点击“确认已买入”才会增加持仓。";
  const status = message
    ? `<div class="notice">${escapeHtml(message)}</div>`
    : current
      ? `<div class="safe">${confirmText}</div>`
      : `<div class="notice">这个信号已经处理或已失效，不会重复修改状态。</div>`;
  const disabled = current ? "" : "disabled";
  const signalLabel = isSell ? "待确认止盈信号" : "待确认买入信号";
  const changeLabel = isSell ? "从观察高点回撤" : "相对买入基准";
  const navLabel = isSell
    ? "实际卖出净值（可留空）"
    : "实际联接基金净值（可稍后确认，可留空）";
  const primaryLabel = isSell ? "✅ 确认已卖出" : "✅ 确认已买入";
  const skipLabel = isSell ? "⏭ 跳过本次止盈" : "⏭ 跳过本档";
  const pauseLabel = isSell ? "🔕 暂停止盈提醒" : "🔕 暂停这只基金";
  const footerText = isSell
    ? `“跳过本次止盈”后，以当前价格重新观察，再回撤约 ${Number(payload.trigger || 4).toFixed(0)}% 时重新提醒。`
    : `“跳过本档”后，价格再跌约 ${Number(payload.trigger || 4).toFixed(0)}% 才会再次提醒；如果价格先重新涨回原触发线上方，下一次重新跌破时也会提醒。`;
  const sharesField = isSell
    ? `<label for="shares">卖出份数</label>
      <input id="shares" name="shares" inputmode="numeric" value="${escapeHtml(payload.shares || 1)}">`
    : "";

  return `<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>基金信号操作面板</title>
  <style>
    :root { color-scheme: light; font-family: -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif; }
    body { margin:0; background:#f3f6fb; color:#172033; }
    main { max-width:520px; margin:0 auto; padding:24px 16px 48px; }
    .card { background:#fff; border-radius:18px; padding:22px; box-shadow:0 10px 30px rgba(27,45,80,.08); }
    h1 { font-size:24px; margin:0 0 8px; }
    .muted { color:#6b7485; font-size:14px; }
    .price { font-size:34px; font-weight:750; margin:20px 0 4px; }
    .drop { color:#15803d; font-weight:650; }
    .safe,.notice { margin:18px 0; padding:12px 14px; border-radius:10px; font-size:14px; }
    .safe { background:#ecfdf3; color:#166534; }
    .notice { background:#fff7ed; color:#9a3412; }
    label { display:block; font-size:14px; margin:18px 0 7px; color:#475569; }
    input { box-sizing:border-box; width:100%; padding:12px; border:1px solid #cad2df; border-radius:10px; font-size:16px; }
    .grid { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:16px; }
    button { border:0; border-radius:11px; padding:13px 10px; font-size:15px; font-weight:650; cursor:pointer; }
    button:disabled { opacity:.45; cursor:not-allowed; }
    .buy { grid-column:1/-1; background:${isSell ? "#d4380d" : "#1677ff"}; color:#fff; }
    .later { background:#e8f1ff; color:#174ea6; }
    .skip { background:#eef2f7; color:#334155; }
    .pause { grid-column:1/-1; background:#fff1f2; color:#be123c; }
    #result { margin-top:16px; min-height:22px; font-size:14px; font-weight:600; }
    footer { margin-top:18px; color:#7b8493; font-size:12px; line-height:1.6; }
  </style>
</head>
<body>
<main>
  <section class="card">
    <h1>${escapeHtml(payload.name)}</h1>
    <div class="muted">基金代码 ${escapeHtml(payload.code)} · ${signalLabel}</div>
    <div class="price">${Number(payload.price).toFixed(4)}</div>
    <div class="drop">${changeLabel} ${Number(payload.drop).toFixed(2)}%</div>
    ${isSell && payload.profit != null ? `<div class="muted">当前持仓收益约 ${Number(payload.profit).toFixed(2)}%</div>` : ""}
    ${status}
    <form id="actions">
      <input type="hidden" name="token" value="${escapeHtml(token)}">
      ${sharesField}
      <label for="fund_nav">${navLabel}</label>
      <input id="fund_nav" name="fund_nav" inputmode="decimal" placeholder="例如 0.9600">
      <div class="grid">
        <button ${disabled} class="buy" name="action" value="confirm">${primaryLabel}</button>
        <button ${disabled} class="later" name="action" value="remind">⏰ 明天提醒</button>
        <button ${disabled} class="skip" name="action" value="skip">${skipLabel}</button>
        <button ${disabled} class="pause" name="action" value="pause">${pauseLabel}</button>
      </div>
    </form>
    <div id="result"></div>
    <footer>
      ${footerText}
    </footer>
  </section>
</main>
<script>
  const form = document.getElementById("actions");
  form.addEventListener("click", async (event) => {
    const button = event.target.closest("button[name=action]");
    if (!button) return;
    event.preventDefault();
    const isSell = ${JSON.stringify(isSell)};
    if (button.value === "confirm" && !confirm(isSell
      ? "确认你已经实际卖出了吗？只有确认后才会减少持仓。"
      : "确认你已经实际申购了吗？只有确认后才会增加一份持仓。")) return;
    if (button.value === "pause" && !confirm(isSell
      ? "确认暂停这只基金的止盈提醒？"
      : "确认暂停这只基金的买入提醒？")) return;
    const result = document.getElementById("result");
    result.textContent = "正在保存…";
    for (const item of form.querySelectorAll("button")) item.disabled = true;
    try {
      const response = await fetch("/api/action", {
        method: "POST",
        headers: {"content-type": "application/json"},
        body: JSON.stringify({
          token: form.elements.token.value,
          action: button.value,
          fund_nav: form.elements.fund_nav.value.trim(),
          shares: form.elements.shares ? form.elements.shares.value.trim() : ""
        })
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "保存失败");
      result.textContent = "✅ " + data.message;
    } catch (error) {
      result.textContent = "❌ " + error.message;
      for (const item of form.querySelectorAll("button")) item.disabled = false;
    }
  });
</script>
</body>
</html>`;
}

async function applyFeedback(request, env) {
  const bodyText = await request.text();
  if (bodyText.length > 20_000) return jsonResponse({ error: "请求过大" }, 413);
  let body;
  try {
    body = JSON.parse(bodyText);
  } catch {
    return jsonResponse({ error: "请求格式错误" }, 400);
  }

  let payload;
  try {
    payload = await verifyFeedbackToken(body.token, env.FEEDBACK_SIGNING_SECRET);
  } catch (error) {
    return jsonResponse({ error: error.message }, 401);
  }

  const actions = new Set(["confirm", "remind", "skip", "pause"]);
  if (!actions.has(body.action)) return jsonResponse({ error: "未知操作" }, 400);
  const states = await loadStates(env);
  const state = defaultState(states[payload.code]);
  const isSell = payload.kind === "sell";
  const pending = isSell ? state.pending_sell_signal : state.pending_signal;
  if (!pending || pending.id !== payload.sid) {
    return jsonResponse({ error: "该信号已经处理或失效" }, 409);
  }

  const today = chinaDate();
  let message;
  const historyItem = {
    date: today,
    time: chinaTime(),
    action: body.action,
    kind: isSell ? "sell" : "buy",
    signal_id: payload.sid,
    etf_price: Number(pending.etf_price),
  };

  if (isSell && body.action === "confirm") {
    const shares = Number(body.shares);
    const held = Math.max(
      0,
      Number(state.total_shares_bought || 0) - Number(state.total_shares_sold || 0),
    );
    const fundNav = body.fund_nav ? Number(body.fund_nav) : null;
    if (!Number.isInteger(shares) || shares <= 0 || shares > held) {
      return jsonResponse({ error: `卖出份数必须是1到${held}之间的整数` }, 400);
    }
    if (body.fund_nav && (!Number.isFinite(fundNav) || fundNav <= 0)) {
      return jsonResponse({ error: "卖出净值必须是大于0的数字" }, 400);
    }
    state.total_shares_sold = Number(state.total_shares_sold || 0) + shares;
    state.sell_history = Array.isArray(state.sell_history) ? state.sell_history : [];
    state.sell_history.push({
      date: today,
      shares,
      fund_nav: fundNav,
      etf_price: Number(pending.etf_price),
      source: "feedback_panel",
    });
    state.pending_sell_signal = null;
    state.sell_watch_high_etf_price = null;
    state.sell_snooze_until = null;
    if (state.total_shares_bought - state.total_shares_sold <= 0) {
      state.anchor_etf_price = null;
      state.anchor_date = null;
      state.anchor_source = null;
      state.pending_signal = null;
    }
    historyItem.shares = shares;
    historyItem.fund_nav = fundNav;
    message = `已记录卖出 ${shares} 份，当前剩余 ${Math.max(0, held - shares)} 份。`;
  } else if (!isSell && body.action === "confirm") {
    const fundNav = body.fund_nav ? Number(body.fund_nav) : null;
    if (body.fund_nav && (!Number.isFinite(fundNav) || fundNav <= 0)) {
      return jsonResponse({ error: "基金净值必须是大于0的数字" }, 400);
    }
    state.total_shares_bought = Number(state.total_shares_bought || 0) + 1;
    state.buy_history = Array.isArray(state.buy_history) ? state.buy_history : [];
    state.buy_history.push({
      date: today,
      etf_price: Number(pending.etf_price),
      fund_nav: fundNav,
      share_no: state.total_shares_bought,
      source: "feedback_panel",
    });
    state.anchor_etf_price = Number(pending.etf_price);
    state.anchor_date = today;
    state.anchor_source = "反馈面板确认买入时的 ETF 价格";
    state.last_buy_date = today;
    state.last_buy_price = fundNav;
    state.pending_signal = null;
    state.paused = false;
    state.snooze_until = null;
    state.skip_buy_below_price = null;
    state.skip_original_trigger_price = null;
    state.sell_watch_high_etf_price = null;
    state.pending_sell_signal = null;
    historyItem.fund_nav = fundNav;
    message = `已记录第 ${state.total_shares_bought} 次买入。`;
  } else if (isSell && body.action === "remind") {
    state.sell_snooze_until = chinaDate(1);
    state.pending_sell_signal = null;
    message = `已延后，${state.sell_snooze_until} 起重新判断止盈信号。`;
  } else if (!isSell && body.action === "remind") {
    state.snooze_until = chinaDate(1);
    state.pending_signal = null;
    message = `已延后，${state.snooze_until} 起重新判断并提醒。`;
  } else if (isSell && body.action === "skip") {
    state.sell_watch_high_etf_price = Number(pending.etf_price);
    state.pending_sell_signal = null;
    state.sell_snooze_until = null;
    message = `已跳过本次止盈；从当前价格再回撤约 ${Number(payload.trigger || 4).toFixed(0)}% 时重新提醒。`;
  } else if (!isSell && body.action === "skip") {
    const trigger = Number(payload.trigger || 4);
    state.skip_buy_below_price = Number(
      (Number(pending.etf_price) * (1 - trigger / 100)).toFixed(6),
    );
    state.skip_original_trigger_price = Number(
      (Number(pending.anchor_etf_price) * (1 - trigger / 100)).toFixed(6),
    );
    state.pending_signal = null;
    state.snooze_until = null;
    historyItem.next_alert_price = state.skip_buy_below_price;
    message = `已跳过本档，下一提醒价约 ${state.skip_buy_below_price.toFixed(4)}。`;
  } else if (isSell) {
    state.sell_alert_paused = true;
    state.pending_sell_signal = null;
    message = "已暂停这只基金的止盈提醒。";
  } else {
    state.paused = true;
    state.pending_signal = null;
    message = "已暂停这只基金的买入提醒。";
  }

  state.feedback_history = Array.isArray(state.feedback_history)
    ? state.feedback_history
    : [];
  state.feedback_history.push(historyItem);
  state.feedback_history = state.feedback_history.slice(-100);
  states[payload.code] = state;
  await saveStates(env, states);
  return jsonResponse({ ok: true, action: body.action, message });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return jsonResponse({ ok: true, service: "fund-monitor-feedback" });
    }
    if (url.pathname === "/state") {
      if (!authorized(request, env)) return jsonResponse({ error: "unauthorized" }, 401);
      if (request.method === "GET") return jsonResponse(await loadStates(env));
      if (request.method === "PUT" || request.method === "POST") {
        const text = await request.text();
        if (text.length > MAX_BODY_BYTES) return jsonResponse({ error: "payload too large" }, 413);
        let states;
        try {
          states = JSON.parse(text);
        } catch {
          return jsonResponse({ error: "invalid json" }, 400);
        }
        if (!states || typeof states !== "object" || Array.isArray(states)) {
          return jsonResponse({ error: "state must be an object" }, 400);
        }
        await saveStates(env, states);
        return jsonResponse({ ok: true });
      }
      return jsonResponse({ error: "method not allowed" }, 405);
    }
    if (url.pathname === "/feedback" && request.method === "GET") {
      const token = url.searchParams.get("t") || "";
      try {
        const payload = await verifyFeedbackToken(token, env.FEEDBACK_SIGNING_SECRET);
        const states = await loadStates(env);
        const state = defaultState(states[payload.code]);
        return htmlResponse(feedbackPage(payload, state, token));
      } catch (error) {
        return htmlResponse(
          `<meta charset="utf-8"><title>链接无效</title><p style="font:16px sans-serif;padding:24px">❌ ${escapeHtml(error.message)}</p>`,
          401,
        );
      }
    }
    if (url.pathname === "/api/action" && request.method === "POST") {
      return applyFeedback(request, env);
    }
    return htmlResponse(
      '<meta charset="utf-8"><title>基金反馈服务</title><p style="font:16px sans-serif;padding:24px">基金反馈服务运行中。</p>',
    );
  },
};

export {
  applyFeedback,
  chinaDate,
  defaultState,
  feedbackPage,
  verifyFeedbackToken,
};
