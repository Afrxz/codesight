"use strict";
// RAGG front end: plain JS, no build step. All text is inserted with
// textContent (never innerHTML), so code and answers can't inject markup.

const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  append(node, children);
  return node;
}
function append(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}
function setChildren(node, ...children) { node.replaceChildren(); append(node, children); }

async function api(path, options = {}) {
  const opts = { ...options, headers: { ...(options.headers || {}) } };
  if (opts.body && typeof opts.body !== "string") {
    opts.body = JSON.stringify(opts.body);
    opts.headers["Content-Type"] = "application/json";
  }
  if (opts.method && opts.method !== "GET") opts.headers["X-RAGG"] = "1";
  const res = await fetch(path, opts);
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { const j = await res.json(); detail = typeof j.detail === "string" ? j.detail : detail; } catch { /* not JSON */ }
    throw new Error(detail);
  }
  return res.json();
}

const store = {
  get(key, fallback) { try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode: fine */ } },
  del(key) { try { localStorage.removeItem(key); } catch { /* ignore */ } },
};

const state = {
  repos: [],
  selected: store.get("ragg.selected", null),
  health: null,
  chats: {},          // repo id -> [{question, answer, hits, cited, timings, model, mode, reranked, status, error}]
  busy: false,
  viewer: null,
};

// ---------------------------------------------------------------------------------
// Markdown (a small, safe subset): paragraphs, headings, lists, `code`, **bold**,
// fenced code blocks, and [n] citations as buttons.

function renderInline(text, onCite) {
  const out = [];
  const re = /(`[^`\n]+`)|(\*\*[^*\n]+\*\*)|(\[(\d{1,2})\])/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) out.push(el("code", { text: m[1].slice(1, -1) }));
    else if (m[2]) out.push(el("strong", { text: m[2].slice(2, -2) }));
    else if (m[3]) {
      const n = Number(m[4]);
      out.push(onCite ? el("button", { class: "cite", type: "button", title: `Open source [${n}]`,
        onclick: () => onCite(n) }, String(n)) : m[3]);
    }
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function renderMarkdown(text, onCite) {
  const frag = document.createDocumentFragment();
  const lines = text.replace(/\r/g, "").split("\n");
  let para = [], list = null;
  const flushPara = () => { if (para.length) { frag.append(el("p", {}, renderInline(para.join(" "), onCite))); para = []; } };
  const flushList = () => { if (list) { frag.append(list); list = null; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const fence = line.match(/^\s*```(\w*)/);
    if (fence) {
      flushPara(); flushList();
      const code = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
      frag.append(el("pre", {}, el("code", { text: code.join("\n") })));
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)/);
    const bullet = line.match(/^\s*[-*]\s+(.*)/);
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)/);
    if (heading) { flushPara(); flushList(); frag.append(el(heading[1].length <= 2 ? "h3" : "h4", {}, renderInline(heading[2], onCite))); }
    else if (bullet || numbered) {
      flushPara();
      const tag = bullet ? "ul" : "ol";
      if (!list || list.tagName.toLowerCase() !== tag) { flushList(); list = el(tag); }
      list.append(el("li", {}, renderInline((bullet || numbered)[1], onCite)));
    } else if (!line.trim()) { flushPara(); flushList(); }
    else { if (list && /^\s{2,}/.test(line)) { list.lastChild.append(" ", ...renderInline(line.trim(), onCite)); } else { flushList(); para.push(line.trim()); } }
  }
  flushPara(); flushList();
  return frag;
}

// ---------------------------------------------------------------------------------
// Health / banner / model badge

async function loadHealth() {
  try { state.health = await api("/api/health"); } catch { state.health = null; }
  const h = state.health, badge = $("#model-badge"), banner = $("#banner");
  if (!h) return;
  badge.textContent = `${h.provider_label} · ${h.model}`;
  badge.classList.toggle("warn", !h.key_set);
  if (!h.key_set) {
    setChildren(banner, `No ${h.provider_label} API key yet: answers need one. `,
      el("button", { class: "link-btn", type: "button", onclick: openSettings }, "Add it in Settings →"));
    banner.hidden = false;
  } else banner.hidden = true;
}

