"use strict";

const state = {
  results: new Map(), // work_id -> Work
  order: [], // work_ids in server-returned order
  selected: new Set(), // checked work_ids
  statuses: new Map(), // work_id -> {status, message} (survives re-sorts)
  downloaded: new Map(), // work_id -> formats already in the library
  sort: { key: null, dir: "desc" }, // local results sort
  searchType: "author",
  lastQuery: "",
  category: "", // download folder name; differs from lastQuery for pasted links
  openPanel: null, // "filters" | "guide" | null
  library: { categories: [], loaded: false },
  activeTab: "search",
  dlCounts: { done: 0, skipped: 0, error: 0 }, // running tally for the download bar
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

// ---------------------------------------------------------------- User menu

(() => {
  const btn = $("user-menu-btn");
  const menu = $("user-menu");
  if (!btn || !menu) return;
  const close = () => {
    menu.classList.add("hidden");
    btn.setAttribute("aria-expanded", "false");
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = !menu.classList.toggle("hidden");
    btn.setAttribute("aria-expanded", String(open));
  });
  document.addEventListener("click", (e) => {
    if (!menu.contains(e.target) && !btn.contains(e.target)) close();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") close();
  });
})();

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
  if (d.status in state.dlCounts) {
    state.dlCounts[d.status] += 1;
    renderDlCounts(state.dlCounts.done, state.dlCounts.skipped, state.dlCounts.error);
  }
});

es.addEventListener("job_done", (e) => {
  const d = JSON.parse(e.data);
  $("progress-text").textContent = "Job complete";
  $("progress-bar").style.width = "100%";
  renderDlCounts(d.done, d.skipped, d.errors);
  $("download-btn").disabled = false;
  state.library.loaded = false; // stale now — refetch on next Library visit
  // Leave the final tally up briefly, then retract the bar (the full detail
  // stays in the activity log below).
  clearTimeout(state.dlHideTimer);
  state.dlHideTimer = setTimeout(hideDlBar, 6000);
});

es.onerror = () => {
  // A 401 closes the stream permanently rather than retrying, so a signed-out
  // session would otherwise sit behind the reconnect banner forever.
  if (es.readyState === EventSource.CLOSED) {
    window.location = "/login?ok=session_expired";
    return;
  }
  showBanner();
};
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

// ---------------------------------------------------------------- Filters & guide panels

const WORK_LINK = /(?:archiveofourown\.org|ao3\.org)?\/?works\/(\d+)/i;

function isWorkLink(query) {
  const q = query.trim();
  return WORK_LINK.test(q) || (/^\d+$/.test(q) && q.length >= 5);
}

function togglePanel(name) {
  state.openPanel = state.openPanel === name ? null : name;
  for (const [panel, btn] of [
    ["filters", "toggle-filters"],
    ["guide", "toggle-guide"],
  ]) {
    const open = state.openPanel === panel;
    $(panel === "filters" ? "filters-panel" : "guide-panel").classList.toggle("hidden", !open);
    $(btn).setAttribute("aria-expanded", String(open));
  }
  paintFilterChip();
}

$("toggle-filters").addEventListener("click", () => togglePanel("filters"));
$("toggle-guide").addEventListener("click", () => togglePanel("guide"));

function readFilters() {
  const wordsMin = parseInt($("filter-words-min").value, 10);
  const wordsMax = parseInt($("filter-words-max").value, 10);
  const excludeTags = $("filter-exclude").value
    .split(",")
    .map((t) => t.trim())
    .filter(Boolean);
  return {
    complete_only: $("filter-complete").checked,
    words_from: Number.isFinite(wordsMin) ? wordsMin : null,
    words_to: Number.isFinite(wordsMax) ? wordsMax : null,
    exclude_tags: excludeTags,
  };
}

function activeFilterCount() {
  const f = readFilters();
  return (
    (f.complete_only ? 1 : 0) +
    (f.words_from !== null ? 1 : 0) +
    (f.words_to !== null ? 1 : 0) +
    (f.exclude_tags.length ? 1 : 0)
  );
}

const CHIP_IDLE = ["bg-slate-800/60", "border-slate-700/50", "text-slate-300"];
const CHIP_ACTIVE = ["bg-indigo-600/20", "border-indigo-500/40", "text-indigo-300"];

function paintFilterChip() {
  const count = activeFilterCount();
  const chip = $("toggle-filters");
  chip.classList.remove(...CHIP_IDLE, ...CHIP_ACTIVE);
  chip.classList.add(...(count ? CHIP_ACTIVE : CHIP_IDLE));
  const badge = $("filters-count");
  badge.textContent = count;
  badge.classList.toggle("hidden", count === 0);
}

["filter-complete", "filter-words-min", "filter-words-max", "filter-exclude"].forEach((id) => {
  $(id).addEventListener("input", paintFilterChip);
  $(id).addEventListener("change", paintFilterChip);
});

$("clear-filters").addEventListener("click", () => {
  $("filter-complete").checked = false;
  $("filter-words-min").value = "";
  $("filter-words-max").value = "";
  $("filter-exclude").value = "";
  paintFilterChip();
});

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

