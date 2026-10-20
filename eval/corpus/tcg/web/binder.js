/* Card Binder — front end.
 *
 * Vanilla JS, no build step. One fetch of /api/binder gives everything; the
 * page renders a 9-pocket binder (two pages side by side on wide screens) and
 * a detail dialog per card. All text is inserted with textContent via `el()`,
 * so card names/notes can never inject HTML.
 */
"use strict";

const POCKETS_PER_PAGE = 9;

const VERDICT = {
  buy:  { label: "BUY",  icon: "+", cls: "v-buy" },
  hold: { label: "HOLD", icon: "‖", cls: "v-hold" },
  sell: { label: "SELL", icon: "↗", cls: "v-sell" },
};
const CONFIDENCE_DOTS = { high: 3, medium: 2, low: 1 };
const SOURCE_LABELS = {
  mock: "sample (mock)", pokemontcg: "TCGplayer via pokemontcg.io", justtcg: "JustTCG",
  tcgapi: "tcgapi.net", real: "tcgapi.net",
};
const VARIANT_LABELS = {
  holofoil: "Holofoil", normal: "Normal", unlimitedHolofoil: "Unlimited Holofoil", unlimited: "Unlimited",
  reverseHolofoil: "Reverse Holofoil", "1stEditionHolofoil": "1st Edition Holofoil", "1stEditionNormal": "1st Edition",
};
const sourceLabel = (s) => SOURCE_LABELS[s] || s;
const TREND_TEXT = { up: "↑ Up", down: "↓ Down", flat: "→ Flat", unknown: "Not enough data" };
const FILTERS = [
  { id: "all", label: "All cards" },
  { id: "owned", label: "Collection" },
  { id: "watching", label: "Watchlist" },
];

const state = {
  data: null,        // the /api/binder payload
  filter: readPref("binder.filter", "all"),
  spread: 0,         // which page (or pair of pages) is showing
  order: [],         // cards in the current filter, in binder order
  openIndex: -1,     // index into `order` of the card in the detail view
};

// ---------- tiny helpers ----------

const $ = (sel) => document.querySelector(sel);

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : String(child));   // strings -> text nodes
  }
  return node;
}

/** Like node.replaceChildren(), but skips null/false children (the native
 *  method would insert them as the text "null"). */
function setChildren(node, ...children) {
  node.replaceChildren(...children.flat().filter((c) => c != null && c !== false));
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
  return node;
}

function readPref(key, fallback) {
  try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
}
function writePref(key, value) {
  try { localStorage.setItem(key, value); } catch { /* storage blocked: fine */ }
}

const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const usd0 = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const money = (v, whole = false) => (v == null ? "—" : (whole ? usd0 : usd).format(v));
const signedMoney = (v) => (v == null ? "—" : `${v >= 0 ? "+" : "−"}${usd.format(Math.abs(v))}`);
const pct = (v) => (v == null ? "—" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(1)}%`);
const tone = (v) => (v == null ? "" : v >= 0 ? "pos" : "neg");

/** "2026-10-03" is a calendar date (parse as local); anything else is a timestamp. */
function parseDate(s) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  return m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(s);
}
const shortDate = (s) => parseDate(s).toLocaleDateString("en-US", { month: "short", day: "numeric" });
const longDate = (s) => parseDate(s).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });

const baseName = (name) => name.replace(/\s*\b\d{1,3}\/\d{1,3}\b/, "").trim();
/** "Mew ex 232/091" -> "232/091"; "Charizard 4" or "Zeraora VSTAR GG01" -> the last word. */
function cardNumber(name) {
  const slash = name.match(/\b\d{1,3}\/\d{1,3}\b/);
  if (slash) return slash[0];
  const last = name.trim().split(/\s+/).pop();
  return name.includes(" ") && /\d/.test(last) ? last : "";
}

/** pokemontcg.io serves a high-res variant next to every small image. */
function largeImage(url) {
  return url && url.startsWith("https://images.pokemontcg.io/") ? url.replace(/\.png$/, "_hires.png") : url;
}

/** Only link to real http(s) hosts (mock listings use dotless fake hosts). */
function safeHref(url) {
  try {
    const u = new URL(url);
    return /^https?:$/.test(u.protocol) && u.hostname.includes(".") ? u.href : null;
  } catch { return null; }
}

function toast(message, isError = false) {
  const t = $("#toast");
  t.textContent = message;
  t.classList.toggle("error", isError);
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 3500);
}

// ---------- data ----------

async function load() {
  const res = await fetch("/api/binder");
  if (!res.ok) throw new Error(`Server returned ${res.status}`);
  state.data = await res.json();
  render();
  openFromHash();
}

async function runAgent() {
  const btn = $("#run-btn");
  btn.disabled = true;
  btn.textContent = "Running…";
  try {
    // No ?source=: the server uses the "Market data" setting.
    const res = await fetch("/api/run", {
      method: "POST",
      headers: { "X-Binder": "1" },   // required by the server's CSRF guard
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || res.statusText);
    state.data = body;
    render();
    toast("Agent run complete: recommendations refreshed.");
  } catch (err) {
    toast(`Run failed: ${err.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "Re-run agent";
  }
}

function filteredCards() {
  const cards = state.data.cards;
  const owned = cards.filter((c) => c.status === "owned");
  const watching = cards.filter((c) => c.status === "watching");
  if (state.filter === "owned") return owned;
  if (state.filter === "watching") return watching;
  return [...owned, ...watching];   // collection first, like a real binder
}

// ---------- page ----------

function render() {
  renderHeader();
  renderBriefing();
  renderAlertBadge();
  renderSummary();
  renderTabs();
  renderBinder();
}

// ---------- agent: briefing, alerts, track record ----------

const ALERT_ICONS = { price_move: "↕", verdict_change: "◆", target_hit: "◎", dip: "↓" };

function whenText(iso) {
  if (!iso) return "—";
  const d = parseDate(iso);
  const today = new Date();
  const tomorrow = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
  const time = d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  if (d.toDateString() === today.toDateString()) return `today ${time}`;
  if (d.toDateString() === tomorrow.toDateString()) return `tomorrow ${time}`;
  return `${shortDate(iso)} ${time}`;
}

function renderBriefing() {
  const agent = state.data.agent;
  const box = $("#briefing");
  if (!agent) { box.hidden = true; return; }
  const b = agent.briefing;
  const s = agent.schedule;
  const month = agent.track_record?.["30d"] || {};
  const week = agent.track_record?.["7d"] || {};
  const record = month.hit_rate != null
    ? `${month.hit_rate}% of 30-day calls right (${month.right}/${month.right + month.wrong})`
    : week.hit_rate != null
      ? `${week.hit_rate}% of 7-day calls right (${week.right}/${week.right + week.wrong})`
      : `grading starts after 7 days (${(week.pending || 0)} calls waiting)`;

  const statusItems = [
    s.auto_run ? `Runs daily at ${s.run_time} · next ${whenText(s.next_run)}` : "Automatic runs are off",
    agent.tools ? "LLM can look things up" : "LLM decides from basic facts",
    `Track record: ${record}`,
    lastRunText(agent.last_run),
  ].filter(Boolean);
  box.hidden = false;
  setChildren(box,
    el("div", { class: "briefing-head" },
      el("span", { class: "briefing-badge", "aria-hidden": "true" }, "AI"),
      el("h2", {}, "Agent briefing"),
      b?.decided_by ? el("span", { class: "muted small" }, b.decided_by === "llm" ? "written by the LLM" : "rule-based") : null,
      el("button", { class: "link-btn", type: "button", onclick: openTrackRecord }, "Track record →")),
    el("p", { class: "briefing-summary" },
      b?.summary || "No briefing yet. Run the agent to get one."),
    b?.highlights?.length ? el("ul", { class: "briefing-list" }, b.highlights.map((h) => el("li", {}, h))) : null,
    el("p", { class: "briefing-status" }, statusItems.join("  ·  "),
      s.last_error ? el("span", { class: "neg" }, `  ·  Last automatic run failed: ${s.last_error}`) : null));
}

// The run ledger's newest run, in one line ("Last run: 7/7 cards").
function lastRunText(r) {
  if (!r) return null;
  if (r.status === "in_progress") return `Last run stopped at ${r.cards_done}/${r.cards_total} cards; it resumes on the next run`;
  if (r.status === "failed" && r.error) return `Last run failed: ${r.error}`;
  const retried = r.failed_cards?.some((c) => c.attempts > 1) ? "retried; " : "";
  const failed = r.failed_cards?.length
    ? ` · couldn't price ${r.failed_cards.map((c) => c.name).join(", ")} (${retried}will try again next run)`
    : "";
  return `Last run: ${r.cards_done}/${r.cards_total} cards${failed}`;
}

function renderAlertBadge() {
  const n = state.data.agent?.unread_alerts || 0;
  const badge = $("#alerts-badge");
  badge.hidden = n === 0;
  badge.textContent = n > 9 ? "9+" : String(n);
  $("#alerts-btn").setAttribute("aria-label", n ? `Alerts, ${n} unread` : "Alerts");
}

async function openAlerts() {
  const dialog = $("#alerts");
  const list = state.data.agent?.alerts || [];
  dialog.replaceChildren(el("div", { class: "side-body" },
    dialogHeader("Alerts", "What the agent noticed, newest first", "alerts"),
    el("div", { class: "side-content" },
      list.length
        ? el("ul", { class: "alert-list" }, list.map((a) => el("li", { class: a.read_at ? "" : "unread" },
          el("span", { class: "alert-icon", "aria-hidden": "true" }, ALERT_ICONS[a.kind] || "•"),
          el("div", {},
            el("p", { class: "alert-msg" }, a.message),
            el("p", { class: "muted small" }, `${whenText(a.created_at)}${a.sent_at ? " · sent to webhook" : ""}`)),
          a.card_id ? el("button", { class: "link-btn", type: "button",
            onclick: () => { dialog.close(); location.hash = `#card-${a.card_id}`; } }, "Open") : null)))
        : el("p", { class: "muted" },
          "No alerts yet. After each run the agent alerts you to big price moves, verdict changes, "
          + "targets reached and dips below the 30-day average."))));
  if (!dialog.open) dialog.showModal();
  if (state.data.agent?.unread_alerts) {
    try {
      await fetch("/api/alerts/read", { method: "POST", headers: { "X-Binder": "1" } });
      state.data.agent.unread_alerts = 0;
      list.forEach((a) => { a.read_at = a.read_at || new Date().toISOString(); });
      renderAlertBadge();
    } catch { /* stays unread; harmless */ }
  }
}