// ---------------------------------------------------------------------------------
// Repos

const MODEL_SHORT = { "BAAI/bge-small-en-v1.5": "bge-small", "jinaai/jina-embeddings-v2-base-code": "jina-code",
  "BAAI/bge-base-en-v1.5": "bge-base" };

async function loadRepos() {
  try { state.repos = await api("/api/repos"); } catch (e) { state.repos = []; }
  if (!state.repos.find((r) => r.id === state.selected)) state.selected = state.repos[0]?.id || null;
  renderRepos();
  renderChatHead();
  if (!chat().length) renderThread();          // the welcome panel reflects indexing state too
  if (state.repos.some((r) => r.job)) setTimeout(loadRepos, 1200);   // poll while indexing
}

function repoMeta(r) {
  if (r.job) {
    const pct = r.job.total ? Math.round((r.job.done / r.job.total) * 100) : 0;
    return [el("div", { class: "repo-meta" }, `${r.job.message || r.job.phase}${r.job.total ? ` ${r.job.done}/${r.job.total}` : ""}`),
      el("div", { class: "progress", role: "progressbar", "aria-valuenow": pct, "aria-valuemin": 0, "aria-valuemax": 100 },
        el("span", { style: `width:${r.job.total ? pct : 8}%` }))];
  }
  if (r.status === "error") return el("div", { class: "repo-meta", style: "color:var(--bad)" }, r.error || "Indexing failed");
  if (!r.stats?.files) return el("div", { class: "repo-meta" }, "Not indexed yet");
  return [el("div", { class: "repo-meta" },
    `${r.stats.files} files · ${r.stats.chunks} chunks · ${r.chunker === "ast" ? "AST" : "windows"} · ${MODEL_SHORT[r.embed_model] || r.embed_model}`),
    r.stale ? el("div", { class: "repo-meta", style: "color:var(--warn)" }, "Settings changed: re-index to apply") : null];
}

function renderRepos() {
  const list = $("#repo-list");
  if (!state.repos.length) {
    setChildren(list, el("li", { class: "empty-side" }, "No repos yet. Add a folder or a git URL to start."));
    return;
  }
  setChildren(list, state.repos.map((r) => {
    const status = r.job ? "indexing" : r.status === "error" ? "error" : r.stats?.files ? "ready" : "";
    return el("li", { class: `repo ${r.id === state.selected ? "selected" : ""}`, tabindex: 0,
      "aria-current": r.id === state.selected ? "true" : null,
      onclick: () => selectRepo(r.id),
      onkeydown: (e) => { if (e.key === "Enter") selectRepo(r.id); } },
      el("div", { class: "repo-name" }, el("span", { class: `dot ${status}`, "aria-hidden": "true" }), r.name),
      repoMeta(r),
      el("div", { class: "repo-actions" },
        el("button", { class: "link-btn", type: "button", disabled: !!r.job,
          onclick: (e) => { e.stopPropagation(); reindex(r.id); } }, r.stats?.files ? "Re-index" : "Index"),
        el("button", { class: "link-btn danger", type: "button", disabled: !!r.job,
          onclick: (e) => { e.stopPropagation(); removeRepo(r); } }, "Remove")));
  }));
}

function selectRepo(id) {
  state.selected = id;
  store.set("ragg.selected", id);
  renderRepos(); renderChatHead(); renderThread();
}

async function reindex(id) {
  try { await api(`/api/repos/${encodeURIComponent(id)}/reindex`, { method: "POST" }); } catch (e) { alert(e.message); }
  loadRepos();
}

async function removeRepo(r) {
  if (!confirm(`Remove “${r.name}” and its index? Your files are not touched.`)) return;
  try { await api(`/api/repos/${encodeURIComponent(r.id)}`, { method: "DELETE" }); } catch (e) { alert(e.message); return; }
  store.del(`ragg.chat.${r.id}`);
  delete state.chats[r.id];
  if (state.selected === r.id) state.selected = null;
  await loadRepos();
  renderThread();
}