function renderResults({ works, message, truncated, downloaded }) {
  state.downloaded = new Map(Object.entries(downloaded || {}));
  // A pasted link would make a useless folder name — file it under the author.
  state.category = isWorkLink(state.lastQuery) && works.length ? works[0].authors[0] : state.lastQuery;

  state.results = new Map(works.map((w) => [w.work_id, w]));
  state.order = works.map((w) => w.work_id);
  state.selected = new Set(); // nothing pre-selected — downloading is opt-in
  state.statuses = new Map();
  state.sort = { key: null, dir: "desc" };

  $("results-count").textContent =
    works.length === 0 ? "No works found." : `${works.length} works found${truncated ? " (limit reached)" : ""}`;
  $("results-message").textContent = message || "";
  $("results-card").classList.remove("hidden");
  $("select-all").checked = false;

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

    // Job status wins; otherwise fall back to what the library already holds.
    const st = state.statuses.get(w.work_id);
    if (st) {
      applyStatusBadge(tr, st.status, st.message);
    } else if (state.downloaded.has(w.work_id)) {
      const formats = state.downloaded.get(w.work_id);
      applyBadge(
        tr,
        "In library",
        "bg-slate-800 text-slate-400 border border-slate-700",
        `Already downloaded as ${formats.join(", ").toUpperCase()} — it will be skipped if selected.`
      );
    }
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
  const [label, classes] = STATUS_BADGES[status] || [status, "bg-slate-800 text-slate-400"];
  applyBadge(tr, label, classes, message);
}

function applyBadge(tr, label, classes, title) {
  const cell = tr.querySelector(".status-cell");
  cell.innerHTML = "";
  const badge = document.createElement("span");
  badge.className = `inline-block text-[11px] rounded px-2 py-0.5 ${classes}`;
  badge.textContent = label;
  if (title) badge.title = title;
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
        category: state.category || state.lastQuery,
      }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      appendLog({ level: "error", message: `Enqueue failed: ${err.detail || resp.statusText}` });
      $("download-btn").disabled = false;
      return;
    }
    selected.forEach((w) => markRow(w.work_id, "queued", ""));
    clearTimeout(state.dlHideTimer);
    state.dlCounts = { done: 0, skipped: 0, error: 0 };
    renderDlCounts(0, 0, 0);
    $("progress-text").textContent = "Queued…";
    $("progress-fraction").textContent = `0 / ${selected.length}`;
    $("progress-bar").style.width = "0%";
    showDlBar();
  } catch (err) {
    appendLog({ level: "error", message: `Enqueue request failed: ${err.message}` });
    $("download-btn").disabled = false;
  }
});

function showProgress({ current, total, title }) {
  showDlBar();
  clearTimeout(state.dlHideTimer);
  $("progress-text").textContent = `Downloading: ${title}`;
  $("progress-fraction").textContent = `${current} / ${total}`;
  $("progress-bar").style.width = `${Math.round((current / total) * 100)}%`;
  renderDlCounts(state.dlCounts.done, state.dlCounts.skipped, state.dlCounts.error);
}

// The sticky download bar shifts the page down while visible so it never
// covers the header — mirrors the #connection-banner fixed idiom.
function showDlBar() {
  $("dl-bar").classList.remove("hidden");
  document.body.style.paddingTop = "56px";
}

function hideDlBar() {
  $("dl-bar").classList.add("hidden");
  document.body.style.paddingTop = "";
}

function renderDlCounts(done, skipped, error) {
  $("progress-counts").innerHTML =
    `<span class="text-emerald-400">${done} done</span>` +
    `<span class="text-slate-400">${skipped} skipped</span>` +
    `<span class="text-red-400">${error} failed</span>`;
}

$("dl-bar-close").addEventListener("click", hideDlBar);

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
    case "size": return file.size ?? -1;
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
  if (n == null) return "—";
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
    if (files.length === 0) continue; // nothing matched the text filter
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
    const onDisk = files.filter((f) => f.present);
    const totalSize = onDisk.reduce((s, f) => s + f.size, 0);
    const displayName = cat.name === "_root" ? "Downloads" : cat.name;
    const parts = [displayName, `${files.length} ${files.length === 1 ? "book" : "books"}`];
    parts.push(onDisk.length ? `${onDisk.length} on disk · ${formatBytes(totalSize)}` : "all imported");
    header.textContent = parts.join(" · ");
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
  if (!file.present) {
    const imported = document.createElement("span");
    imported.className = "text-[11px] rounded px-1.5 py-0.5 border border-slate-700 text-slate-400";
    imported.textContent = "Imported";
    imported.title = "No longer in the downloads folder — Calibre has picked it up.";
    head.appendChild(imported);
  }
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
  if (file.present) {
    const dl = document.createElement("a");
    dl.href = `/api/downloads/${encodeURIComponent(category)}/${encodeURIComponent(file.filename)}`;
    dl.setAttribute("download", "");
    dl.className = "text-sm text-indigo-400 hover:text-indigo-300 mr-4";
    dl.textContent = "Download";
    tdActions.appendChild(dl);
  }
  const del = document.createElement("button");
  del.className = "text-sm text-red-400 hover:text-red-300";
  del.textContent = file.present ? "Delete" : "Forget";
  del.addEventListener("click", () => deleteLibraryFile(category, file));
  tdActions.appendChild(del);

  tr.append(tdTitle, tdWords, tdFormat, tdSize, tdDate, tdActions);
  return tr;
}

async function deleteLibraryFile(category, file) {
  const title = file.entry?.title || file.filename;
  const prompt = file.present
    ? `Delete "${title}"? This removes the file and its library entry.`
    : `Forget "${title}"? The file is already gone; this only drops the record, so the work will be downloaded again next time you select it.`;
  if (!confirm(prompt)) return;
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