async function openTrackRecord() {
  const dialog = $("#track");
  dialog.replaceChildren(el("p", { class: "settings-loading" }, "Grading past calls…"));
  if (!dialog.open) dialog.showModal();
  let record;
  try {
    const res = await fetch("/api/track-record");
    record = await res.json();
  } catch (err) {
    dialog.replaceChildren(el("p", { class: "settings-loading" }, `Couldn't load: ${err.message}`));
    return;
  }

  const rate = (v) => (v.hit_rate == null ? "—" : `${v.hit_rate}%`);
  const table = (title, groups) => el("div", {},
    el("h4", {}, title),
    el("table", { class: "data-table" },
      el("thead", {}, el("tr", {}, el("th", {}, ""), el("th", {}, "Right"), el("th", {}, "Wrong"),
        el("th", {}, "Neutral"), el("th", {}, "Hit rate"))),
      el("tbody", {}, Object.entries(groups).map(([k, v]) => el("tr", {},
        el("td", {}, k.toUpperCase()), el("td", {}, v.right), el("td", {}, v.wrong),
        el("td", {}, v.neutral), el("td", {}, rate(v)))))));

  const horizon = (key, label) => {
    const h = record[key];
    if (!h || !h.graded) {
      return el("section", { class: "panel" }, el("h3", {}, label),
        el("p", { class: "muted" }, `Nothing graded yet${h?.pending ? ` (${h.pending} calls waiting)` : ""}.`));
    }
    return el("section", { class: "panel" },
      el("h3", {}, label),
      el("p", { class: "track-headline" }, el("strong", {}, rate(h)), ` right · ${h.right} right, ${h.wrong} wrong, `
        + `${h.neutral} neutral · ${h.pending} waiting`),
      el("div", { class: "track-grid" },
        table("By verdict", h.by_verdict), table("LLM vs rules", h.by_decided_by),
        table("By confidence", h.by_confidence)),
      h.recent.length ? el("details", { class: "table-view" }, el("summary", {}, "Recent graded calls"),
        el("table", { class: "data-table" },
          el("thead", {}, el("tr", {}, el("th", {}, "Card"), el("th", {}, "Call"), el("th", {}, "Then"),
            el("th", {}, "Later"), el("th", {}, "Change"), el("th", {}, "Result"))),
          el("tbody", {}, h.recent.map((g) => el("tr", {},
            el("td", {}, `${baseName(g.card)} · ${shortDate(g.made_at)}`),
            el("td", {}, `${g.verdict.toUpperCase()} (${g.decided_by})`),
            el("td", {}, money(g.price)), el("td", {}, money(g.later)),
            el("td", { class: tone(g.change_pct) }, pct(g.change_pct)),
            el("td", {}, g.outcome)))))) : null);
  };

  dialog.replaceChildren(el("div", { class: "side-body" },
    dialogHeader("Track record", "How the agent's past calls turned out", "track"),
    el("div", { class: "side-content" },
      el("p", { class: "muted small" },
        "Each call is checked against the price 7 and 30 days later. Buy is right if the price rose 5%+, "
        + "sell is right if it fell 5%+, hold is right if it stayed within ±10%. Smaller moves count as neutral."),
      horizon("7d", "After 7 days"), horizon("30d", "After 30 days"))));
}

function researchPanel(d) {
  if (!d?.research?.length) return null;
  return el("section", { class: "panel" },
    el("h3", {}, "How the agent decided"),
    el("p", { class: "muted small" }, `It looked up ${d.research.length} thing${d.research.length === 1 ? "" : "s"} before deciding:`),
    el("ol", { class: "research-list" }, d.research.map((r) => el("li", {}, r.summary))));
}

function renderHeader() {
  const { generated_at: at, source } = state.data;
  $("#last-run").textContent = at
    ? `Last run ${longDate(at)}, ${parseDate(at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })} · ${sourceLabel(source)} prices`
    : "Not run yet";
  const notice = $("#notice");
  notice.hidden = Boolean(at);
  if (!at) {
    notice.replaceChildren("No recommendations yet. Press ", el("strong", {}, "Re-run agent"),
      " or run ", el("code", {}, "python -m src.demo"), " for sample data with a month of history.");
  }
}

function tile(label, value, sub, valueClass = "") {
  return el("div", { class: "tile" },
    el("div", { class: "tile-label" }, label),
    el("div", { class: `tile-value ${valueClass}`, title: value }, value),
    el("div", { class: "tile-sub", title: sub }, sub));
}

function renderSummary() {
  const s = state.data.summary;
  const cards = state.data.cards;
  const watching = cards.filter((c) => c.status === "watching");
  const buys = watching.filter((c) => c.decision?.verdict === "buy").length;
  const sells = cards.filter((c) => c.decision?.verdict === "sell").length;
  $("#summary").replaceChildren(
    tile("Collection value", money(s.total_value), `${s.card_count} owned card${s.card_count === 1 ? "" : "s"}, at sold prices`),
    // No cost recorded for any card: "+$0.00" would read as break-even, so
    // show a dash and say what's missing. All-pulled cards ($0 cost): the
    // whole value is gain and a percentage means nothing.
    !s.cards_with_pnl
      ? tile("Unrealized P&L", "—", "Record what you paid (or that you pulled it) to see P&L")
      : s.total_pnl_pct == null
        ? tile("Unrealized P&L", signedMoney(s.total_pnl), "all value is gain (pulled from packs)", tone(s.total_pnl))
        : tile("Unrealized P&L", signedMoney(s.total_pnl), `${pct(s.total_pnl_pct)} on ${money(s.total_cost)} paid`,
          tone(s.total_pnl)),
    tile("Best performer", s.best ? pct(s.best.unrealized_pnl_pct) : "—",
      s.best ? baseName(s.best.name)
        : s.cards_with_pnl ? "Only for cards with a cost"
          : s.card_count ? "Needs purchase prices" : "No owned cards yet",
      s.best ? tone(s.best.unrealized_pnl_pct) : ""),
    tile("Signals", `${buys} buy · ${sells} sell`, `${watching.length} on the watchlist`),
  );
}

function renderTabs() {
  const counts = {
    all: state.data.cards.length,
    owned: state.data.cards.filter((c) => c.status === "owned").length,
    watching: state.data.cards.filter((c) => c.status === "watching").length,
  };
  $("#tabs").replaceChildren(...FILTERS.map((f) =>
    el("button", {
      class: "tab", type: "button", role: "tab",
      "aria-selected": String(state.filter === f.id),
      onclick: () => { state.filter = f.id; state.spread = 0; writePref("binder.filter", f.id); render(); },
    }, f.label, el("span", { class: "count" }, counts[f.id]))));
}

const pagesPerView = () => (window.matchMedia("(min-width: 980px)").matches ? 2 : 1);

function renderBinder() {
  const cards = filteredCards();
  state.order = cards;
  const perView = pagesPerView();
  const pageCount = Math.max(1, Math.ceil(cards.length / POCKETS_PER_PAGE));
  const views = Math.ceil(pageCount / perView);
  state.spread = Math.min(state.spread, views - 1);

  const binder = $("#binder");
  binder.classList.toggle("spread", perView === 2);
  const nodes = [];
  for (let i = 0; i < perView; i++) {
    const pageIndex = state.spread * perView + i;
    nodes.push(renderPage(cards, pageIndex));
  }
  // Rings go between the two pages of a spread, or down the left edge of one page.
  if (perView === 2) nodes.splice(1, 0, spine());
  else nodes.unshift(spine());
  binder.replaceChildren(...nodes);

  // A spread always shows two pages (the right one may be blank, like a real binder).
  const first = state.spread * perView + 1;
  const totalPages = views * perView;
  $("#page-label").textContent = perView === 2
    ? `Pages ${first}–${first + 1} of ${totalPages}`
    : `Page ${first} of ${totalPages}`;
  $("#prev-page").disabled = state.spread === 0;
  $("#next-page").disabled = state.spread >= views - 1;
}

function spine() {
  return el("div", { class: "spine", "aria-hidden": "true" },
    el("span", { class: "ring" }), el("span", { class: "ring" }), el("span", { class: "ring" }));
}

function renderPage(cards, pageIndex) {
  const page = el("section", { class: "page", "aria-label": `Binder page ${pageIndex + 1}` });
  for (let slot = 0; slot < POCKETS_PER_PAGE; slot++) {
    const index = pageIndex * POCKETS_PER_PAGE + slot;
    page.append(cards[index]
      ? pocket(cards[index], index)
      : el("div", { class: "pocket empty", "aria-hidden": "true" },
        // Invisible caption keeps empty rows the same height as filled ones.
        el("div", { class: "sleeve" }), el("div", { class: "caption" }, el("span", {}, " "), el("span", {}, " "))));
  }
  page.append(el("span", { class: "page-no" }, pageIndex + 1));
  return page;
}

function pocket(card, index) {
  const d = card.decision;
  const label = `${card.name}, ${card.grade}, ${card.status === "owned" ? "in collection" : "watching"}`
    + (d ? `, recommendation ${VERDICT[d.verdict].label}` : "") + ". Open details.";
  return el("button", {
    class: `pocket ${card.status}`, type: "button", "aria-label": label,
    "data-index": index, onclick: () => openDetail(index),
  },
    el("div", { class: "sleeve" }, cardArt(card, "small"), d ? sticker(d.verdict) : null),
    el("div", { class: "caption", "aria-hidden": "true" },
      el("span", { class: "cap-name", title: card.name }, baseName(card.name)),
      el("span", { class: "cap-meta" },
        el("span", {}, `${card.grade} · ${money(d?.sold_avg, true)}`),
        captionMetric(card))));
}

