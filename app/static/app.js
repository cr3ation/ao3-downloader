"use strict";

const state = {
  results: new Map(), // work_id -> Work
  order: [], // work_ids in server-returned order
  selected: new Set(), // checked work_ids
  statuses: new Map(), // work_id -> {status, message} (survives re-sorts)
  sort: { key: null, dir: "desc" }, // local results sort
  searchType: "author",
  lastQuery: "",
  library: { categories: [], loaded: false },
  activeTab: "search",
};

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- Tabs

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => switchTab(btn.dataset.tab));
});

function switchTab(name) {
  state.activeTab = name;
  $("tab-search").classList.toggle("hidden", name !== "search");
  $("tab-library").classList.toggle("hidden", name !== "library");
  document.querySelectorAll(".tab-btn").forEach((b) => {
    const active = b.dataset.tab === name;
    b.classList.toggle("border-indigo-500", active);
    b.classList.toggle("text-white", active);
    b.classList.toggle("border-transparent", !active);
    b.classList.toggle("text-slate-400", !active);
  });
  if (name === "library") loadLibrary();
}

// ---------------------------------------------------------------- SSE

const es = new EventSource("/api/events");

es.addEventListener("snapshot", (e) => {
  hideBanner();
  const snap = JSON.parse(e.data);
  if (snap.current) showProgress(snap.current);
});

es.addEventListener("log", (e) => appendLog(JSON.parse(e.data)));

es.addEventListener("progress", (e) => showProgress(JSON.parse(e.data)));

es.addEventListener("item_done", (e) => {
  const d = JSON.parse(e.data);
  markRow(d.work_id, d.status, d.message);
});

es.addEventListener("job_done", (e) => {
  const d = JSON.parse(e.data);
  const summary = $("progress-summary");
  summary.textContent = `Finished — done: ${d.done} · skipped: ${d.skipped} · errors: ${d.errors}`;
  summary.classList.remove("hidden");
  $("progress-text").textContent = "Job complete";
  $("progress-bar").style.width = "100%";
  $("download-btn").disabled = false;
  state.library.loaded = false; // stale now — refetch on next Library visit
});

es.onerror = () => showBanner();
es.onopen = () => hideBanner();

function showBanner() {
  $("connection-banner").classList.remove("hidden");
}
function hideBanner() {
  $("connection-banner").classList.add("hidden");
}

// ---------------------------------------------------------------- Log window

const MAX_LOG_ENTRIES = 500;
const LOG_COLORS = { info: "text-slate-400", warning: "text-amber-400", error: "text-red-400" };

function appendLog({ level, message, ts }) {
  const log = $("log");
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
  const line = document.createElement("div");
  line.className = LOG_COLORS[level] || "text-slate-400";
  const time = ts ? new Date(ts).toLocaleTimeString() : new Date().toLocaleTimeString();
  line.textContent = `[${time}] ${message}`;
  log.appendChild(line);
  while (log.children.length > MAX_LOG_ENTRIES) log.removeChild(log.firstChild);
  if (atBottom) log.scrollTop = log.scrollHeight;
}

$("clear-log").addEventListener("click", () => ($("log").innerHTML = ""));

// ---------------------------------------------------------------- Search

document.querySelectorAll(".type-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    state.searchType = btn.dataset.type;
    document.querySelectorAll(".type-btn").forEach((b) => {
      const active = b === btn;
      b.classList.toggle("bg-indigo-600", active);
      b.classList.toggle("text-white", active);
      b.classList.toggle("bg-slate-800", !active);
      b.classList.toggle("text-slate-300", !active);
    });
  });
});

function readFilters() {
  const wordsMin = parseInt($("filter-words-min").value, 10);
  const wordsMax = parseInt($("filter-words-max").value, 10);
  const excludeTags = $("filter-exclude").value
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  const filters = {
    complete_only: $("filter-complete").checked,
    words_from: Number.isFinite(wordsMin) ? wordsMin : null,
    words_to: Number.isFinite(wordsMax) ? wordsMax : null,
    exclude_tags: excludeTags,
  };
  const active =
    filters.complete_only || filters.words_from !== null || filters.words_to !== null || excludeTags.length > 0;
  $("filters-active").classList.toggle("hidden", !active);
  return filters;
}