// ---------------------------------------------------------------------------------
// Chat

const EXAMPLES = ["What does this project do, and how is it structured?", "Where is the entry point?",
  "How is configuration loaded?", "How are errors handled?", "What are the main data models?"];

function chat(id = state.selected) {
  if (!id) return [];
  if (!state.chats[id]) state.chats[id] = store.get(`ragg.chat.${id}`, []);
  return state.chats[id];
}
function saveChat(id) { store.set(`ragg.chat.${id}`, chat(id).filter((m) => m.status === "done").slice(-30)); }

function currentRepo() { return state.repos.find((r) => r.id === state.selected) || null; }

function renderChatHead() {
  const repo = currentRepo();
  $("#chat-title").textContent = repo ? `Ask about ${repo.name}` : "Ask about your code";
  $("#chat-sub").textContent = repo ? repo.source : "";
  $("#clear-chat").hidden = !repo || !chat().length;
  const ready = !!(repo && repo.stats?.files && !repo.job);
  $("#question").disabled = !ready || state.busy;
  $("#ask-btn").disabled = !ready || state.busy;
  $("#question").placeholder = !repo ? "Add a repo first (left)…" : repo.job ? "Indexing… you can ask once it's done."
    : !ready ? "Index this repo first…" : "Ask about the code… (Enter to send, Shift+Enter for a new line)";
}

function welcome() {
  const repo = currentRepo();
  if (!repo) {
    return el("div", { class: "welcome" },
      el("h2", {}, "Ask questions about any codebase"),
      el("p", {}, "RAGG reads a repository, splits it into functions and classes, and answers your questions with citations to the exact files and lines."),
      el("ol", {},
        el("li", {}, "Press ", el("strong", {}, "+ Add repo"), " and give a folder path or an https git URL."),
        el("li", {}, "Wait for indexing (a progress bar shows; it runs on this computer)."),
        el("li", {}, "Ask anything. Click a ", el("span", { class: "cite" }, "1"), " citation to see the code.")),
      el("p", { class: "small" }, "Answers are written by the LLM in Settings (DeepSeek by default), from the code found, never from guesses."));
  }
  if (!repo.stats?.files) return el("div", { class: "welcome" }, el("h2", {}, repo.job ? "Indexing…" : "Not indexed yet"),
    el("p", {}, repo.job ? "You can ask questions as soon as indexing finishes." : "Press Index in the sidebar."));
  return el("div", { class: "welcome" },
    el("h2", {}, `Ready: ${repo.stats.files} files, ${repo.stats.chunks} chunks`),
    el("p", {}, "Try one of these, or ask your own:"),
    el("div", { class: "examples" }, EXAMPLES.map((q) => el("button", { class: "chip", type: "button",
      onclick: () => { $("#question").value = q; send(); } }, q))));
}

function renderThread() {
  const thread = $("#thread");
  const msgs = chat();
  if (!msgs.length) { setChildren(thread, welcome()); renderChatHead(); return; }
  setChildren(thread, msgs.map((m, i) => renderMessage(m, i)));
  renderChatHead();
}

function updateMessage(index) {
  const thread = $("#thread");
  const node = thread.querySelector(`[data-msg="${index}"]`);
  const fresh = renderMessage(chat()[index], index);
  if (node) node.replaceWith(fresh); else thread.append(fresh);
}

function fmtMs(v) { return v == null ? "–" : v >= 1000 ? `${(v / 1000).toFixed(1)} s` : `${Math.round(v)} ms`; }