/** Owned: P&L %. Watching: how far the cheapest listing is from market. */
function captionMetric(card) {
  const d = card.decision;
  if (!d) return el("span", { class: "muted" }, "no data");
  if (card.status === "owned") {
    if (d.unrealized_pnl_pct != null) return el("span", { class: tone(d.unrealized_pnl_pct) }, pct(d.unrealized_pnl_pct));
    // Pulled for $0: show the gain in dollars instead of an undefined percentage.
    if (d.unrealized_pnl != null) return el("span", { class: tone(d.unrealized_pnl) }, signedMoney(d.unrealized_pnl));
    return el("span", { class: "muted" }, "—");
  }
  return el("span", { title: "Cheapest listing vs sold average" },
    d.vs_market_pct == null ? "—" : `${pct(d.vs_market_pct)} mkt`);
}

function sticker(verdict, big = false) {
  const v = VERDICT[verdict];
  return el("span", { class: `sticker ${v.cls}${big ? " big" : ""}` },
    el("span", { class: "ico", "aria-hidden": "true" }, v.icon), v.label);
}

function cardArt(card, size) {
  const placeholder = () => el("div", { class: "art placeholder" },
    el("strong", {}, baseName(card.name)), el("span", {}, cardNumber(card.name)), el("span", {}, card.set_code));
  if (!card.image_url) return placeholder();

  const img = el("img", {
    class: "art", src: card.image_url, alt: `${card.name} card art`,
    loading: size === "small" ? "lazy" : null, decoding: "async",
  });
  img.addEventListener("error", () => img.replaceWith(placeholder()), { once: true });
  // In the detail view, show the small image instantly, then swap in high-res.
  const hi = largeImage(card.image_url);
  if (size === "large" && hi !== card.image_url) {
    const loader = new Image();
    loader.onload = () => { img.src = hi; };
    loader.src = hi;
  }
  return img;
}

// ---------- detail dialog ----------

function openDetail(index) {
  const card = state.order[index];
  if (!card) return;
  state.openIndex = index;
  const dialog = $("#detail");
  dialog.replaceChildren(renderDetail(card, index));
  if (!dialog.open) dialog.showModal();
  dialog.scrollTop = 0;
  history.replaceState(null, "", `#card-${card.id}`);
  loadChart(card, state.chartRange || "30d");   // needs layout, so after showModal
}

function closeDetail() {
  $("#detail").close();
}

function openFromHash() {
  const m = /^#card-(\d+)$/.exec(location.hash);
  if (!m) return;
  let index = state.order.findIndex((c) => c.id === +m[1]);
  if (index === -1) {             // card isn't in the current filter: show all
    state.filter = "all";
    render();
    index = state.order.findIndex((c) => c.id === +m[1]);
  }
  if (index !== -1) openDetail(index);
}

function renderDetail(card, index) {
  const d = card.decision;
  const owned = card.status === "owned";
  const navButton = (symbol, target, label) => el("button", {
    class: "icon-btn nav", type: "button", "aria-label": label,
    disabled: target < 0 || target >= state.order.length, onclick: () => openDetail(target),
  }, symbol);

  return el("article", { class: "detail" },
    el("header", { class: "detail-head" },
      el("div", {},
        el("p", { class: "eyebrow" },
          `${owned ? "In your collection" : "On your watchlist"} · Set ${card.set_code} · #${cardNumber(card.name) || "—"}`),
        el("h2", { id: "detail-title" }, baseName(card.name)),
        el("p", { class: "grade-line" },
          el("span", { class: "chip" }, card.grade),
          card.variant ? el("span", { class: "chip" }, VARIANT_LABELS[card.variant] || card.variant) : null,
          card.notes ? el("span", {}, card.notes) : null)),
      el("div", { class: "head-actions" },
        el("button", { class: "btn btn-sm", type: "button", onclick: () => openEditForm(index) }, "Edit"),
        navButton("‹", index - 1, "Previous card"),
        navButton("›", index + 1, "Next card"),
        el("button", { class: "icon-btn", type: "button", "aria-label": "Close", onclick: closeDetail }, "✕"))),
    el("div", { class: "detail-body" },
      el("div", { class: "detail-art" }, holo(cardArt(card, "large"))),
      el("div", { class: "detail-info" },
        verdictPanel(d),
        researchPanel(d),
        marketStats(card),
        owned ? ownedPanel(card) : watchPanel(card),
        chartPanel(card),
        evidencePanel(card),
        manualPricePanel(card),
        removePanel(card),
        el("p", { class: "detail-foot" },
          state.data.generated_at
            ? `Prices: ${sourceLabel(state.data.source)}, agent run ${longDate(state.data.generated_at)}. Decision support only, not investment advice.`
            : "Decision support only, not investment advice."))));
}

function holo(art) {
  const wrap = el("div", { class: "holo" }, art, el("div", { class: "shine", "aria-hidden": "true" }));
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return wrap;
  wrap.addEventListener("pointermove", (e) => {
    const r = wrap.getBoundingClientRect();
    const px = (e.clientX - r.left) / r.width;
    const py = (e.clientY - r.top) / r.height;
    wrap.style.setProperty("--rx", `${(0.5 - py) * 14}deg`);
    wrap.style.setProperty("--ry", `${(px - 0.5) * 18}deg`);
    wrap.style.setProperty("--mx", `${px * 100}%`);
    wrap.style.setProperty("--my", `${py * 100}%`);
    wrap.classList.add("active");
  });
  wrap.addEventListener("pointerleave", () => {
    wrap.classList.remove("active");
    for (const p of ["--rx", "--ry", "--mx", "--my"]) wrap.style.removeProperty(p);
  });
  return wrap;
}

function verdictPanel(d) {
  if (!d) {
    return el("section", { class: "panel verdict-panel" },
      el("h3", {}, "Recommendation"),
      el("p", { class: "reason" }, "No recommendation yet. Re-run the agent to analyse this card."));
  }
  const v = VERDICT[d.verdict];
  const dots = CONFIDENCE_DOTS[d.confidence];
  return el("section", { class: `panel verdict-panel ${v.cls}` },
    el("div", { class: "verdict-top" },
      sticker(d.verdict, true),
      el("span", { class: "confidence" },
        el("span", { class: "dots", "aria-hidden": "true" },
          [1, 2, 3].map((i) => el("span", { class: `dot${i <= dots ? " on" : ""}` }))),
        `${d.confidence[0].toUpperCase()}${d.confidence.slice(1)} confidence`)),
    el("p", { class: "reason" }, d.reason),
    el("p", { class: "decided" }, d.decided_by === "llm"
      ? "Reasoning written by the LLM from the computed numbers below."
      : "Rule-based judgment (no LLM key configured). Numbers are computed, not generated."));
}

function stat(label, value, sub, valueClass = "") {
  return el("div", { class: "stat" },
    el("div", { class: "stat-label" }, label),
    el("div", { class: `stat-value ${valueClass}` }, value),
    sub ? el("div", { class: "stat-sub" }, sub) : null);
}

function marketStats(card) {
  const d = card.decision;
  const m = card.market;
  if (!d) return null;
  const vs = d.vs_market_pct;
  const n = d.sold_count;
  const salesText = d.sold_avg == null ? "no price available"
    : n == null ? "market price, sales count not reported"
      : `${n} recent sale${n === 1 ? "" : "s"}${n <= 2 ? " · thin data" : ""}`;
  const listingText = !m ? null
    : m.listings.length === 1 ? m.listings[0].condition.replace(/^Raw · /, "")
      : m.listings.length ? `${m.listings.length} listings seen`
        : d.sold_avg != null ? "not reported by this source" : "none found";
  return el("div", { class: "market-block" },
    el("section", { class: "stats", "aria-label": "Market numbers" },
      m?.manual
        ? stat("Value (your price)", money(d.sold_avg), "no market price available")
        : stat("Sold average", money(d.sold_avg), salesText),
      stat("Cheapest listing", money(d.best_listing), listingText),
      stat("vs market", pct(vs), vs == null ? null : vs < 0 ? "listing under sold avg" : "listing over sold avg"),
      stat("30-day trend", TREND_TEXT[d.trend_30d], "of the sold price")),
    m?.source_note ? el("p", { class: "source-note" }, m.source_note) : null,
    m?.by_condition?.length ? conditionTable(m.by_condition, card) : null,
    m?.product_url && safeHref(m.product_url)
      ? el("a", { class: "check-link", href: safeHref(m.product_url), target: "_blank", rel: "noopener noreferrer" },
        "Check this price on TCGplayer ↗")
      : null);
}

/** The same card's price in every condition (JustTCG), your copy's row highlighted. */
function conditionTable(rows, card) {
  const mine = card.grade === "Raw" ? (card.condition || "Near Mint") : null;
  const printings = [...new Set(rows.map((r) => r.printing))];
  return el("details", { class: "table-view", open: true },
    el("summary", {}, "Prices by condition"),
    el("table", { class: "data-table" },
      el("thead", {}, el("tr", {}, el("th", {}, "Condition"),
        printings.length > 1 ? el("th", {}, "Printing") : null,
        el("th", {}, "Price"), el("th", {}, "30 days"))),
      el("tbody", {}, rows.map((r) => el("tr", { class: r.condition === mine ? "mine" : null },
        el("td", {}, r.condition, r.condition === mine ? el("span", { class: "muted" }, " (yours)") : null),
        printings.length > 1 ? el("td", {}, r.printing) : null,
        el("td", {}, money(r.price)),
        el("td", { class: tone(r.change_30d) }, r.change_30d == null ? "—" : pct(r.change_30d)))))));
}

