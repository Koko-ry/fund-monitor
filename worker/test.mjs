import assert from "node:assert/strict";
import { test } from "node:test";
import worker from "./src/index.js";

class MemoryKV {
  constructor() {
    this.values = new Map();
  }
  async get(key) {
    return this.values.get(key) ?? null;
  }
  async put(key, value) {
    this.values.set(key, value);
  }
}

function toBase64Url(bytes) {
  return Buffer.from(bytes)
    .toString("base64")
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replaceAll("=", "");
}

async function tokenFor(payload, secret) {
  const encoded = toBase64Url(Buffer.from(JSON.stringify(payload)));
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(encoded),
  );
  return `${encoded}.${toBase64Url(new Uint8Array(sig))}`;
}

function envWithState(state) {
  const env = {
    STATE_KV: new MemoryKV(),
    STATE_API_TOKEN: "state-token",
    FEEDBACK_SIGNING_SECRET: "feedback-secret",
  };
  return env.STATE_KV.put("fund_state", JSON.stringify(state)).then(() => env);
}

const pendingState = {
  "000000": {
    anchor_etf_price: 1,
    pending_signal: {
      id: "signal-1",
      date: "2026-06-23",
      etf_price: 0.95,
      anchor_etf_price: 1,
      drop_pct: -5,
      suggested_share_no: 1,
    },
    buy_history: [],
    sell_history: [],
    total_shares_bought: 0,
    total_shares_sold: 0,
  },
};

test("state API requires bearer token", async () => {
  const env = await envWithState(pendingState);
  const response = await worker.fetch(new Request("https://example.com/state"), env);
  assert.equal(response.status, 401);
});

test("confirm is the only action that increases holdings", async () => {
  const env = await envWithState(structuredClone(pendingState));
  const token = await tokenFor(
    {
      v: 1,
      sid: "signal-1",
      code: "000000",
      name: "测试基金",
      price: 0.95,
      drop: -5,
      trigger: 4,
      exp: Math.floor(Date.now() / 1000) + 3600,
    },
    env.FEEDBACK_SIGNING_SECRET,
  );
  const response = await worker.fetch(
    new Request("https://example.com/api/action", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token, action: "confirm", fund_nav: "0.92" }),
    }),
    env,
  );
  assert.equal(response.status, 200);
  const states = JSON.parse(await env.STATE_KV.get("fund_state"));
  assert.equal(states["000000"].total_shares_bought, 1);
  assert.equal(states["000000"].anchor_etf_price, 0.95);
  assert.equal(states["000000"].pending_signal, null);
});

test("skip keeps holdings unchanged and lowers next alert", async () => {
  const env = await envWithState(structuredClone(pendingState));
  const token = await tokenFor(
    {
      v: 1,
      sid: "signal-1",
      code: "000000",
      name: "测试基金",
      price: 0.95,
      drop: -5,
      trigger: 4,
      exp: Math.floor(Date.now() / 1000) + 3600,
    },
    env.FEEDBACK_SIGNING_SECRET,
  );
  const response = await worker.fetch(
    new Request("https://example.com/api/action", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ token, action: "skip" }),
    }),
    env,
  );
  assert.equal(response.status, 200);
  const states = JSON.parse(await env.STATE_KV.get("fund_state"));
  assert.equal(states["000000"].total_shares_bought, 0);
  assert.equal(states["000000"].skip_buy_below_price, 0.912);
  assert.equal(states["000000"].pending_signal, null);
});

test("sell confirmation reduces holdings only after confirmation", async () => {
  const state = {
    "000000": {
      anchor_etf_price: 1,
      buy_history: [
        { date: "2026-01-01", etf_price: 1, share_no: 1 },
        { date: "2026-02-01", etf_price: 0.96, share_no: 2 },
      ],
      sell_history: [],
      total_shares_bought: 2,
      total_shares_sold: 0,
      sell_watch_high_etf_price: 1.2,
      pending_sell_signal: {
        id: "sell-signal-1",
        etf_price: 1.15,
        average_entry_etf_price: 0.98,
        high_etf_price: 1.2,
        profit_pct: 17.35,
        drawdown_pct: -4.17,
        suggested_shares: 2,
      },
    },
  };
  const env = await envWithState(state);
  const token = await tokenFor(
    {
      v: 1,
      kind: "sell",
      sid: "sell-signal-1",
      code: "000000",
      name: "测试基金",
      price: 1.15,
      drop: -4.17,
      profit: 17.35,
      shares: 2,
      trigger: 4,
      exp: Math.floor(Date.now() / 1000) + 3600,
    },
    env.FEEDBACK_SIGNING_SECRET,
  );
  const response = await worker.fetch(
    new Request("https://example.com/api/action", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        token,
        action: "confirm",
        shares: "1",
        fund_nav: "1.10",
      }),
    }),
    env,
  );
  assert.equal(response.status, 200);
  const states = JSON.parse(await env.STATE_KV.get("fund_state"));
  assert.equal(states["000000"].total_shares_sold, 1);
  assert.equal(states["000000"].pending_sell_signal, null);
  assert.equal(states["000000"].sell_history.at(-1).shares, 1);
});