$("search-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = $("query").value.trim();
  if (!query) return;

  setSearching(true);
  try {
    const resp = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        query,
        search_type: state.searchType,
        max_results: parseInt($("max-results").value, 10) || 100,
        sort_by: $("sort-by").value,
        ...readFilters(),
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      appendLog({ level: "error", message: `Search failed: ${err.detail || resp.statusText}` });
      return;
    }
    const data = await resp.json();
    state.lastQuery = query;
    renderResults(data);
  } catch (err) {
    appendLog({ level: "error", message: `Search request failed: ${err.message}` });
  } finally {
    setSearching(false);
  }
});

function setSearching(on) {
  $("search-btn").disabled = on;
  $("search-spinner").classList.toggle("hidden", !on);
  $("search-btn-label").textContent = on ? "Searching..." : "Search";
}

// ---------------------------------------------------------------- Results table

function renderResults({ works, message, truncated }) {
  state.results = new Map(works.map((w) => [w.work_id, w]));
  state.order = works.map((w) => w.work_id);
  state.selected = new Set(state.order);
  state.statuses = new Map();
  state.sort = { key: null, dir: "desc" };

  $("results-count").textContent =
    works.length === 0 ? "No works found." : `${works.length} works found${truncated ? " (limit reached)" : ""}`;
  $("results-message").textContent = message || "";
  $("results-card").classList.remove("hidden");
  $("select-all").checked = true;

  renderResultRows();
}

const RATING_COLORS = {
  "General Audiences": "bg-emerald-900/60 text-emerald-300",
  "Teen And Up Audiences": "bg-amber-900/60 text-amber-300",
  "Mature": "bg-orange-900/60 text-orange-300",
  "Explicit": "bg-red-900/60 text-red-300",
};

function sortedWorkIds() {
  const ids = [...state.order];
  const { key, dir } = state.sort;
  if (!key) return ids;
  const sign = dir === "asc" ? 1 : -1;
  ids.sort((a, b) => {
    const va = state.results.get(a)?.[key];
    const vb = state.results.get(b)?.[key];
    if (va == null && vb == null) return 0;
    if (va == null) return 1; // nulls always last
    if (vb == null) return -1;
    return (va - vb) * sign;
  });
  return ids;
}

function renderResultRows() {
  const body = $("results-body");
  body.innerHTML = "";

  document.querySelectorAll("th.sortable").forEach((th) => {
    const arrow = th.querySelector(".sort-arrow");
    arrow.textContent = th.dataset.sortKey === state.sort.key ? (state.sort.dir === "asc" ? "↑" : "↓") : "";
  });

  for (const id of sortedWorkIds()) {
    const w = state.results.get(id);
    const tr = document.createElement("tr");
    tr.dataset.workId = w.work_id;

    const tdCheck = document.createElement("td");
    tdCheck.className = "px-5 py-3 align-top";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = state.selected.has(w.work_id);
    cb.className = "row-check rounded accent-indigo-500";
    cb.addEventListener("change", () => {
      if (cb.checked) state.selected.add(w.work_id);
      else state.selected.delete(w.work_id);
      updateSelectedCount();
    });
    tdCheck.appendChild(cb);

    const tdWork = document.createElement("td");
    tdWork.className = "px-2 py-3";
    const link = document.createElement("a");
    link.href = `https://archiveofourown.org/works/${w.work_id}`;
    link.target = "_blank";
    link.rel = "noopener";
    link.className = "text-indigo-400 hover:text-indigo-300 font-medium";
    link.textContent = w.title;
    const author = document.createElement("span");
    author.className = "text-slate-400";
    author.textContent = ` by ${w.authors.join(", ")}`;
    const head = document.createElement("div");
    head.className = "flex flex-wrap items-center gap-1.5";
    head.append(link, author);
    for (const badge of workBadges(w)) head.appendChild(badge);

    const tags = document.createElement("div");
    tags.className = "mt-1 flex flex-wrap gap-1";
    for (const t of w.tags.slice(0, 5)) {
      const chip = document.createElement("span");
      chip.className = "bg-slate-800 text-slate-400 text-[11px] rounded px-1.5 py-0.5";
      chip.textContent = t;
      tags.appendChild(chip);
    }
    if (w.tags.length > 5) {
      const more = document.createElement("span");
      more.className = "text-slate-500 text-[11px] py-0.5";
      more.textContent = `+${w.tags.length - 5} more`;
      tags.appendChild(more);
    }

    tdWork.append(head, tags);
    if (w.summary) {
      const sum = document.createElement("p");
      sum.className = "mt-1 text-slate-500 text-xs line-clamp-2 max-w-2xl";
      sum.textContent = w.summary;
      tdWork.appendChild(sum);
    }

    const tdWords = numberCell(w.word_count);
    const tdKudos = numberCell(w.kudos);
    const tdHits = numberCell(w.hits);

    const tdStatus = document.createElement("td");
    tdStatus.className = "px-5 py-3 align-top status-cell";

    tr.append(tdCheck, tdWork, tdWords, tdKudos, tdHits, tdStatus);
    body.appendChild(tr);

    const st = state.statuses.get(w.work_id);
    if (st) applyStatusBadge(tr, st.status, st.message);
  }
  updateSelectedCount();
}