function ownedPanel(card) {
  const d = card.decision;
  const children = [el("h3", {}, "Your copy")];
  const how = card.acquired || (card.purchase_price != null ? "bought" : null);
  const costLabel = how === "pulled" ? "Pack cost" : how === "gift" ? "Value given" : "Paid";
  const when = card.purchase_date ? longDate(card.purchase_date) : null;
  const costSub = how === "pulled" ? `pulled from a pack${when ? ` on ${when}` : ""}`
    : how === "gift" ? `gift / trade${when ? ` on ${when}` : ""}`
      : when ? `on ${when}` : card.purchase_price == null ? "not recorded yet: use Edit" : null;
  const pnlSub = d?.unrealized_pnl_pct != null ? pct(d.unrealized_pnl_pct)
    : d?.unrealized_pnl != null ? "the whole value is gain" : null;
  children.push(el("div", { class: "stats" },
    stat(costLabel, money(card.purchase_price), costSub),
    stat("Worth now", money(d?.sold_avg), "at the market price"),
    stat("Unrealized P&L", signedMoney(d?.unrealized_pnl), pnlSub, tone(d?.unrealized_pnl))));
  if (card.target_sell != null) {
    const gap = d?.sold_avg == null ? null : d.sold_avg - card.target_sell;
    children.push(el("p", { class: "target-line" }, "Sell target ", el("strong", {}, money(card.target_sell)),
      gap == null ? "" : gap >= 0 ? `: reached, ${money(gap)} above.` : `: ${money(-gap)} to go.`));
  }
  return el("section", { class: "panel" }, children);
}

function watchPanel(card) {
  const d = card.decision;
  if (card.target_buy == null && card.target_sell == null) return null;
  const lines = [];
  if (card.target_buy != null) {
    const gap = d?.best_listing == null ? null : card.target_buy - d.best_listing;
    lines.push(el("p", { class: "target-line" }, "Buy target ", el("strong", {}, money(card.target_buy)),
      gap == null ? "" : gap >= 0
        ? `: cheapest listing is ${money(gap)} under it.`
        : `: cheapest listing is ${money(-gap)} above it.`));
  }
  if (card.target_sell != null) {
    lines.push(el("p", { class: "target-line" }, "Planned sell target ", el("strong", {}, money(card.target_sell))));
  }
  return el("section", { class: "panel" }, el("h3", {}, "Your targets"), lines);
}

const CHART_RANGES = ["7d", "30d", "90d", "1y"];

function chartPanel(card) {
  return el("section", { class: "panel chart-panel" },
    el("div", { class: "chart-head" },
      el("h3", {}, "Price history"),
      el("div", { class: "range-buttons", role: "group", "aria-label": "Time range" },
        CHART_RANGES.map((r) => el("button", {
          class: "range-btn", type: "button", "data-range": r,
          "aria-pressed": String(r === (state.chartRange || "30d")),
          onclick: () => loadChart(card, r),
        }, r.toUpperCase())))),
    el("div", { class: "legend" }),
    el("div", { class: "chart" }),
    el("p", { class: "chart-source muted small" }),
    el("div", { class: "chart-table" }));
}

/** Fetch the price history for a range (server caches it) and draw it. */
async function loadChart(card, range) {
  state.chartRange = range;
  const panel = $("#detail").querySelector(".chart-panel");
  if (!panel) return;
  panel.querySelectorAll("[data-range]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.range === range)));
  panel.querySelector(".chart").replaceChildren(el("p", { class: "chart-empty" }, "Loading price history…"));
  let data = null;
  try {
    const res = await fetch(`/api/cards/${card.id}/price-history?range=${range}`);
    if (res.ok) data = await res.json();
  } catch { /* drawn as "no history" below */ }
  // The user may have closed the card or moved on while this loaded.
  if (!$("#detail").open || state.order[state.openIndex]?.id !== card.id) return;
  state.chart = { card, data };
  renderChart(panel, card, data);
}

function renderChart(panel, card, data) {
  const points = (data?.points || []).map((x) => ({ t: x.t, p: x.p, b: x.b ?? null }));
  const paid = card.status === "owned" && card.purchase_price > 0;
  const hasListings = points.some((x) => x.b != null);
  setChildren(panel.querySelector(".legend"),
    el("span", {}, el("i", { class: "swatch s1" }), data?.source === "justtcg" ? "Market price" : "Sold average"),
    hasListings ? el("span", {}, el("i", { class: "swatch s2" }), "Cheapest listing") : null,
    paid ? el("span", {}, el("i", { class: "swatch ref" }), "What you paid") : null);
  drawChart(panel.querySelector(".chart"), card, points, data?.source);
  panel.querySelector(".chart-source").textContent = data
    ? (data.source === "justtcg"
      ? `${data.label}. Full history from JustTCG, not just our runs.`
      : "From the agent's own daily snapshots. Real history from day one needs the JustTCG source.")
    : "";
  panel.querySelector(".chart-table").replaceChildren(historyTable(points) || "");
}

function historyTable(points) {
  if (!points.length) return null;
  const hasListings = points.some((x) => x.b != null);
  return el("details", { class: "table-view" },
    el("summary", {}, "Show as table"),
    el("table", { class: "data-table" },
      el("thead", {}, el("tr", {}, el("th", {}, "Date"), el("th", {}, "Price"),
        hasListings ? el("th", {}, "Cheapest listing") : null)),
      el("tbody", {}, [...points].reverse().map((x) => el("tr", {},
        el("td", {}, longDate(x.t)), el("td", {}, money(x.p)),
        hasListings ? el("td", {}, money(x.b)) : null)))));
}

function evidencePanel(card) {
  const m = card.market;
  if (!m) return null;
  const sales = [...m.sold_comps].sort((a, b) => b.date.localeCompare(a.date));
  const listings = [...m.listings].sort((a, b) => a.price - b.price);
  return el("section", { class: "evidence" },
    el("div", { class: "panel" },
      el("h3", {}, "Recent sales"),
      sales.length
        ? el("ul", {}, sales.map((s) => el("li", {}, el("span", { class: "muted" }, longDate(s.date)), el("strong", {}, money(s.price)))))
        : el("p", { class: "muted" }, card.decision?.sold_count == null && card.decision?.sold_avg != null
          ? "This source reports an aggregated market price, not individual sales."
          : "No matching sales found.")),
    el("div", { class: "panel" },
      el("h3", {}, "Cheapest listings"),
      listings.length
        ? el("ul", {}, listings.map((l) => {
          const href = safeHref(l.url);
          return el("li", {},
            el("span", { class: "muted" }, l.condition || card.grade,
              href ? [" · ", el("a", { href, target: "_blank", rel: "noopener noreferrer" }, "view")] : null),
            el("strong", {}, money(l.price)));
        }))
        : el("p", { class: "muted" }, m.sold_avg != null && m.best_listing == null
          ? "This price source doesn't report listings." : "No current listings found.")));
}

/** Your own price: a fallback the agent uses only when no market source has one. */
function manualPricePanel(card) {
  const hasMarket = card.market && card.market.sold_avg != null && !card.market.manual;
  const input = el("input", {
    id: "manual-price", name: "price", type: "number", inputmode: "decimal", min: 0, step: "0.01",
    value: card.manual_price ?? "", placeholder: "e.g. 25.00", "aria-describedby": "manual-price-help",
  });
  const error = el("p", { class: "field-error", role: "alert" });
  const save = el("button", { class: "btn btn-sm", type: "submit" }, "Save price");
  const clear = card.manual_price != null
    ? el("button", { class: "link-btn", type: "button", onclick: () => saveManualPrice(card, null, save, error) },
      "Clear")
    : null;

  const status = card.manual_price != null
    ? `${money(card.manual_price)}, set on ${longDate(card.manual_price_date)}.`
    : "Not set.";
  const usage = hasMarket
    ? " The market source has a price for this card, so yours is only kept as a backup."
    : " The agent uses it because no market source has a price for this card.";

  const form = el("form", { class: "manual-price", novalidate: true },
    el("div", { class: "with-unit money" }, el("span", { class: "unit" }, "$"), input),
    save, clear);
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    saveManualPrice(card, input.value.trim(), save, error);
  });

  return el("section", { class: "panel" },
    el("h3", {}, "Your price"),
    el("p", { class: "target-line", id: "manual-price-help" },
      el("strong", {}, status), card.manual_price != null ? usage : " Set one if no market source can price this card (e.g. a brand-new set)."),
    form, error);
}