function renderMessage(m, index) {
  const onCite = (n) => { const h = m.hits?.[n - 1]; if (h) openViewer(state.selected, h); };
  const body = el("div", { class: "msg-a" });
  if (m.status === "retrieving") body.append(el("div", { class: "status-line" }, el("span", { class: "spinner" }), "Searching the code…"));
  if (m.answer) {
    const ans = el("div", { class: `answer ${m.status === "writing" ? "cursor" : ""}` });
    ans.append(renderMarkdown(m.answer, onCite));
    body.append(ans);
  } else if (m.status === "writing") body.append(el("div", { class: "status-line" }, el("span", { class: "spinner" }), "Writing the answer…"));
  if (m.error) body.append(el("p", { class: "msg-error" }, m.error));
  if (m.hits?.length) {
    const cited = new Set(m.cited || []);
    body.append(el("div", { class: "sources", "aria-label": "Sources" }, m.hits.map((h, i) =>
      el("button", { class: `source ${cited.has(i + 1) ? "cited" : ""}`, type: "button",
        title: `${h.path}:${h.start_line}-${h.end_line}${h.symbol ? " · " + h.symbol : ""}`, onclick: () => openViewer(state.selected, h) },
        el("span", { class: "n" }, `[${i + 1}]`), `${h.path.split("/").pop()}:${h.start_line}-${h.end_line}`,
        h.symbol ? el("span", { class: "sym" }, h.symbol) : null))));
    body.append(foundDetails(m));
  }
  return el("div", { class: "msg", "data-msg": index },
    el("div", { class: "msg-q" }, m.question), body);
}

function foundDetails(m) {
  const t = m.timings || {};
  const num = (v, d = 3) => v == null ? "–" : (typeof v === "number" ? (Number.isInteger(v) ? v : v.toFixed(d)) : v);
  const rows = m.hits.map((h, i) => el("tr", {},
    el("td", {}, String(i + 1)),
    el("td", { class: "mono" }, `${h.path}:${h.start_line}-${h.end_line}`, h.symbol ? el("div", { class: "muted small" }, h.symbol) : null),
    el("td", { class: "num" }, num(h.dense_rank)), el("td", { class: "num" }, num(h.sparse_rank)),
    el("td", { class: "num" }, num(h.rrf_score, 4)), el("td", { class: "num" }, num(h.rerank_score, 2))));
  const search = (t.dense_ms || 0) + (t.sparse_ms || 0);
  return el("details", { class: "found" },
    el("summary", {}, `How these were found · ${({ hybrid: "hybrid search", dense: "meaning search", sparse: "keyword search" })[m.mode] || m.mode}${m.reranked ? " + re-ranking" : ""}${m.model ? " · " + m.model : ""}`),
    el("table", { class: "found-table" },
      el("thead", {}, el("tr", {}, ["#", "Code", "Meaning rank", "Keyword rank", "RRF score", "Re-rank score"].map((h) => el("th", {}, h)))),
      el("tbody", {}, rows)),
    el("div", { class: "timings" },
      el("span", {}, "Search ", el("b", {}, fmtMs(search))),
      t.rerank_ms != null ? el("span", {}, "Re-rank ", el("b", {}, fmtMs(t.rerank_ms))) : null,
      t.first_token_ms != null ? el("span", {}, "First words ", el("b", {}, fmtMs(t.first_token_ms))) : null,
      t.overall_ms != null ? el("span", {}, "Total ", el("b", {}, fmtMs(t.overall_ms))) : null,
      m.candidates ? el("span", {}, `${m.candidates} candidates considered`) : null));
}