function numberCell(value) {
  const td = document.createElement("td");
  td.className = "px-2 py-3 align-top text-slate-400 whitespace-nowrap";
  td.textContent = value != null ? value.toLocaleString("en-US") : "—";
  return td;
}

function workBadges(w) {
  const badges = [];
  if (w.rating) {
    const b = document.createElement("span");
    b.className = `text-[11px] rounded px-1.5 py-0.5 ${RATING_COLORS[w.rating] || "bg-slate-800 text-slate-400"}`;
    b.textContent = w.rating;
    badges.push(b);
  }
  if (w.complete === true) {
    const b = document.createElement("span");
    b.className = "text-[11px] rounded px-1.5 py-0.5 border border-emerald-700 text-emerald-400";
    b.textContent = w.chapters ? `Complete · ${w.chapters}` : "Complete";
    badges.push(b);
  } else if (w.complete === false) {
    const b = document.createElement("span");
    b.className = "text-[11px] rounded px-1.5 py-0.5 border border-sky-700 text-sky-400";
    b.textContent = w.chapters ? `WIP · ${w.chapters}` : "WIP";
    badges.push(b);
  }
  return badges;
}

document.querySelectorAll("th.sortable").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sortKey;
    if (state.sort.key === key) {
      state.sort.dir = state.sort.dir === "desc" ? "asc" : "desc";
    } else {
      state.sort = { key, dir: "desc" };
    }
    renderResultRows(); // purely local — no AO3 requests
  });
});

$("select-all").addEventListener("change", (e) => {
  if (e.target.checked) state.selected = new Set(state.order);
  else state.selected = new Set();
  document.querySelectorAll(".row-check").forEach((cb) => (cb.checked = e.target.checked));
  updateSelectedCount();
});

function updateSelectedCount() {
  const n = state.selected.size;
  $("selected-count").textContent = n;
  $("download-btn").disabled = n === 0;
}

const STATUS_BADGES = {
  done: ["✓ Done", "bg-emerald-900/60 text-emerald-300"],
  skipped: ["Skipped", "bg-slate-800 text-slate-400"],
  error: ["Error", "bg-red-900/60 text-red-300"],
  queued: ["Queued", "bg-slate-800 text-slate-400"],
  downloading: ["Downloading", "bg-indigo-900/60 text-indigo-300"],
};

function markRow(workId, status, message) {
  state.statuses.set(workId, { status, message });
  const tr = document.querySelector(`tr[data-work-id="${workId}"]`);
  if (tr) applyStatusBadge(tr, status, message);
}

function applyStatusBadge(tr, status, message) {
  const cell = tr.querySelector(".status-cell");
  const [label, classes] = STATUS_BADGES[status] || [status, "bg-slate-800 text-slate-400"];
  cell.innerHTML = "";
  const badge = document.createElement("span");
  badge.className = `inline-block text-[11px] rounded px-2 py-0.5 ${classes}`;
  badge.textContent = label;
  if (message) badge.title = message;
  cell.appendChild(badge);
}