async function saveManualPrice(card, value, button, error) {
  button.disabled = true;
  error.textContent = "";
  try {
    const res = await fetch(`/api/cards/${card.id}/price`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Binder": "1" },
      body: JSON.stringify({ price: value === "" ? null : value }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || res.statusText);
    state.data = body.binder;
    render();
    // Reopen the same card with its refreshed recommendation.
    const index = state.order.findIndex((c) => c.id === card.id);
    if (index !== -1) openDetail(index);
    toast(value == null || value === "" ? "Your price was cleared." : "Price saved and recommendation updated.");
  } catch (err) {
    error.textContent = err.message;
  } finally {
    button.disabled = false;
  }
}

/** "Remove" with a two-step confirm, because it also deletes the card's history. */
function removePanel(card) {
  const where = card.status === "owned" ? "collection" : "watchlist";
  const panel = el("section", { class: "remove-panel" });

  const showButton = () => panel.replaceChildren(
    el("button", { class: "btn btn-danger-ghost", type: "button", onclick: showConfirm },
      `Remove from ${where}`));

  const showConfirm = () => {
    const days = card.history.length;
    const cancel = el("button", { class: "btn", type: "button", onclick: showButton }, "Cancel");
    panel.replaceChildren(
      el("p", { class: "remove-warning" },
        `Remove ${card.name} (${card.grade}) from your ${where}? `,
        days ? `Its ${days} saved price snapshot${days === 1 ? "" : "s"} will be deleted too. ` : "",
        "This can't be undone."),
      el("div", { class: "remove-actions" },
        el("button", { class: "btn btn-danger", type: "button", onclick: (e) => removeCard(card, e.currentTarget) },
          "Remove card"),
        cancel));
    cancel.focus();   // the safe choice gets focus
  };

  showButton();
  return panel;
}

async function removeCard(card, button) {
  button.disabled = true;
  button.textContent = "Removing…";
  try {
    const res = await fetch(`/api/cards/${card.id}`, {
      method: "DELETE",
      headers: { "X-Binder": "1" },   // required by the server's CSRF guard
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || res.statusText);
    state.data = body;
    closeDetail();
    render();
    toast(`Removed ${card.name} from your ${card.status === "owned" ? "collection" : "watchlist"}.`);
  } catch (err) {
    button.disabled = false;
    button.textContent = "Remove card";
    toast(`Couldn't remove the card: ${err.message}`, true);
  }
}

// ---------- price chart (hand-rolled SVG) ----------

function niceTicks(lo, hi, count) {
  const raw = (hi - lo) / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((s) => s * mag).find((s) => s >= raw) || raw;
  const ticks = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) ticks.push(+v.toFixed(2));
  return ticks;
}

function drawChart(box, card, points, source) {
  if (!box) return;
  box.replaceChildren();
  const paid = card.status === "owned" && card.purchase_price > 0 ? card.purchase_price : null;
  const pts = points
    .map((x) => ({ t: parseDate(x.t).getTime(), sold: x.p, best: x.b, at: x.t }))
    .filter((p) => p.sold != null || p.best != null);

  if (pts.length < 2) {
    box.append(el("p", { class: "chart-empty" }, source === "justtcg"
      ? "Not enough price history for this range yet. Try a longer range."
      : "The trend line appears after 2+ daily runs. Switch Market data to JustTCG in Settings "
        + "for full history right away."));
    return;
  }

  const series = [
    { key: "sold", label: source === "justtcg" ? "Market" : "Sold avg", cls: "s1" },
    { key: "best", label: "Cheapest", cls: "s2" },
  ];
  const W = Math.max(280, box.clientWidth);
  const H = 230;
  const m = { t: 12, r: 108, b: 26, l: 54 };
  const iw = W - m.l - m.r;
  const ih = H - m.t - m.b;

  const values = pts.flatMap((p) => [p.sold, p.best]).filter((v) => v != null);
  if (paid != null) values.push(paid);
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  const pad = (hi - lo) * 0.12 || hi * 0.05 || 1;
  lo = Math.max(0, lo - pad);
  hi += pad;
  const ticks = niceTicks(lo, hi, 4);

  const t0 = pts[0].t;
  const t1 = pts[pts.length - 1].t;
  const x = (t) => m.l + ((t - t0) / (t1 - t0 || 1)) * iw;
  const y = (v) => m.t + (1 - (v - lo) / (hi - lo)) * ih;

  const root = svg("svg", {
    width: W, height: H, viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `Price history for ${card.name}: sold average and cheapest listing over the last 30 days`,
  });

  // Recessive grid + y labels (one shared $ axis).
  for (const v of ticks) {
    root.append(svg("line", { class: "grid", x1: m.l, x2: m.l + iw, y1: y(v), y2: y(v) }));
    const label = svg("text", { x: m.l - 8, y: y(v) + 4, "text-anchor": "end" });
    label.textContent = money(v, true);
    root.append(label);
  }
  root.append(svg("line", { class: "baseline", x1: m.l, x2: m.l + iw, y1: m.t + ih, y2: m.t + ih }));

  // A few date labels along the bottom.
  const labelIdx = [...new Set([0, Math.round((pts.length - 1) / 3), Math.round((2 * (pts.length - 1)) / 3), pts.length - 1])];
  for (const i of labelIdx) {
    const anchor = i === 0 ? "start" : i === pts.length - 1 ? "end" : "middle";
    const label = svg("text", { x: x(pts[i].t), y: H - 6, "text-anchor": anchor });
    label.textContent = shortDate(pts[i].at);
    root.append(label);
  }

  // What you paid, as a dashed reference line.
  if (paid != null) {
    root.append(svg("line", { class: "ref", x1: m.l, x2: m.l + iw, y1: y(paid), y2: y(paid) }));
  }

  // Series lines, end dots and direct end labels (nudged apart if they collide).
  const ends = [];
  for (const s of series) {
    const sp = pts.filter((p) => p[s.key] != null);
    if (!sp.length) continue;
    const d = sp.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p[s.key]).toFixed(1)}`).join("");
    root.append(svg("path", { class: `line ${s.cls}`, d }));
    const last = sp[sp.length - 1];
    root.append(svg("circle", { class: `end-dot ${s.cls}`, cx: x(last.t), cy: y(last[s.key]), r: 4 }));
    ends.push({ y: y(last[s.key]), text: `${s.label} ${money(last[s.key], true)}` });
  }
  if (paid != null) ends.push({ y: y(paid), text: `Paid ${money(paid, true)}` });
  ends.sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) {
    if (ends[i].y - ends[i - 1].y < 14) ends[i].y = ends[i - 1].y + 14;
  }
  for (const e of ends) {
    const label = svg("text", { class: "end-label", x: m.l + iw + 10, y: e.y + 4 });
    label.textContent = e.text;
    root.append(label);
  }

  // Hover layer: crosshair + per-series dots + a tooltip, on a hit area bigger than the lines.
  const cross = svg("line", { class: "crosshair", y1: m.t, y2: m.t + ih, visibility: "hidden" });
  const hoverDots = series.map((s) => svg("circle", { class: `end-dot ${s.cls}`, r: 5, visibility: "hidden" }));
  const hit = svg("rect", { x: m.l, y: 0, width: iw, height: H, fill: "transparent" });
  root.append(cross, ...hoverDots, hit);

  const tip = el("div", { class: "chart-tip", hidden: true });
  box.append(root, tip);

  hit.addEventListener("pointermove", (e) => {
    const rect = root.getBoundingClientRect();
    const px = (e.clientX - rect.left) * (W / rect.width);
    const t = t0 + ((px - m.l) / iw) * (t1 - t0);
    let p = pts[0];
    for (const q of pts) if (Math.abs(q.t - t) < Math.abs(p.t - t)) p = q;

    const cx = x(p.t);
    cross.setAttribute("x1", cx);
    cross.setAttribute("x2", cx);
    cross.setAttribute("visibility", "visible");
    series.forEach((s, i) => {
      const v = p[s.key];
      hoverDots[i].setAttribute("visibility", v == null ? "hidden" : "visible");
      if (v != null) { hoverDots[i].setAttribute("cx", cx); hoverDots[i].setAttribute("cy", y(v)); }
    });

    setChildren(tip,
      el("div", { class: "tip-date" }, longDate(p.at)),
      el("div", { class: "tip-row" }, el("i", { class: "swatch s1" }), "Price", el("b", {}, money(p.sold))),
      p.best != null
        ? el("div", { class: "tip-row" }, el("i", { class: "swatch s2" }), "Cheapest", el("b", {}, money(p.best)))
        : null);
    tip.hidden = false;
    const left = (cx / W) * rect.width;
    const flip = left > rect.width * 0.6;
    tip.style.left = `${flip ? left - tip.offsetWidth - 12 : left + 12}px`;
    tip.style.top = `${m.t}px`;
  });
  hit.addEventListener("pointerleave", () => {
    cross.setAttribute("visibility", "hidden");
    hoverDots.forEach((dot) => dot.setAttribute("visibility", "hidden"));
    tip.hidden = true;
  });
}

// ---------- settings ----------
// The form is generated from GET /api/settings, which comes from the one
// settings registry in src/config.py. Saved API keys are never sent to the
// page: we only get "is it set" and its last four characters.

const SETTINGS_GROUPS = ["API keys", "AI model", "Agent runs", "Agent", "Decision rules", "Live data"];
const GROUP_NOTES = {
  "API keys": "Saved to the .env file in your project folder. A saved key is never shown again, only its last four characters.",
  "AI model": "Any OpenAI-compatible provider works. Change the URL and model to switch.",
  "Decision rules": "Applied by both the built-in rules and the LLM prompt. Re-run the agent to update existing recommendations.",
  "Live data": "Only used with real market data.",
  "Agent": "How the agent works on its own: when it runs, how much it may look up, and how it tells you about changes.",
};
const RESETTABLE_GROUPS = new Set(["AI model", "Agent runs", "Agent", "Decision rules", "Live data"]);
const CHOICE_LABELS = {
  mock: "Mock: offline sample data",
  pokemontcg: "pokemontcg.io: free TCGplayer prices (raw cards)",
  justtcg: "JustTCG: Near Mint prices incl. new sets (needs a JustTCG key)",
  tcgapi: "tcgapi.net: eBay sold comps (needs a TCG API key)",
  // Labels for values whose meaning depends on the setting ("on" means different things).
  "RUN_LLM:auto": "Auto: when an LLM key is set", "RUN_LLM:on": "Always (fail if no key)",
  "RUN_LLM:off": "Never: built-in rules only",
  on: "On", off: "Off",
};
const choiceLabel = (key, value) => CHOICE_LABELS[`${key}:${value}`] || CHOICE_LABELS[value] || value;
const TEST_TARGET = {
  LLM_API_KEY: "llm", TCGAPI_KEY: "tcgapi", POKEMONTCG_API_KEY: "pokemontcg", JUSTTCG_API_KEY: "justtcg",
  ALERT_WEBHOOK_URL: "webhook",
};
const DEEPSEEK = { base: "https://api.deepseek.com", model: "deepseek-chat" };

const settingsState = { items: [], removeKeys: new Set() };

async function openSettings() {
  const dialog = $("#settings");
  dialog.replaceChildren(el("p", { class: "settings-loading" }, "Loading settings…"));
  if (!dialog.open) dialog.showModal();
  try {
    const res = await fetch("/api/settings");
    if (!res.ok) throw new Error(`Server returned ${res.status}`);
    settingsState.items = (await res.json()).settings;
    settingsState.removeKeys = new Set();
    dialog.replaceChildren(renderSettings());
  } catch (err) {
    dialog.replaceChildren(el("p", { class: "settings-loading" }, `Couldn't load settings: ${err.message}`));
  }
}

const settingItem = (key) => settingsState.items.find((s) => s.key === key);
const fieldInput = (form, key) => form.querySelector(`[name="${key}"]`);