async function send() {
  const repo = currentRepo();
  const q = $("#question").value.trim();
  if (!repo || !q || state.busy) return;
  const msgs = chat();
  const history = msgs.filter((m) => m.status === "done" && m.answer).slice(-3).map((m) => ({ question: m.question, answer: m.answer }));
  const m = { question: q, answer: "", hits: [], cited: [], status: "retrieving" };
  msgs.push(m);
  const index = msgs.length - 1;
  $("#question").value = "";
  state.busy = true;
  renderThread();
  scrollDown();
  let pending = false;
  const repaint = () => { if (!pending) { pending = true; requestAnimationFrame(() => { pending = false; updateMessage(index); scrollDown(true); }); } };
  try {
    const res = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json", "X-RAGG": "1" },
      body: JSON.stringify({ repo_id: repo.id, question: q, history }) });
    if (!res.ok) { let d = `HTTP ${res.status}`; try { d = (await res.json()).detail || d; } catch { /* */ } throw new Error(d); }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf("\n\n")) >= 0) {
        const raw = buffer.slice(0, cut); buffer = buffer.slice(cut + 2);
        const line = raw.split("\n").find((l) => l.startsWith("data: "));
        if (!line) continue;
        const ev = JSON.parse(line.slice(6));
        if (ev.type === "retrieval") Object.assign(m, { hits: ev.hits, mode: ev.mode, reranked: ev.reranked, timings: ev.timings_ms, candidates: ev.candidates, status: "writing" });
        else if (ev.type === "token") m.answer += ev.text;
        else if (ev.type === "done") Object.assign(m, { answer: ev.answer, cited: ev.cited, model: ev.model, timings: ev.timings_ms, status: "done" });
        else if (ev.type === "error") Object.assign(m, { error: ev.message, status: "error" });
        repaint();
      }
    }
    if (m.status !== "done" && m.status !== "error") Object.assign(m, { status: "error", error: "The answer stream ended early." });
  } catch (e) {
    Object.assign(m, { status: "error", error: e.message });
  } finally {
    state.busy = false;
    updateMessage(index);
    saveChat(repo.id);
    renderChatHead();
    $("#question").focus();
  }
}

function scrollDown(onlyIfNear = false) {
  const t = $("#thread");
  if (!onlyIfNear || t.scrollHeight - t.scrollTop - t.clientHeight < 240) t.scrollTop = t.scrollHeight;
}

// ---------------------------------------------------------------------------------
// Code viewer

async function openViewer(repoId, hit, whole = false) {
  const pad = 12;
  const start = whole ? 1 : Math.max(1, hit.start_line - pad);
  const end = whole ? null : hit.end_line + pad;
  let data;
  try {
    data = await api(`/api/repos/${encodeURIComponent(repoId)}/file?path=${encodeURIComponent(hit.path)}&start=${start}${end ? `&end=${end}` : ""}`);
  } catch (e) { alert(`Couldn't open the file: ${e.message}`); return; }
  state.viewer = { repoId, hit, whole };
  $("#viewer-path").textContent = hit.path;
  $("#viewer-meta").textContent = `lines ${hit.start_line}–${hit.end_line}${hit.symbol ? " · " + hit.symbol : ""} · ${data.total_lines} lines in file`;
  $("#viewer-more").textContent = whole ? "Show only around the cited lines" : "Show whole file";
  const code = $("#viewer-code");
  setChildren(code, data.lines.map((text, i) => {
    const n = data.start + i;
    const hl = n >= hit.start_line && n <= hit.end_line;
    return el("div", { class: `ln ${hl ? "hl" : ""}`, "data-line": n }, el("span", { class: "no" }, String(n)), el("span", { class: "src" }, text || " "));
  }));
  $("#viewer").hidden = false;
  const first = code.querySelector(`[data-line="${hit.start_line}"]`);
  if (first) code.scrollTop = Math.max(0, first.offsetTop - code.offsetTop - 40);
}

// ---------------------------------------------------------------------------------
// Add repo

function openAddDialog() {
  $("#add-error").textContent = "";
  $("#add-form").reset();
  $("#add-dialog").showModal();
  $("#add-source").focus();
}

async function submitAdd(e) {
  e.preventDefault();
  const source = $("#add-source").value.trim();
  const name = $("#add-name").value.trim() || null;
  $("#add-error").textContent = "";
  try {
    const res = await api("/api/repos", { method: "POST", body: { source, name } });
    $("#add-dialog").close();
    state.selected = res.repo.id;
    store.set("ragg.selected", res.repo.id);
    await loadRepos();
    renderThread();
  } catch (err) { $("#add-error").textContent = err.message; }
}

// ---------------------------------------------------------------------------------
// Settings