// ---------------------------------------------------------------- Download

$("download-btn").addEventListener("click", async () => {
  const selected = [...state.selected].map((id) => state.results.get(id)).filter(Boolean);
  if (selected.length === 0) return;

  $("download-btn").disabled = true;
  try {
    const resp = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        works: selected,
        format: $("format").value,
        category: state.lastQuery,
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      appendLog({ level: "error", message: `Enqueue failed: ${err.detail || resp.statusText}` });
      $("download-btn").disabled = false;
      return;
    }
    selected.forEach((w) => markRow(w.work_id, "queued", ""));
    $("progress-summary").classList.add("hidden");
    $("progress-card").classList.remove("hidden");
    $("progress-text").textContent = "Queued...";
    $("progress-bar").style.width = "0%";
  } catch (err) {
    appendLog({ level: "error", message: `Enqueue request failed: ${err.message}` });
    $("download-btn").disabled = false;
  }
});

function showProgress({ current, total, title }) {
  $("progress-card").classList.remove("hidden");
  $("progress-summary").classList.add("hidden");
  $("progress-text").textContent = `Downloading: ${title}`;
  $("progress-fraction").textContent = `${current} / ${total}`;
  $("progress-bar").style.width = `${Math.round((current / total) * 100)}%`;
}

// ---------------------------------------------------------------- Library

$("lib-refresh").addEventListener("click", () => loadLibrary(true));
$("lib-filter").addEventListener("input", renderLibrary);
$("lib-sort").addEventListener("change", renderLibrary);

async function loadLibrary(force = false) {
  if (state.library.loaded && !force) {
    renderLibrary();
    return;
  }
  try {
    const resp = await fetch("/api/downloads");
    if (!resp.ok) throw new Error(resp.statusText);
    const data = await resp.json();
    state.library.categories = data.categories;
    state.library.loaded = true;
    renderLibrary();
  } catch (err) {
    appendLog({ level: "error", message: `Failed to load library: ${err.message}` });
  }
}

function libSortValue(file, key) {
  const e = file.entry;
  switch (key) {
    case "title": return (e?.title || file.filename).toLowerCase();
    case "author": return (e?.authors?.[0] || "￿").toLowerCase();
    case "words": return e?.word_count ?? -1;
    case "downloaded": return e?.downloaded_at || "";
    case "size": return file.size;
    default: return file.filename;
  }
}

function matchesLibFilter(file, needle) {
  if (!needle) return true;
  const e = file.entry;
  const haystack = [file.filename, e?.title, ...(e?.authors || []), ...(e?.tags || []), ...(e?.fandoms || [])]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes(needle);
}