function renderSettings() {
  const form = el("form", { class: "settings", novalidate: true });
  form.addEventListener("submit", (e) => { e.preventDefault(); saveSettings(form); });

  const groups = SETTINGS_GROUPS.map((group) => {
    const items = settingsState.items.filter((s) => s.group === group);
    if (!items.length) return null;
    return el("fieldset", { class: "settings-group" },
      el("legend", {}, group),
      GROUP_NOTES[group] ? el("p", { class: "group-note" }, GROUP_NOTES[group]) : null,
      group === "AI model" ? providerField(form) : null,
      items.map((item) => settingField(item, form)),
      RESETTABLE_GROUPS.has(group)
        ? el("button", { class: "link-btn", type: "button", onclick: () => restoreDefaults(form, items) },
          "Restore defaults")
        : null);
  });

  form.append(
    el("header", { class: "settings-head" },
      el("div", {},
        el("h2", { id: "settings-title" }, "Settings"),
        el("p", { class: "eyebrow" }, "API keys, AI model and agent rules")),
      el("button", { class: "icon-btn", type: "button", "aria-label": "Close settings",
        onclick: () => $("#settings").close() }, "✕")),
    el("div", { class: "settings-body" }, groups),
    el("footer", { class: "settings-foot" },
      el("span", { class: "settings-status", role: "status" }),
      el("button", { class: "btn", type: "button", onclick: () => $("#settings").close() }, "Cancel"),
      el("button", { class: "btn btn-primary", type: "submit" }, "Save settings")));
  return form;
}

/** Not a stored setting: a shortcut that fills in the base URL and model. */
function providerField(form) {
  const base = settingItem("LLM_BASE_URL")?.value;
  const isDeepSeek = base === DEEPSEEK.base;
  const select = el("select", {
    id: "provider", onchange: () => {
      if (select.value === "deepseek") {
        fieldInput(form, "LLM_BASE_URL").value = DEEPSEEK.base;
        fieldInput(form, "LLM_MODEL").value = DEEPSEEK.model;
      } else {
        fieldInput(form, "LLM_BASE_URL").focus();
      }
    },
  },
    el("option", { value: "deepseek", selected: isDeepSeek }, "DeepSeek"),
    el("option", { value: "custom", selected: !isDeepSeek }, "Other OpenAI-compatible provider"));
  return el("div", { class: "field" },
    el("label", { for: "provider" }, "Provider"),
    el("div", { class: "control" }, select));
}

function settingField(item, form) {
  const id = `set-${item.key}`;
  const describedBy = `${id}-help ${id}-err`;
  let control;

  if (item.kind === "secret" || item.kind === "secret_url") {
    control = secretControl(item, id, describedBy, form);
  } else if (item.kind === "time") {
    control = el("input", { id, name: item.key, type: "time", value: item.value ?? "", "aria-describedby": describedBy });
  } else if (item.kind === "choice") {
    control = el("select", { id, name: item.key, "aria-describedby": describedBy },
      item.choices.map((c) => el("option", { value: c, selected: c === item.value }, choiceLabel(item.key, c))));
  } else if (item.kind === "number") {
    control = el("div", { class: "with-unit" },
      el("input", {
        id, name: item.key, type: "number", inputmode: "decimal", value: item.value,
        min: item.min, max: item.max, step: item.max > 100 ? 1 : 0.5, "aria-describedby": describedBy,
      }),
      item.unit ? el("span", { class: "unit" }, item.unit) : null);
  } else {
    control = el("input", {
      id, name: item.key, type: item.kind === "url" ? "url" : "text", value: item.value ?? "",
      spellcheck: "false", autocomplete: "off", "aria-describedby": describedBy,
    });
  }

  const defaultHint = !item.kind.startsWith("secret") && item.default != null && item.default !== 0
    ? ` Default: ${item.kind === "choice" ? item.default : `${item.default}${item.unit || ""}`}.` : "";
  return el("div", { class: "field", "data-key": item.key },
    el("label", { for: id }, item.label),
    el("div", { class: "control" }, control,
      el("p", { class: "help", id: `${id}-help` }, item.help + defaultHint),
      el("p", { class: "field-error", id: `${id}-err`, role: "alert" })));
}

function secretControl(item, id, describedBy, form) {
  const input = el("input", {
    id, name: item.key, type: "password", autocomplete: "off", spellcheck: "false",
    placeholder: item.kind === "secret_url"
      ? (item.is_set ? "Paste a new URL to replace the saved one" : "https://discord.com/api/webhooks/…")
      : (item.is_set ? "Paste a new key to replace the saved one" : "Paste your key"),
    "aria-describedby": `${id}-status ${describedBy}`,
  });
  const status = el("span", { class: "key-status", id: `${id}-status` });
  const result = el("p", { class: "test-result", role: "status" });

  const paintStatus = () => {
    const removing = settingsState.removeKeys.has(item.key);
    status.className = `key-status ${removing ? "removing" : item.is_set ? "set" : "unset"}`;
    status.replaceChildren(removing
      ? "Will be removed when you save"
      : item.is_set ? `Saved · ${item.hint}` : "Not set");
    removeBtn.textContent = removing ? "Undo remove" : "Remove";
    removeBtn.hidden = !item.is_set;
  };
  const removeBtn = el("button", {
    class: "link-btn danger", type: "button",
    onclick: () => {
      const keys = settingsState.removeKeys;
      keys.has(item.key) ? keys.delete(item.key) : keys.add(item.key);
      paintStatus();
    },
  });
  const toggle = el("button", {
    class: "btn btn-sm", type: "button", "aria-label": `Show ${item.label}`, "aria-pressed": "false",
    onclick: () => {
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      toggle.textContent = show ? "Hide" : "Show";
      toggle.setAttribute("aria-pressed", String(show));
    },
  }, "Show");
  const testBtn = el("button", {
    class: "btn btn-sm", type: "button",
    onclick: () => testConnection(item.key, form, testBtn, result),
  }, item.kind === "secret_url" ? "Send test" : "Test");
  testBtn.hidden = !TEST_TARGET[item.key];

  paintStatus();
  return el("div", { class: "secret" },
    el("div", { class: "secret-row" }, input, toggle, testBtn),
    el("div", { class: "secret-meta" }, status, removeBtn),
    result);
}

function restoreDefaults(form, items) {
  for (const item of items) {
    if (item.kind.startsWith("secret")) continue;
    fieldInput(form, item.key).value = item.default ?? "";
  }
  const provider = form.querySelector("#provider");
  if (provider && items.some((i) => i.key === "LLM_BASE_URL")) provider.value = "deepseek";
}

/** Only send what changed; blank means "back to the default". */
function collectUpdates(form) {
  const updates = {};
  for (const item of settingsState.items) {
    const input = fieldInput(form, item.key);
    if (item.kind.startsWith("secret")) {
      if (settingsState.removeKeys.has(item.key)) updates[item.key] = null;
      else if (input.value.trim()) updates[item.key] = input.value.trim();
      continue;
    }
    const value = input.value.trim();
    const same = (a, b) => (item.kind === "number" ? Number(a) === Number(b) : String(a ?? "") === String(b ?? ""));
    if (same(value, item.value)) continue;
    // Back to the default (or blank) -> remove the line, so .env only holds real overrides.
    updates[item.key] = value === "" || same(value, item.default) ? null : value;
  }
  return updates;
}

function showFieldErrors(form, errors) {
  form.querySelectorAll(".field").forEach((f) => f.classList.remove("invalid"));
  form.querySelectorAll(".field-error").forEach((p) => { p.textContent = ""; });
  let first = null;
  for (const [key, message] of Object.entries(errors)) {
    const field = form.querySelector(`.field[data-key="${key}"]`);
    if (!field) continue;
    field.classList.add("invalid");
    field.querySelector(".field-error").textContent = message;
    first ??= fieldInput(form, key);
  }
  first?.focus();
}

async function saveSettings(form) {
  const updates = collectUpdates(form);
  const status = form.querySelector(".settings-status");
  if (!Object.keys(updates).length) {
    $("#settings").close();
    toast("No changes to save.");
    return;
  }
  const submit = form.querySelector('button[type="submit"]');
  submit.disabled = true;
  status.textContent = "Saving…";
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Binder": "1" },
      body: JSON.stringify({ updates }),
    });
    const body = await res.json();
    if (!res.ok) {
      showFieldErrors(form, body.errors || {});
      status.textContent = body.errors ? "Fix the highlighted fields." : body.error;
      return;
    }
    settingsState.items = body.settings;
    $("#settings").close();
    const affectsResults = Object.keys(updates).some((k) => settingItem(k)?.group !== "API keys");
    toast(affectsResults ? "Settings saved. Re-run the agent to apply them." : "Settings saved.");
  } catch (err) {
    status.textContent = `Couldn't save: ${err.message}`;
  } finally {
    submit.disabled = false;
  }
}