const GROUP_ORDER = ["API keys", "Answering", "Retrieval", "Indexing", "Storage"];
const KEY_PROVIDER = { DEEPSEEK_API_KEY: "deepseek", GROQ_API_KEY: "groq", OPENAI_API_KEY: "openai", GEMINI_API_KEY: "gemini" };
const settingsState = { items: [], remove: new Set() };

async function openSettings() {
  $("#settings-error").textContent = "";
  $("#settings-dialog").showModal();
  try {
    const data = await api("/api/settings");
    settingsState.items = data.settings;
    settingsState.remove = new Set();
    renderSettings();
  } catch (e) { $("#settings-error").textContent = e.message; }
}

function settingField(item) {
  const id = `set-${item.key}`;
  const help = item.help ? el("span", { class: "help" }, item.help) : null;
  if (item.kind === "secret") {
    const input = el("input", { id, type: "password", autocomplete: "off", "data-key": item.key,
      placeholder: item.is_set ? "Paste a new key to replace the saved one" : "Paste your key" });
    const status = el("span", { class: "key-status" });
    const result = el("p", { class: "test-result", role: "status" });
    const removeBtn = el("button", { class: "link-btn danger", type: "button" });
    const paint = () => {
      const removing = settingsState.remove.has(item.key);
      status.className = `key-status ${removing ? "removing" : item.is_set ? "set" : "unset"}`;
      status.textContent = removing ? "Will be removed when you save" : item.is_set ? `Saved · ${item.hint || "set"}` : "Not set";
      removeBtn.textContent = removing ? "Undo remove" : "Remove";
      removeBtn.hidden = !item.is_set;
    };
    removeBtn.addEventListener("click", () => {
      if (settingsState.remove.has(item.key)) settingsState.remove.delete(item.key); else settingsState.remove.add(item.key);
      paint();
    });
    paint();
    const provider = KEY_PROVIDER[item.key];
    const test = provider ? el("button", { class: "btn btn-sm", type: "button", onclick: async () => {
      result.className = "test-result"; result.textContent = "Testing…";
      try {
        const r = await api("/api/settings/test", { method: "POST", body: { provider, key: input.value.trim() || null } });
        result.className = `test-result ${r.ok ? "ok" : "fail"}`; result.textContent = r.message;
      } catch (e) { result.className = "test-result fail"; result.textContent = e.message; }
    } }, "Test") : null;
    return el("div", { class: "field" }, el("label", { for: id }, item.label),
      el("div", { class: "key-row" }, input, test), el("div", {}, status, " ", removeBtn), result, help);
  }
  let control;
  if (item.kind === "choice") {
    control = el("select", { id, "data-key": item.key }, item.choices.map((c) =>
      el("option", { value: c, selected: c === item.value }, item.choice_labels?.[c] || c)));
  } else if (item.kind === "number" || item.kind === "int") {
    control = el("input", { id, type: "number", "data-key": item.key, value: item.value ?? "", min: item.minimum, max: item.maximum,
      step: item.kind === "int" ? 1 : "any" });
  } else {
    control = el("input", { id, type: item.kind === "url" ? "url" : "text", "data-key": item.key, value: item.value ?? "",
      placeholder: item.kind === "url" ? "http://localhost:6333" : "" });
  }
  return el("div", { class: "field" }, el("label", { for: id }, item.label, item.unit ? el("span", { class: "unit" }, ` (${item.unit})`) : null), control, help);
}

function renderSettings() {
  const groups = {};
  for (const item of settingsState.items) (groups[item.group] ||= []).push(item);
  setChildren($("#settings-body"), GROUP_ORDER.filter((g) => groups[g]).map((g) =>
    el("section", { class: "settings-group" },
      el("h3", {}, g, g !== "API keys" ? el("button", { class: "link-btn small", type: "button", onclick: () => restoreDefaults(groups[g]) }, "Restore defaults") : null),
      el("div", { class: g === "API keys" ? "" : "grid-2" }, groups[g].map(settingField)))));
}