function formatBytes(n) {
  if (n >= 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(n / 1024))} KB`;
}

function renderLibrary() {
  const needle = $("lib-filter").value.trim().toLowerCase();
  const sortKey = $("lib-sort").value;
  const numeric = sortKey === "words" || sortKey === "size";
  const desc = numeric || sortKey === "downloaded"; // biggest/newest first

  const container = $("lib-categories");
  container.innerHTML = "";
  let shown = 0;

  for (const cat of state.library.categories) {
    const files = cat.files.filter((f) => matchesLibFilter(f, needle));
    if (files.length === 0) continue;
    files.sort((a, b) => {
      const va = libSortValue(a, sortKey);
      const vb = libSortValue(b, sortKey);
      const cmp = numeric ? va - vb : String(va).localeCompare(String(vb));
      return desc ? -cmp : cmp;
    });
    shown += files.length;

    const section = document.createElement("div");
    section.className = "px-5 py-4";
    const header = document.createElement("h3");
    header.className = "text-sm font-medium text-slate-300 mb-3";
    const totalSize = files.reduce((s, f) => s + f.size, 0);
    const displayName = cat.name === "_root" ? "Downloads folder (awaiting Calibre import)" : cat.name;
    header.textContent = `${displayName} · ${files.length} ${files.length === 1 ? "book" : "books"} · ${formatBytes(totalSize)}`;
    section.appendChild(header);

    const table = document.createElement("table");
    table.className = "w-full text-sm";
    const tbody = document.createElement("tbody");
    tbody.className = "divide-y divide-slate-800/60";

    for (const f of files) {
      tbody.appendChild(libraryRow(cat.name, f));
    }
    table.appendChild(tbody);
    const wrap = document.createElement("div");
    wrap.className = "overflow-x-auto";
    wrap.appendChild(table);
    section.appendChild(wrap);
    container.appendChild(section);
  }

  $("lib-empty").classList.toggle("hidden", shown > 0);
}

function libraryRow(category, file) {
  const e = file.entry;
  const tr = document.createElement("tr");

  const tdTitle = document.createElement("td");
  tdTitle.className = "py-2.5 pr-3";
  const head = document.createElement("div");
  head.className = "flex flex-wrap items-center gap-1.5";
  if (e?.url) {
    const link = document.createElement("a");
    link.href = e.url;
    link.target = "_blank";
    link.rel = "noopener";
    link.className = "text-indigo-400 hover:text-indigo-300 font-medium";
    link.textContent = e.title || file.filename;
    head.appendChild(link);
  } else {
    const span = document.createElement("span");
    span.className = "text-slate-200 font-medium";
    span.textContent = e?.title || file.filename;
    head.appendChild(span);
  }
  if (e?.authors?.length) {
    const author = document.createElement("span");
    author.className = "text-slate-400";
    author.textContent = `by ${e.authors.join(", ")}`;
    head.appendChild(author);
  }
  for (const badge of workBadges(e || {})) head.appendChild(badge);
  tdTitle.appendChild(head);

  const tdWords = document.createElement("td");
  tdWords.className = "py-2.5 px-3 text-slate-400 whitespace-nowrap";
  tdWords.textContent = e?.word_count != null ? `${e.word_count.toLocaleString("en-US")} words` : "—";

  const tdFormat = document.createElement("td");
  tdFormat.className = "py-2.5 px-3 whitespace-nowrap";
  const fmt = document.createElement("span");
  fmt.className = "bg-slate-800 text-slate-300 text-[11px] uppercase rounded px-1.5 py-0.5";
  fmt.textContent = e?.format || file.filename.split(".").pop();
  tdFormat.appendChild(fmt);

  const tdSize = document.createElement("td");
  tdSize.className = "py-2.5 px-3 text-slate-400 whitespace-nowrap";
  tdSize.textContent = formatBytes(file.size);

  const tdDate = document.createElement("td");
  tdDate.className = "py-2.5 px-3 text-slate-500 whitespace-nowrap";
  tdDate.textContent = e?.downloaded_at ? new Date(e.downloaded_at).toLocaleDateString() : "—";

  const tdActions = document.createElement("td");
  tdActions.className = "py-2.5 pl-3 whitespace-nowrap text-right";
  const fileUrl = `/api/downloads/${encodeURIComponent(category)}/${encodeURIComponent(file.filename)}`;
  const dl = document.createElement("a");
  dl.href = fileUrl;
  dl.setAttribute("download", "");
  dl.className = "text-sm text-indigo-400 hover:text-indigo-300 mr-4";
  dl.textContent = "Download";
  const del = document.createElement("button");
  del.className = "text-sm text-red-400 hover:text-red-300";
  del.textContent = "Delete";
  del.addEventListener("click", () => deleteLibraryFile(category, file));
  tdActions.append(dl, del);

  tr.append(tdTitle, tdWords, tdFormat, tdSize, tdDate, tdActions);
  return tr;
}

async function deleteLibraryFile(category, file) {
  const title = file.entry?.title || file.filename;
  if (!confirm(`Delete "${title}"? This removes the file and its metadata entry.`)) return;
  try {
    const resp = await fetch(`/api/downloads/${encodeURIComponent(category)}/${encodeURIComponent(file.filename)}`, {
      method: "DELETE",
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      appendLog({ level: "error", message: `Delete failed: ${err.detail || resp.statusText}` });
      return;
    }
    const cat = state.library.categories.find((c) => c.name === category);
    if (cat) cat.files = cat.files.filter((f) => f.filename !== file.filename);
    renderLibrary();
  } catch (err) {
    appendLog({ level: "error", message: `Delete request failed: ${err.message}` });
  }
}