async function testConnection(key, form, button, result) {
  // Test with what's typed now, so a key can be checked before it's saved.
  const overrides = {};
  const typed = fieldInput(form, key).value.trim();
  if (typed) overrides[key] = typed;
  if (key === "LLM_API_KEY") {
    overrides.LLM_BASE_URL = fieldInput(form, "LLM_BASE_URL").value.trim();
    overrides.LLM_MODEL = fieldInput(form, "LLM_MODEL").value.trim();
  }
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Testing…";
  result.className = "test-result";
  result.textContent = "";
  try {
    const res = await fetch("/api/settings/test", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Binder": "1" },
      body: JSON.stringify({ target: TEST_TARGET[key], overrides }),
    });
    const body = await res.json();
    result.className = `test-result ${body.ok ? "ok" : "fail"}`;
    result.textContent = `${body.ok ? "✓" : "✕"} ${body.message || body.error}`;
  } catch (err) {
    result.className = "test-result fail";
    result.textContent = `✕ ${err.message}`;
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

// ---------- add card (catalog search) ----------
// Search pokemontcg.io through our server (GET /api/search), pick a printing,
// fill in a short form, POST /api/cards. The server re-reads the card from the
// catalog by id, so name/number/set/art always come from the catalog itself.

const GRADES = ["Raw", "PSA 10", "PSA 9", "PSA 8", "BGS 10", "BGS 9.5", "CGC 10", "CGC 9.5"];
const CONDITIONS = ["Near Mint", "Lightly Played", "Moderately Played", "Heavily Played", "Damaged"];

/** Grade select plus, for raw cards, the copy's condition (prices depend on it). */
function gradeFields(prefix, grade, condition) {
  const gradeSelect = el("select", { id: `${prefix}-grade`, name: "grade" },
    [...new Set([...GRADES, grade])].map((g) => el("option", { value: g, selected: g === grade },
      g === "Raw" ? "Raw (ungraded)" : g)));
  const conditionSelect = el("select", { id: `${prefix}-condition`, name: "condition" },
    CONDITIONS.map((c) => el("option", { value: c, selected: c === (condition || "Near Mint") }, c)));
  const conditionField = formField("condition", "Condition", conditionSelect,
    "Lightly Played and below sell for less; JustTCG prices each condition.", prefix);
  conditionField.hidden = grade !== "Raw";
  gradeSelect.addEventListener("change", () => { conditionField.hidden = gradeSelect.value !== "Raw"; });
  return [formField("grade", "Grade", gradeSelect, "Free price sources cover raw (ungraded) cards.", prefix),
    conditionField];
}
const addState = { query: "", results: [], controller: null, timer: null };

function dialogHeader(title, subtitle, dialogId) {
  return el("header", { class: "settings-head" },
    el("div", {},
      el("h2", { id: `${dialogId}-title` }, title),
      el("p", { class: "eyebrow" }, subtitle)),
    el("button", { class: "icon-btn", type: "button", "aria-label": "Close",
      onclick: () => $(`#${dialogId}`).close() }, "✕"));
}

function openAddCard() {
  const dialog = $("#add");
  dialog.replaceChildren(renderSearchStep());
  if (!dialog.open) dialog.showModal();
  const input = dialog.querySelector("#card-search");
  input.focus();
  input.select();
}

function renderSearchStep() {
  const status = el("p", { class: "search-status", id: "search-status", role: "status" },
    "Search the pokemontcg.io catalog by name, collector number, or both.");
  const results = el("div", { class: "search-results", id: "search-results" });
  const input = el("input", {
    id: "card-search", type: "search", value: addState.query, autocomplete: "off", spellcheck: "false",
    placeholder: "Try “charizard ex”, “pikachu 58” or “199/165”",
    "aria-label": "Search cards", "aria-describedby": "search-status", "aria-controls": "search-results",
  });
  input.addEventListener("input", () => {
    clearTimeout(addState.timer);
    addState.timer = setTimeout(() => runSearch(input.value, status, results), 350);   // debounce typing
  });
  input.addEventListener("keydown", (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    clearTimeout(addState.timer);
    runSearch(input.value, status, results);
  });
  // Coming back from the form: show the previous results again, instantly.
  if (addState.query && addState.results.length) {
    paintResults(results, addState.results);
    status.textContent = resultsSummary(addState.results);
  }
  return el("div", { class: "add-card" },
    dialogHeader("Add a card", "Find it in the catalog, then choose where it goes", "add"),
    el("div", { class: "add-body" }, el("div", { class: "search-box" }, input), status, results));
}

function resultsSummary(results) {
  if (!results.length) return "No cards found. Try fewer words, or just the Pokémon's name.";
  return results.length >= 30
    ? "Showing the 30 newest printings. Add a collector number (like “58”) to narrow it down."
    : `${results.length} printing${results.length === 1 ? "" : "s"} found, newest first.`;
}

async function runSearch(text, status, box) {
  text = text.trim();
  addState.query = text;
  if (text.length < 2) {
    status.textContent = "Type at least 2 characters.";
    box.replaceChildren();
    return;
  }
  addState.controller?.abort();                // a newer search replaces an older one
  const controller = (addState.controller = new AbortController());
  status.textContent = "Searching pokemontcg.io…";
  box.setAttribute("aria-busy", "true");
  try {
    const res = await fetch(`/api/search?q=${encodeURIComponent(text)}`, { signal: controller.signal });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || res.statusText);
    addState.results = body.results;
    status.textContent = resultsSummary(body.results);
    paintResults(box, body.results);
  } catch (err) {
    if (err.name === "AbortError") return;
    status.replaceChildren(`${err.message} `,
      el("button", { class: "link-btn", type: "button", onclick: () => runSearch(text, status, box) }, "Try again"));
  } finally {
    if (addState.controller === controller) box.removeAttribute("aria-busy");
  }
}

function priceLine(result) {
  const priced = result.finishes.filter((f) => f.market != null);
  if (!priced.length) return "No price yet";
  return priced.slice(0, 2).map((f) => `${f.label} ${money(f.market)}`).join(" · ")
    + (priced.length > 2 ? ` +${priced.length - 2}` : "");
}

function paintResults(box, results) {
  box.replaceChildren(...results.map((r) => {
    const year = (r.set.release_date || "").slice(0, 4);
    const where = r.in_binder.some((x) => x.status === "owned") ? "In your collection"
      : r.in_binder.length ? "On your watchlist" : null;
    return el("button", {
      class: "result", type: "button", onclick: () => showAddForm(r),
      "aria-label": `${r.display_name}, ${r.set.name} ${year}${r.rarity ? `, ${r.rarity}` : ""}, ${priceLine(r)}${where ? `, ${where}` : ""}`,
    },
      el("div", { class: "result-art" },
        r.image_small ? el("img", { src: r.image_small, alt: "", loading: "lazy", decoding: "async" })
          : el("div", { class: "art placeholder" }, el("strong", {}, r.name))),
      where ? el("span", { class: "chip in-binder" }, where) : null,
      el("div", { class: "result-info", "aria-hidden": "true" },
        el("strong", { class: "result-name" }, r.display_name),
        el("span", { class: "muted" }, `${r.set.name}${year ? ` · ${year}` : ""}`),
        r.rarity ? el("span", { class: "muted" }, r.rarity) : null,
        el("span", { class: "result-price" }, priceLine(r))));
  }));
}

function formField(key, label, control, help, prefix = "add") {
  return el("div", { class: "field", "data-key": key },
    el("label", { for: `${prefix}-${key}` }, label),
    el("div", { class: "control" }, control,
      help ? el("p", { class: "help" }, help) : null,
      el("p", { class: "field-error", role: "alert" })));
}

function moneyInput(key, placeholder, prefix = "add", value = "") {
  return el("div", { class: "with-unit money" },
    el("span", { class: "unit" }, "$"),
    el("input", { id: `${prefix}-${key}`, name: key, type: "number", inputmode: "decimal",
      min: 0, step: "0.01", placeholder, value: value ?? "" }));
}

// How an owned card was acquired. For pulled cards the "price" is the pack it
// came from (blank = $0), and the whole value counts as gain.
const ACQUIRED_LABELS = { bought: "Bought it", pulled: "Pulled from a pack", gift: "Gift / trade" };

function acquiredFields(prefix, acquired, price, dateValue, today) {
  const choice = el("div", { class: "segmented", role: "radiogroup", "aria-label": "How you got it" },
    Object.entries(ACQUIRED_LABELS).map(([value, label]) => el("label", {},
      el("input", { type: "radio", name: "acquired", value, checked: value === acquired }),
      el("span", {}, label))));
  return el("div", {},
    formField("acquired", "How you got it", choice, null, prefix),
    formField("purchase_price", "What you paid", moneyInput("purchase_price", "e.g. 12.50", prefix, price),
      "Used for profit / loss.", prefix),
    formField("purchase_date", "Purchase date", el("input", {
      id: `${prefix}-purchase_date`, name: "purchase_date", type: "date", max: today, value: dateValue }),
      null, prefix));
}

/** Relabel the price/date fields to match how the card was acquired. */
function paintAcquired(form, prefix) {
  const how = form.querySelector('input[name="acquired"]:checked')?.value || "bought";
  const price = form.querySelector('.field[data-key="purchase_price"]');
  const date = form.querySelector('.field[data-key="purchase_date"]');
  if (!price || !date) return;
  const text = {
    bought: ["What you paid", "Used for profit / loss and sell signals.", "e.g. 12.50", "Purchase date"],
    pulled: ["Pack cost (optional)", "What the pack cost, if you want to count it. Blank = $0, so the whole value is gain.", "0.00", "Date pulled"],
    gift: ["Value given up (optional)", "For a trade, what you gave; blank = $0.", "0.00", "Date received"],
  }[how];
  price.querySelector("label").textContent = text[0];
  price.querySelector(".help").textContent = text[1];
  price.querySelector("input").placeholder = text[2];
  date.querySelector("label").textContent = text[3];
}

/** Edit card: replaces the detail view's body with a form for the editable fields. */
function renderEditForm(card, index) {
  const today = new Date().toLocaleDateString("en-CA");
  const owned = card.status === "owned";
  const P = "edit";
  const statusChoice = el("div", { class: "segmented", role: "radiogroup", "aria-label": "Where it is" },
    ["owned", "watching"].map((value) => el("label", {},
      el("input", { type: "radio", name: "status", value, checked: value === card.status }),
      el("span", {}, value === "owned" ? "My collection" : "Watchlist"))));
  const finish = el("select", { id: `${P}-variant`, name: "variant" },
    el("option", { value: "", selected: !card.variant }, "Auto (regular printing)"),
    Object.entries(VARIANT_LABELS).map(([key, label]) =>
      el("option", { value: key, selected: key === card.variant }, label)));

  const form = el("form", { id: "edit-form", class: "edit-form", novalidate: true },
    formField("status", "Where it is", statusChoice, null, P),
    formField("variant", "Finish", finish, "Which printing you have. Finishes sell for different prices.", P),
    gradeFields(P, card.grade, card.condition),
    el("div", { class: "owned-only", hidden: !owned },
      acquiredFields(P, card.acquired || (card.purchase_price != null ? "bought" : "pulled"),
        card.purchase_price, card.purchase_date || "", today)),
    el("div", { class: "watching-only", hidden: owned },
      formField("target_buy", "Buy target", moneyInput("target_buy", "optional", P, card.target_buy),
        "Flag it when the price reaches this.", P)),
    formField("target_sell", "Sell target", moneyInput("target_sell", "optional", P, card.target_sell), null, P),
    formField("notes", "Notes", el("input", {
      id: `${P}-notes`, name: "notes", type: "text", maxlength: 200, value: card.notes || "" }), null, P),
    el("p", { class: "settings-status", role: "status" }),
    el("div", { class: "edit-actions" },
      el("button", { class: "btn", type: "button", onclick: () => openDetail(index) }, "Cancel"),
      el("button", { class: "btn btn-primary", type: "submit" }, "Save changes")));

  form.addEventListener("change", (e) => {
    if (e.target.name === "acquired") paintAcquired(form, P);
    if (e.target.name !== "status") return;
    const isOwned = e.target.value === "owned";
    form.querySelector(".owned-only").hidden = !isOwned;
    form.querySelector(".watching-only").hidden = isOwned;
  });
  paintAcquired(form, P);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const status = form.querySelector(".settings-status");
    const submit = form.querySelector('button[type="submit"]');
    submit.disabled = true;
    status.textContent = "Saving…";
    try {
      const res = await fetch(`/api/cards/${card.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", "X-Binder": "1" },
        body: JSON.stringify(Object.fromEntries(new FormData(form))),
      });
      const body = await res.json();
      if (!res.ok) {
        showFieldErrors(form, body.errors || {});
        status.textContent = body.errors && Object.keys(body.errors).length ? "Fix the highlighted fields." : body.error;
        return;
      }
      state.data = body.binder;
      render();
      const newIndex = state.order.findIndex((c) => c.id === card.id);
      if (newIndex !== -1) openDetail(newIndex);
      else $("#detail").close();     // moved to the other tab
      toast("Saved. Recommendation updated.");
    } catch (err) {
      status.textContent = `Couldn't save: ${err.message}`;
    } finally {
      submit.disabled = false;
    }
  });

  return el("div", { class: "edit-body" },
    el("div", { class: "detail-art" }, holo(cardArt(card, "large"))),
    el("div", { class: "detail-info" },
      el("section", { class: "panel" }, el("h3", {}, "Edit card"), form)));
}