function restoreDefaults(items) {
  for (const item of items) {
    const input = document.querySelector(`[data-key="${item.key}"]`);
    if (input && item.kind !== "secret") input.value = item.default ?? "";
  }
}

async function saveSettings(e) {
  e.preventDefault();
  const values = {};
  for (const item of settingsState.items) {
    const input = document.querySelector(`[data-key="${item.key}"]`);
    if (!input) continue;
    const v = input.value.trim();
    if (item.kind === "secret") { if (v) values[item.key] = v; }
    else if (String(item.value ?? "") !== v) values[item.key] = v;
  }
  try {
    await api("/api/settings", { method: "POST", body: { values, remove: [...settingsState.remove] } });
    $("#settings-dialog").close();
    await loadHealth();
    await loadRepos();
  } catch (err) { $("#settings-error").textContent = err.message; }
}

// ---------------------------------------------------------------------------------
// Eval view

const pct = (v) => v == null ? "–" : `${(v * 100).toFixed(1)}%`;

async function renderEval() {
  const view = $("#eval-view");
  setChildren(view, el("div", { class: "eval-inner" }, el("p", { class: "muted" }, "Loading…")));
  let data;
  try { data = await api("/api/eval"); } catch (e) { setChildren(view, el("p", { class: "error" }, e.message)); return; }
  const run = data.run;
  if (!run) {
    setChildren(view, el("div", { class: "eval-inner" }, el("h1", {}, "Evaluation"),
      el("p", {}, "No eval runs yet. Run the benchmark from a terminal in the project folder:"),
      el("pre", { class: "code", style: "padding:0.8rem" }, "python -m eval.run")));
    return;
  }
  const configs = run.retrieval?.configs || [];
  const ks = run.retrieval?.k_values || [1, 3, 5, 10];
  const metricCols = [...ks.map((k) => [`R@${k}`, (c) => c.recall[String(k)]]), ["MRR", (c) => c.mrr],
    ["Ctx precision@5", (c) => c["context_precision@5"]]];
  const best = metricCols.map(([, f]) => Math.max(...configs.map((c) => f(c) ?? -1)));
  const top = configs.reduce((a, c) => (!a || c.recall["5"] > a.recall["5"] ? c : a), null);
  const gen = run.generation;
  const cards = [
    top && ["Best Recall@5", pct(top.recall["5"]), top.label],
    top && ["Its MRR", (top.mrr ?? 0).toFixed(3), "1.0 = right chunk always ranked first"],
    gen && ["Key facts in answers", pct(gen.key_fact_coverage), `${gen.model} · ${gen.questions} questions`],
    gen && ["Answers citing the right code", pct(gen.cited_gold_rate), "a cited excerpt overlaps the gold code"],
  ].filter(Boolean);
  setChildren(view, el("div", { class: "eval-inner" },
    el("h1", {}, "Evaluation"),
    el("p", { class: "muted" }, `Run ${run.run_id} · ${run.corpus?.questions} questions on ${run.corpus?.name} (${run.corpus?.files} files) · ${run.created_at}`),
    el("div", { class: "cards" }, cards.map(([k, v, d]) => el("div", { class: "card" }, el("div", { class: "k" }, k), el("div", { class: "v" }, v), el("div", { class: "d" }, d)))),
    el("h2", {}, "Retrieval: did the right code come back?"),
    el("p", { class: "muted small" }, "R@k = share of questions where a gold chunk is in the top k. MRR = mean of 1/rank of the first gold chunk. Context precision@5 = share of the top-5 chunks' lines that are inside the gold code."),
    el("div", { class: "table-wrap" }, el("table", { class: "data-table" },
      el("thead", {}, el("tr", {}, ["Configuration", "Chunking", "Embedding", "Search", "Re-rank", ...metricCols.map(([h]) => h), "p50 latency"].map((h) => el("th", {}, h)))),
      el("tbody", {}, configs.map((c) => el("tr", { class: c === top ? "best" : "" },
        el("td", {}, c.label), el("td", {}, c.chunker), el("td", {}, MODEL_SHORT[c.embed_model] || c.embed_model), el("td", {}, c.mode), el("td", {}, c.rerank ? "yes" : "no"),
        metricCols.map(([name, f], i) => { const v = f(c); return el("td", { class: `num ${v === best[i] ? "top" : ""}` }, name === "MRR" ? (v ?? 0).toFixed(3) : pct(v)); }),
        el("td", { class: "num" }, fmtMs(c.latency_ms?.p50))))))),
    gen ? (() => {
      const runs = run.generation_runs?.length ? run.generation_runs : [gen];
      const rows = [
        ["Key-fact coverage", (g) => pct(g.key_fact_coverage)],
        ["All key facts present", (g) => pct(g.fully_correct_rate)],
        ["Cites a gold excerpt", (g) => pct(g.cited_gold_rate)],
        ["Citations valid", (g) => pct(g.citation_validity)],
        ["Declines unanswerable questions", (g) => g.abstain_ok == null ? "–" : pct(g.abstain_ok)],
        ["Wrongly declines answerable ones", (g) => pct(g.false_abstain_rate)],
        ["LLM judge: correctness (1–5)", (g) => g.judge ? g.judge.correctness.toFixed(2) : "not run"],
        ["LLM judge: grounded (1–5)", (g) => g.judge ? g.judge.grounded.toFixed(2) : "not run"],
        ["Median time per answer", (g) => fmtMs(g.latency_ms?.p50)],
      ];
      return [el("h2", {}, "Answers (generation)"),
        el("p", { class: "muted small" }, `${runs[0].model} answering all ${runs[0].questions} questions with each retrieval setup. Key facts are checked deterministically; the LLM judge is a separate, fuzzier score.`),
        el("div", { class: "table-wrap" }, el("table", { class: "data-table gen-table" },
          el("thead", {}, el("tr", {}, el("th", {}, "Metric"), runs.map((g) => el("th", {}, g.label)))),
          el("tbody", {}, rows.map(([k, f]) => el("tr", {}, el("th", {}, k), runs.map((g) => el("td", { class: "num" }, f(g))))))))];
    })() : null,
    run.notes?.length ? [el("h2", {}, "Notes"), el("ul", {}, run.notes.map((n) => el("li", { class: "muted" }, n)))] : null));
}

function showView(name) {
  for (const b of document.querySelectorAll(".view-btn")) b.classList.toggle("active", b.dataset.view === name);
  $("#chat-view").hidden = name !== "chat";
  $("#eval-view").hidden = name !== "eval";
  $(".sidebar").hidden = name !== "chat";
  if (name !== "chat") $("#viewer").hidden = true;
  if (name === "eval") renderEval();
}

// ---------------------------------------------------------------------------------
// Wiring

document.addEventListener("DOMContentLoaded", async () => {
  $("#settings-btn").addEventListener("click", openSettings);
  $("#add-repo-btn").addEventListener("click", openAddDialog);
  $("#add-form").addEventListener("submit", submitAdd);
  $("#settings-form").addEventListener("submit", saveSettings);
  for (const btn of document.querySelectorAll("[data-close]")) btn.addEventListener("click", () => btn.closest("dialog").close());
  for (const b of document.querySelectorAll(".view-btn")) b.addEventListener("click", () => showView(b.dataset.view));
  $("#ask-form").addEventListener("submit", (e) => { e.preventDefault(); send(); });
  $("#question").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  $("#viewer-close").addEventListener("click", () => { $("#viewer").hidden = true; });
  $("#viewer-more").addEventListener("click", () => { const v = state.viewer; if (v) openViewer(v.repoId, v.hit, !v.whole); });
  $("#clear-chat").addEventListener("click", () => {
    if (!state.selected || !confirm("Clear this repo's chat?")) return;
    state.chats[state.selected] = []; store.del(`ragg.chat.${state.selected}`); renderThread();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !document.querySelector("dialog[open]")) $("#viewer").hidden = true; });
  await Promise.all([loadHealth(), loadRepos()]);
  renderThread();
});