function openEditForm(index) {
  const card = state.order[index];
  const dialog = $("#detail");
  const body = dialog.querySelector(".detail-body");
  if (!card || !body) return;
  body.replaceWith(Object.assign(renderEditForm(card, index), { className: "detail-body" }));
  dialog.querySelector('#edit-form input[name="status"]:checked')?.focus();
}

function showAddForm(r) {
  const dialog = $("#add");
  const today = new Date().toLocaleDateString("en-CA");   // YYYY-MM-DD, local time
  const priced = r.finishes.filter((f) => f.market != null);

  const statusChoice = el("div", { class: "segmented", role: "radiogroup", "aria-label": "Add to" },
    ["owned", "watching"].map((value) => el("label", {},
      el("input", { type: "radio", name: "status", value, checked: value === "owned" }),
      el("span", {}, value === "owned" ? "My collection" : "Watchlist"))));

  const finish = el("select", { id: "add-variant", name: "variant", disabled: !r.finishes.length },
    r.finishes.length
      ? r.finishes.map((f, i) => el("option", { value: f.key, selected: i === 0 },
        `${f.label}${f.market != null ? ` · ${money(f.market)}` : ""}`))
      : el("option", { value: "" }, "No TCGplayer prices yet"));

  // The submit button sits in the dialog footer; form="..." ties it to the form.
  const submit = el("button", { class: "btn btn-primary", type: "submit", form: "add-form-el" },
    "Add to collection");
  const form = el("form", { id: "add-form-el", class: "add-form", novalidate: true },
    el("div", { class: "add-form-art" },
      holo(cardArt({ name: r.display_name, set_code: r.set.code, image_url: r.image_small }, "large")),
      el("p", { class: "muted" }, `${r.set.name} · ${r.set.series || ""}`),
      el("p", { class: "muted" }, priced.length
        ? `TCGplayer market: ${priced.map((f) => `${f.label} ${money(f.market)}`).join(", ")}`
        : "No TCGplayer price yet (new sets can take a while).")),
    el("div", { class: "add-form-fields" },
      el("h3", { class: "add-form-title" }, r.display_name),
      formField("status", "Add to", statusChoice),
      formField("variant", "Finish", finish,
        r.finishes.length > 1 ? "Different finishes sell for different prices." : null),
      gradeFields("add", "Raw", "Near Mint"),
      el("div", { class: "owned-only" }, acquiredFields("add", "pulled", null, today, today)),
      el("div", { class: "watching-only", hidden: true },
        formField("target_buy", "Buy target", moneyInput("target_buy", "optional"),
          "Flag it when the cheapest listing reaches this price.")),
      formField("target_sell", "Sell target", moneyInput("target_sell", "optional")),
      formField("notes", "Notes", el("input", {
        id: "add-notes", name: "notes", type: "text", maxlength: 200, placeholder: "optional" })),
      el("p", { class: "settings-status", role: "status" })),
  );

  // Collection vs watchlist shows different fields.
  form.addEventListener("change", (e) => {
    if (e.target.name === "acquired") paintAcquired(form, "add");
    if (e.target.name !== "status") return;
    const owned = e.target.value === "owned";
    form.querySelector(".owned-only").hidden = !owned;
    form.querySelector(".watching-only").hidden = owned;
    submit.textContent = owned ? "Add to collection" : "Add to watchlist";
  });
  paintAcquired(form, "add");
  form.addEventListener("submit", (e) => { e.preventDefault(); addCard(r, form, submit); });

  dialog.replaceChildren(el("div", { class: "add-card" },
    dialogHeader("Add a card", "Check the details, then add it", "add"),
    el("div", { class: "add-body" }, form),
    el("footer", { class: "settings-foot" },
      el("button", { class: "btn", type: "button", onclick: () => {
        dialog.replaceChildren(renderSearchStep());
        dialog.querySelector("#card-search").focus();
      } }, "← Back to results"),
      submit)));
  dialog.scrollTop = 0;
  form.querySelector('input[name="status"]').focus();
}

async function addCard(result, form, submit) {
  const data = Object.fromEntries(new FormData(form));
  const payload = { catalog_id: result.id, ...data, variant: data.variant || null };
  const status = form.querySelector(".settings-status");
  submit.disabled = true;
  status.textContent = "Adding…";
  try {
    const res = await fetch("/api/cards", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Binder": "1" },
      body: JSON.stringify(payload),
    });
    const body = await res.json();
    if (!res.ok) {
      showFieldErrors(form, body.errors || {});
      status.textContent = body.errors && Object.keys(body.errors).length ? "Fix the highlighted fields." : body.error;
      return;
    }
    state.data = body.binder;
    $("#add").close();
    addState.results = addState.results.map((r) => (r.id === result.id
      ? { ...r, in_binder: [...r.in_binder, { id: body.card_id, status: payload.status }] } : r));
    showNewCard(body.card_id, payload.status);
    const decision = state.data.cards.find((c) => c.id === body.card_id)?.decision;
    toast(decision
      ? `Added ${result.display_name}: ${VERDICT[decision.verdict].label} (${decision.confidence} confidence).`
      : `Added ${result.display_name}. Re-run the agent to get its first recommendation.`);
  } catch (err) {
    status.textContent = `Couldn't add the card: ${err.message}`;
  } finally {
    submit.disabled = false;
  }
}

/** Jump to the page with the new card and briefly highlight its pocket. */
function showNewCard(cardId, status) {
  state.filter = status;
  writePref("binder.filter", status);
  render();
  const index = state.order.findIndex((c) => c.id === cardId);
  if (index === -1) return;
  state.spread = Math.floor(index / (POCKETS_PER_PAGE * pagesPerView()));
  renderBinder();
  const pocketEl = document.querySelector(`.pocket[data-index="${index}"]`);
  pocketEl?.classList.add("just-added");
  pocketEl?.scrollIntoView({ block: "center", behavior: "smooth" });
  setTimeout(() => pocketEl?.classList.remove("just-added"), 2600);
}

// ---------- wiring ----------

function init() {
  $("#run-btn").addEventListener("click", runAgent);
  $("#settings-btn").addEventListener("click", openSettings);
  $("#add-btn").addEventListener("click", openAddCard);
  $("#alerts-btn").addEventListener("click", openAlerts);
  for (const id of ["#alerts", "#track"]) {
    const d = $(id);
    d.addEventListener("click", (e) => { if (e.target === d) d.close(); });
  }
  const addDialog = $("#add");
  addDialog.addEventListener("click", (e) => { if (e.target === addDialog) addDialog.close(); });
  const settingsDialog = $("#settings");
  settingsDialog.addEventListener("click", (e) => { if (e.target === settingsDialog) settingsDialog.close(); });
  $("#prev-page").addEventListener("click", () => { state.spread--; renderBinder(); });
  $("#next-page").addEventListener("click", () => { state.spread++; renderBinder(); });

  const dialog = $("#detail");
  // Click on the dimmed backdrop (the dialog element itself, outside its content) closes it.
  dialog.addEventListener("click", (e) => { if (e.target === dialog) closeDetail(); });
  dialog.addEventListener("close", () => {
    // "close" fires asynchronously; if a card was reopened meanwhile, ignore it.
    if (dialog.open) return;
    history.replaceState(null, "", location.pathname);
    document.querySelector(`.pocket[data-index="${state.openIndex}"]`)?.focus();
    state.openIndex = -1;
  });
  dialog.addEventListener("keydown", (e) => {
    if (e.target.closest("summary, a")) return;
    if (e.key === "ArrowLeft" && state.openIndex > 0) openDetail(state.openIndex - 1);
    if (e.key === "ArrowRight" && state.openIndex < state.order.length - 1) openDetail(state.openIndex + 1);
  });

  // Editing the URL's #card-N in an open tab doesn't reload the page — handle it here.
  // (openDetail/close use replaceState, which doesn't fire hashchange, so no loop.)
  window.addEventListener("hashchange", () => { if (state.data) openFromHash(); });

  // Re-layout when crossing the one-page / two-page breakpoint, and redraw the chart.
  let lastPerView = pagesPerView();
  let resizeTimer;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!state.data) return;
      if (pagesPerView() !== lastPerView) { lastPerView = pagesPerView(); state.spread = 0; renderBinder(); }
      const panel = dialog.querySelector(".chart-panel");
      if (dialog.open && panel && state.chart) renderChart(panel, state.chart.card, state.chart.data);
    }, 150);
  });

  load().catch((err) => {
    $("#notice").hidden = false;
    $("#notice").textContent = `Couldn't load the binder: ${err.message}. Is "python -m src.web" running?`;
  });
}

init();
