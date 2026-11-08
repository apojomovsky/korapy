// Entry module: routing, view mounting, the poll loop.

import {
  BOARD_PAGE_SIZE,
  TAIL_POLL_MS,
  VERSION_CHECK_MS,
  capEvents,
  capJournal,
  forgetToken,
  hasTicket,
  isSupportedApi,
  isSupportedDocument,
  loadBoard,
  loadCachedSnapshot,
  loadConfig,
  loadEvents,
  loadJournalLast,
  loadJournalOlder,
  loadProjects,
  loadSnapshot,
  loadTail,
  loadToken,
  loadVersion,
  nextBoardDelay,
  nextDelay,
  nextLogDelay,
  olderStart,
  projectNames,
  resolveAgent,
  saveCachedSnapshot,
  saveToken,
  snapshotProjects,
} from "./api.js";
import { canonicalJson, formatAge, stripVolatile } from "./fmt.js";
import {
  hideToken,
  renderBoardRows,
  renderBoardShell,
  renderConnection,
  renderDetail,
  renderDetailMissing,
  renderLog,
  renderOverview,
  renderShell,
  renderStopped,
  renderStrip,
  renderTail,
  renderTailButtons,
  renderTailFailed,
  renderTailLoading,
  renderTailMissing,
  renderTailPaused,
  setAgentFilter,
  showToken,
  toast,
} from "./views.js";

// The document schema this shell renders. Newer documents proceed
// (additive); older or missing ones render as unsupported. Pinned
// by the suite against the server's own schema number.
const EXPECTED_SCHEMA_VERSION = 3;

let seenMs = null;
let failures = 0;
let timer = null;
let lastDoc = null;
let lastFingerprint = null;
let lastProject = null;
let lastNotice = null;
let lastAgentKey = null;
let lastVersion = null;
let lastVersionCheckMs = 0;
let versionCheckForced = false;
let outdated = false;
// Board list state: applied filters and the page offset. The shell
// inputs are rebuilt only on mount, so drafts typed between polls
// are never clobbered; applying resets to the first page.
let boardFilters = { repo: null, state: null, label: null };
let boardOffset = 0;
let liveToken = null;
// Navigation generation: a poll that resolves after a route switch
// must neither draw nor reschedule, so each navigation retires the
// loop it interrupted instead of fighting it over the one timer.
let pollGen = 0;
// The tail poll is its own loop beside the snapshot poll: one timer,
// its own failure count, and operator state that survives polls.
let tailTimer = null;
let tailFailures = 0;
let tailPaused = false;
let tailFollow = true;
let lastTailKey = null;
let tailTarget = null;

// Log view memory, one project at a time. The journal accumulates
// oldest-first from the newest window backwards; polls replace the
// newest window in place instead of growing the DOM, and replace
// the whole accumulation when new entries arrive.
let logKind = null;
let logKey = null;
let logJournal = [];
let logJournalStart = 0;
let logJournalTotal = 0;
let logHistory = false;
let logFp = null;
let lastLogView = null;

function helpFor(mode) {
  if (mode === "hub") {
    return "Paste the reader token for this hub, minted with the hub add-reader verb.";
  }
  return "Paste the API token for this server. It lives in the host token file; mint one with korapy serve --new-token.";
}

function routeFromHash() {
  const hash = window.location.hash || "#/";
  if (hash === "#/") {
    return { name: "overview", project: null, notice: null, key: null };
  }
  if (hash === "#/board") {
    return { name: "board", project: null, notice: null, key: null };
  }
  if (hash.startsWith("#/?")) {
    let project = null;
    try {
      const picked = new URLSearchParams(hash.slice(3)).get("project");
      project = picked === null || picked === "" ? null : picked;
    } catch {
      project = null;
    }
    return { name: "overview", project, notice: null, key: null };
  }
  if (hash === "#/agent" || hash.startsWith("#/agent?")) {
    let key = null;
    try {
      const query = hash.startsWith("#/agent?") ? hash.slice("#/agent?".length) : "";
      const params = new URLSearchParams(query);
      const id = params.get("id");
      if (id !== null && id !== "") {
        key = { kind: "id", id };
      } else {
        const repo = params.get("repo");
        const number = params.get("number");
        if (repo !== null && repo !== "" && number !== null && number !== "") {
          const rawRole = params.get("role");
          key = {
            kind: "ticket",
            repo,
            number,
            role: rawRole === null || rawRole === "" ? null : rawRole,
          };
        }
      }
    } catch {
      key = null;
    }
    if (key !== null) {
      return { name: "agent", project: null, notice: null, key };
    }
  }
  if (hash === "#/log") {
    return { name: "log", project: null, notice: null, key: null };
  }
  if (hash.startsWith("#/log?")) {
    let project = null;
    try {
      const picked = new URLSearchParams(hash.slice(6)).get("project");
      project = picked === null || picked === "" ? null : picked;
    } catch {
      project = null;
    }
    return { name: "log", project, notice: null, key: null };
  }
  return {
    name: "unknown",
    project: null,
    notice: "Unknown view " + hash + ", showing the overview.",
    key: null,
  };
}

function logViewKey(route) {
  return String(route.project || "") + "\n" + String(logKind || "");
}

// The row a detail key names, drawn, with the row for the tail poll.
// A null row draws the miss notice; a throw leaves every cache and
// the live page untouched for the caller to fall through.
function drawAgentDetail(doc, key, nowMs) {
  const row = resolveAgent(doc, key);
  if (row === null) {
    renderDetailMissing();
    return null;
  }
  renderDetail(row, { nowMs });
  return row;
}

// The log route re-renders from its own cache beside the snapshot
// one: a hashchange redraws even when the snapshot is unchanged.
// Kept beside renderCurrent so the overview cache-order pin below
// reads one draw path, not two interleaved ones.
function renderCurrentLog(route) {
  if (lastLogView === null || lastLogView.key !== logViewKey(route)) {
    return false;
  }
  try {
    renderLog(lastLogView.view, { nowMs: Date.now(), notice: route.notice });
  } catch {
    return false;
  }
  // The cache updates only after a clean draw, so a throw above
  // retries on the next poll instead of skipping.
  lastProject = route.project;
  lastNotice = route.notice;
  lastAgentKey = null;
  stopTail();
  return true;
}

function renderCurrent(config, token) {
  if (outdated || lastDoc === null) {
    stopTail();
    return false;
  }
  const route = routeFromHash();
  if (route.name === "log") {
    return renderCurrentLog(route);
  }
  if (route.name === "agent") {
    let row = null;
    try {
      row = drawAgentDetail(lastDoc, route.key, Date.now());
    } catch {
      return false;
    }
    // The cache updates only after a clean draw, so a throw above
    // retries on the next poll instead of skipping.
    lastAgentKey = canonicalJson(route.key);
    lastProject = null;
    lastNotice = null;
    syncTailPoll(config, token, row);
    return true;
  }
  try {
    renderOverview(lastDoc, { project: route.project, nowMs: Date.now(), notice: route.notice });
    restoreSelection();
  } catch {
    return false;
  }
  // The cache updates only after a clean draw, so a throw above
  // retries on the next poll instead of skipping.
  lastProject = route.project;
  lastNotice = route.notice;
  lastAgentKey = null;
  stopTail();
  return true;
}

async function checkVersion(config, token, nowMs) {
  if (!versionCheckForced && lastVersion !== null && nowMs - lastVersionCheckMs < VERSION_CHECK_MS) {
    return true;
  }
  versionCheckForced = false;
  let reply = null;
  try {
    reply = await loadVersion(token);
  } catch {
    reply = null;
  }
  if (reply && reply.status === 200 && reply.data) {
    lastVersion = reply.data;
    lastVersionCheckMs = nowMs;
    if (!isSupportedApi(reply.data, config.api)) {
      outdated = true;
      enterStopped("This dashboard is out of date for this server, reload after upgrading.");
      renderConnection({ live: true, seenMs: nowMs });
      return false;
    }
    // A recheck after an upgrade resumes rendering on the next poll.
    outdated = false;
    return true;
  }
  // Any other outcome leaves the last version (or none) in place;
  // the snapshot poll below decides the failure branch.
  return true;
}

// Entering the stopped gate drops the compare cache, so the
// compare-and-skip path cannot skip the redraw when the version
// matches again and an empty page claims to be live.
function enterStopped(message) {
  lastDoc = null;
  lastFingerprint = null;
  lastAgentKey = null;
  lastLogView = null;
  logFp = null;
  stopTail();
  renderStopped(message);
}

function shellFor(config, doc) {
  return {
    mode: config.mode,
    api: config.api,
    version: lastVersion,
    seenMs,
    serverNowMs:
      typeof doc.now === "number"
        ? doc.now * 1000
        : typeof doc.fleet_header === "object" &&
            doc.fleet_header !== null &&
            typeof doc.fleet_header.now === "number"
          ? doc.fleet_header.now * 1000
          : null,
  };
}

// The log view behind one hash route: the project list unions the
// registered names with the set derived from activity; per-card
// failures read as card text with a live connection, since the
// snapshot heartbeat is the freshness signal, not each stack. The
// snapshot arrives from the shared poll below, so this draws the
// stacks only; auth, failure and staleness stay in one place.
async function tickLog(config, token, route, doc) {
  const gen = pollGen;
  lastDoc = doc;
  lastFingerprint = canonicalJson(stripVolatile(doc));
  lastAgentKey = null;
  try {
    renderStrip(doc, { nowMs: Date.now() });
  } catch {
    // The strip is a courtesy; the log stacks below carry the view.
  }
  try {
    saveCachedSnapshot(doc);
  } catch {
    // A refused write keeps the page working for this load only.
  }
  let regReply = null;
  try {
    regReply = await loadProjects(token);
  } catch {
    regReply = null;
  }
  if (gen !== pollGen) {
    return;
  }
  let registered = null;
  let derivedOnly = false;
  if (
    regReply &&
    regReply.status === 200 &&
    regReply.data &&
    Array.isArray(regReply.data.projects)
  ) {
    registered = regReply.data.projects;
  } else {
    derivedOnly = true;
  }
  const choices = projectNames(registered, snapshotProjects(doc));
  const key = logViewKey(route);
  if (key !== logKey) {
    logKey = key;
    logJournal = [];
    logJournalStart = 0;
    logJournalTotal = 0;
    logHistory = false;
    logFp = null;
  }
  if (route.project === null) {
    const view = {
      project: null,
      choices,
      derivedOnly,
      unknown: false,
      eventsState: "missing",
      events: [],
      eventsTotal: 0,
      journalState: "missing",
      journal: [],
      journalTotal: 0,
      journalStart: 0,
      kind: logKind,
      history: false,
    };
    lastLogView = { key, view };
    lastProject = route.project;
    lastNotice = route.notice;
    renderLog(view, { nowMs: seenMs, notice: route.notice });
    return;
  }
  let eventsReply = null;
  try {
    eventsReply = await loadEvents(route.project, token);
  } catch {
    eventsReply = null;
  }
  if (gen !== pollGen) {
    return;
  }
  // The kind comes from the route's own tuple and the paging is
  // computed, so a 400 from either stack reads as a bad project
  // name: the project may have been removed since the list loaded.
  let journalReply = null;
  if (!logHistory) {
    try {
      journalReply = await loadJournalLast(route.project, logKind, token);
    } catch {
      journalReply = null;
    }
    if (gen !== pollGen) {
      return;
    }
  }
  if (
    (eventsReply && eventsReply.status === 400) ||
    (journalReply && journalReply.status === 400)
  ) {
    const view = {
      project: route.project,
      choices,
      derivedOnly,
      unknown: true,
      eventsState: "unknown",
      events: [],
      eventsTotal: 0,
      journalState: "unknown",
      journal: [],
      journalTotal: 0,
      journalStart: 0,
      kind: logKind,
      history: false,
    };
    lastLogView = { key, view };
    lastProject = route.project;
    lastNotice = route.notice;
    renderLog(view, { nowMs: seenMs, notice: route.notice });
    return;
  }
  let eventsState = "ready";
  let events = [];
  let eventsTotal = 0;
  if (eventsReply && eventsReply.status === 200 && eventsReply.data) {
    const rows = Array.isArray(eventsReply.data.events) ? eventsReply.data.events : [];
    eventsTotal = rows.length;
    events = capEvents(rows);
  } else {
    eventsState = "failed";
  }
  let journalState = "ready";
  if (journalReply && journalReply.status === 200 && journalReply.data) {
    const entries = Array.isArray(journalReply.data.entries) ? journalReply.data.entries : [];
    logJournalTotal =
      typeof journalReply.data.total === "number" ? journalReply.data.total : entries.length;
    logJournal = entries;
    // The window reply carries its own cursor (pos + len); derive
    // start from it so entries arriving between the probe and the
    // window cannot mislabel the rendered range.
    const rawCursor = journalReply.data.cursor;
    const parsedCursor =
      typeof rawCursor === "string"
        ? Number.parseInt(rawCursor, 10)
        : typeof rawCursor === "number"
          ? rawCursor
          : NaN;
    logJournalStart = Number.isFinite(parsedCursor)
      ? Math.max(0, parsedCursor - entries.length)
      : Math.max(0, logJournalTotal - entries.length);
  } else if (!logHistory) {
    journalState = "failed";
  }
  const view = {
    project: route.project,
    choices,
    derivedOnly,
    unknown: false,
    eventsState,
    events,
    eventsTotal,
    journalState,
    journal: logJournal,
    journalTotal: logJournalTotal,
    journalStart: logJournalStart,
    kind: logKind,
    history: logHistory,
  };
  const fingerprint =
    String(logKind || "") +
    "|" +
    canonicalJson(stripVolatile(eventsReply ? eventsReply.data : null)) +
    "|" +
    canonicalJson(choices) +
    "|" +
    (logHistory ? "history:" + canonicalJson(logJournal) : canonicalJson(stripVolatile(logJournal)));
  if (
    fingerprint !== logFp ||
    route.project !== lastProject ||
    route.notice !== lastNotice
  ) {
    lastLogView = { key, view };
    renderLog(view, { nowMs: seenMs, notice: route.notice });
    logFp = fingerprint;
    lastProject = route.project;
    lastNotice = route.notice;
  }
}

function stopTail() {
  if (tailTimer !== null) {
    window.clearTimeout(tailTimer);
    tailTimer = null;
  }
  lastTailKey = null;
  tailTarget = null;
}

function scheduleTail(config, token, delayMs) {
  if (tailTimer !== null) {
    window.clearTimeout(tailTimer);
    tailTimer = null;
  }
  if (document.hidden || tailPaused || outdated) {
    return;
  }
  tailTimer = window.setTimeout(() => {
    void tickTail(config, token);
  }, delayMs);
}

function tailAtBottom() {
  const pane = document.getElementById("tail");
  return pane.scrollHeight - pane.scrollTop - pane.clientHeight <= 8;
}

// Keeps the tail poll on its ticket: a new ticket clears the pane
// and reloads at once, the same ticket re-arms a dead timer (after
// a visibility return or a resume), anything else stops the loop.
function syncTailPoll(config, token, row) {
  if (row !== null && hasTicket(row)) {
    const target = { repo: String(row.repo), number: String(row.number) };
    const key = target.repo + "\n" + target.number;
    tailTarget = target;
    if (key !== lastTailKey) {
      lastTailKey = key;
      tailFailures = 0;
      renderTailLoading();
    }
    if (tailTimer === null && !tailPaused && !document.hidden && !outdated) {
      void tickTail(config, token);
    }
    return;
  }
  stopTail();
}

async function tickTail(config, token) {
  tailTimer = null;
  if (tailTarget === null || outdated || lastDoc === null) {
    return;
  }
  const target = tailTarget;
  try {
    let reply = null;
    try {
      reply = await loadTail(token, target.repo, target.number);
    } catch {
      reply = null;
    }
    if (target !== tailTarget) {
      return;
    }
    if (reply && reply.status === 200) {
      tailFailures = 0;
      const pinned = tailFollow && tailAtBottom();
      renderTail(reply.text === null ? "" : reply.text);
      if (pinned) {
        const pane = document.getElementById("tail");
        pane.scrollTop = pane.scrollHeight;
      }
    } else if (reply && reply.status === 404) {
      // No session file yet (or any more): an answer, not a failure.
      tailFailures = 0;
      renderTailMissing();
    } else {
      tailFailures += 1;
      renderTailFailed();
    }
  } catch {
    tailFailures += 1;
    renderTailFailed();
  } finally {
    if (target === tailTarget && tailTarget !== null) {
      scheduleTail(config, token, tailFailures === 0 ? TAIL_POLL_MS : nextDelay(tailFailures));
    }
  }
}

function bindTailButtons(config, getToken) {
  const pause = document.getElementById("tail-pause");
  const follow = document.getElementById("tail-follow");
  const pane = document.getElementById("tail");
  renderTailButtons(tailPaused, tailFollow);
  pause.addEventListener("click", () => {
    tailPaused = !tailPaused;
    renderTailButtons(tailPaused, tailFollow);
    if (tailPaused) {
      if (tailTimer !== null) {
        window.clearTimeout(tailTimer);
        tailTimer = null;
      }
      renderTailPaused(true);
    } else {
      renderTailPaused(false);
      void tickTail(config, getToken());
    }
  });
  follow.addEventListener("click", () => {
    tailFollow = !tailFollow;
    renderTailButtons(tailPaused, tailFollow);
    if (tailFollow) {
      pane.scrollTop = pane.scrollHeight;
    }
  });
  pane.addEventListener("scroll", () => {
    // Scrolling up disengages follow; only the button re-engages it.
    if (tailFollow && !tailAtBottom()) {
      tailFollow = false;
      renderTailButtons(tailPaused, tailFollow);
    }
  });
}

async function tick(config, token) {
  const gen = pollGen;
  const nowMs = Date.now();
  try {
    let versionOk = true;
    try {
      versionOk = await checkVersion(config, token, nowMs);
    } catch {
      versionOk = true;
    }
    if (gen !== pollGen) {
      return;
    }
    if (!versionOk) {
      return;
    }
    let reply = null;
    try {
      reply = await loadSnapshot(token);
    } catch {
      reply = null;
    }
    if (gen !== pollGen) {
      return;
    }
    if (reply && reply.status === 200 && reply.data) {
      seenMs = Date.now();
      failures = 0;
      hideToken();
      saveCachedSnapshot(reply.data);
      renderShell(shellFor(config, reply.data));
      if (!isSupportedDocument(reply.data, EXPECTED_SCHEMA_VERSION)) {
        const value =
          reply.data && typeof reply.data.schema_version === "number"
            ? String(reply.data.schema_version)
            : "missing";
        enterStopped(
          "Unsupported document schema " + value + ": reload after upgrading.",
        );
      } else {
        const route = routeFromHash();
        if (route.name === "log") {
          await tickLog(config, token, route, reply.data);
          if (gen !== pollGen) {
            return;
          }
        } else {
          const fingerprint = canonicalJson(stripVolatile(reply.data));
          const agentKeyText = route.name === "agent" ? canonicalJson(route.key) : null;
          if (
            fingerprint !== lastFingerprint ||
            route.project !== lastProject ||
            route.notice !== lastNotice ||
            agentKeyText !== lastAgentKey
          ) {
            // The cache updates only after a clean draw, so a throw
            // below retries on the next poll instead of skipping.
            if (route.name === "agent") {
              drawAgentDetail(reply.data, route.key, seenMs);
            } else {
              renderOverview(reply.data, {
                project: route.project,
                nowMs: seenMs,
                notice: route.notice,
              });
              restoreSelection();
            }
            lastDoc = reply.data;
            lastFingerprint = fingerprint;
            lastProject = route.project;
            lastNotice = route.notice;
            lastAgentKey = agentKeyText;
          }
        }
        syncTailPoll(
          config,
          token,
          route.name === "agent" ? resolveAgent(reply.data, route.key) : null,
        );
      }
      renderConnection({ live: true, seenMs });
    } else if (reply && reply.status === 401) {
      stopTail();
      showToken({
        message: "This server needs a token.",
        help: helpFor(config.mode),
      });
      if (seenMs !== null) {
        renderConnection({ live: false, seenMs });
      }
    } else {
      // Any other status, a status-0 network failure, or a throw:
      // count the backoff, show stale-with-age when a last good poll
      // exists (or the never-connected state when none does).
      failures += 1;
      renderConnection({ live: false, seenMs });
    }
  } catch {
    // A render throw (a new server shape, an unexpected null) must
    // never kill the loop: renders swap fully built trees, so the
    // last good data stays in place under its age banner; count the
    // failure and say the draw failed.
    failures += 1;
    renderConnection({ live: false, seenMs });
    const note = document.getElementById("notice");
    note.hidden = false;
    note.textContent = "";
    note.appendChild(document.createTextNode("Could not draw this update; the next poll retries."));
  } finally {
    if (gen === pollGen) {
      schedule(config, token);
    }
  }
}

function schedule(config, token) {
  if (timer !== null) {
    window.clearTimeout(timer);
  }
  if (document.hidden) {
    return;
  }
  const route = routeFromHash();
  const delay = route.name === "log" ? nextLogDelay(failures) : nextDelay(failures);
  timer = window.setTimeout(() => {
    void tick(config, token);
  }, delay);
}

// The selected agent row for keyboard movement. An index, not a node:
// the poll loop rebuilds the tree and a kept node would go stale.
let selectedRow = -1;

function agentRowNodes() {
  return [...document.querySelectorAll("#agents table.agents tbody tr")];
}

function selectRow(index) {
  const rows = agentRowNodes();
  for (const row of rows) {
    row.classList.remove("selected");
  }
  if (rows.length === 0) {
    selectedRow = -1;
    return;
  }
  selectedRow = Math.max(0, Math.min(index, rows.length - 1));
  rows[selectedRow].classList.add("selected");
  rows[selectedRow].scrollIntoView({ block: "nearest" });
}

// The poll loop rebuilds the agents table on every overview render,
// so the keyboard selection is re-applied to the row at the kept
// index afterwards. No scrolling: a poll must never move the page.
function restoreSelection() {
  const rows = agentRowNodes();
  for (const row of rows) {
    row.classList.remove("selected");
  }
  if (selectedRow < 0 || selectedRow >= rows.length) {
    selectedRow = rows.length === 0 ? -1 : Math.max(0, Math.min(selectedRow, rows.length - 1));
    if (selectedRow < 0) {
      return;
    }
  }
  rows[selectedRow].classList.add("selected");
}

function openSelectedRow() {
  const rows = agentRowNodes();
  const row = rows[selectedRow];
  const link = row ? row.querySelector("a") : null;
  if (link !== null && link !== undefined) {
    const href = link.getAttribute("href");
    if (href !== null && href.startsWith("#/")) {
      window.location.hash = href;
    }
  }
}

function toggleHelp() {
  const panel = document.getElementById("help");
  if (panel !== null) {
    panel.hidden = !panel.hidden;
  }
}

// The filter box, row clicks and the copy-link button, one delegated
// listener that outlives the poll rebuilds. Typing never loses focus:
// the rebuild re-applies the filter text and focus returns to it.
function bindViewActions(config, getToken) {
  document.getElementById("view").addEventListener("input", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement) || target.id !== "agent-search") {
      return;
    }
    setAgentFilter(target.value);
    if (routeFromHash().name === "overview" && lastDoc !== null && !outdated) {
      renderCurrent(config, getToken());
      const box = document.getElementById("agent-search");
      if (box !== null) {
        box.focus();
        box.setSelectionRange(box.value.length, box.value.length);
      }
    }
  });
  // The in-card project picker navigates by hash, so a change lands
  // on the same overview route the pills used to link to.
  document.getElementById("view").addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLElement) || target.id !== "project-select") {
      return;
    }
    const value = target.value;
    if (typeof value === "string" && value.startsWith("#")) {
      window.location.hash = value;
    }
  });
  document.getElementById("view").addEventListener("click", (event) => {
    const target = event.target instanceof HTMLElement ? event.target : null;
    if (target === null) {
      return;
    }
    const copy = target.closest("[data-copy-link]");
    if (copy !== null) {
      void (async () => {
        try {
          await window.navigator.clipboard.writeText(window.location.href);
          toast("link copied");
        } catch {
          toast("copy failed: select the address bar instead");
        }
      })();
      return;
    }
    const row = target.closest("#agents table.agents tbody tr");
    if (row !== null) {
      selectRow(agentRowNodes().indexOf(row));
    }
  });
}

function bindKeys() {
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    const typing =
      target instanceof HTMLInputElement ||
      target instanceof HTMLTextAreaElement ||
      (target instanceof HTMLElement && target.isContentEditable);
    if (event.key === "?" && !typing) {
      event.preventDefault();
      toggleHelp();
      return;
    }
    if (typing) {
      return;
    }
    if (event.key === "/") {
      const box = document.getElementById("agent-search");
      if (box !== null) {
        event.preventDefault();
        box.focus();
      }
    } else if (event.key === "j") {
      selectRow(selectedRow + 1);
    } else if (event.key === "k") {
      selectRow(selectedRow - 1);
    } else if (event.key === "Enter") {
      openSelectedRow();
    } else if (event.key === "Escape") {
      const panel = document.getElementById("help");
      if (panel !== null && !panel.hidden) {
        panel.hidden = true;
      }
    }
  });
  document.getElementById("help-close").addEventListener("click", () => {
    document.getElementById("help").hidden = true;
  });
}

// The kind chips and paging buttons are re-created on every draw,
// so one delegated listener on the view outlives them all.
function bindLogView(getToken, pollNow) {
  document.getElementById("view").addEventListener("click", (event) => {
    const target =
      event.target instanceof HTMLElement
        ? event.target.closest("[data-log-kind],[data-log-older],[data-log-latest]")
        : null;
    if (!(target instanceof HTMLElement)) {
      return;
    }
    if (target.hasAttribute("data-log-kind")) {
      const picked = target.getAttribute("data-log-kind");
      logKind = picked === null || picked === "" ? null : picked;
      logHistory = false;
      pollNow(getToken());
      return;
    }
    if (target.hasAttribute("data-log-latest")) {
      logJournal = [];
      logJournalStart = 0;
      logJournalTotal = 0;
      logHistory = false;
      logFp = null;
      pollNow(getToken());
      return;
    }
    if (target.hasAttribute("data-log-older")) {
      pollOlder(getToken());
    }
  });
}

async function pollOlder(token) {
  const route = routeFromHash();
  if (route.name !== "log" || route.project === null) {
    return;
  }
  let reply = null;
  try {
    reply = await loadJournalOlder(route.project, logKind, logJournalStart, token);
  } catch {
    reply = null;
  }
  if (!(reply && reply.status === 200 && reply.data && Array.isArray(reply.data.entries))) {
    // The snapshot poll is the freshness signal; a failed older
    // page reads as the journal card's retry text only, never as a
    // disconnected banner.
    const prior = lastLogView ? lastLogView.view : null;
    if (prior !== null && !prior.unknown && prior.project !== null) {
      const next = { ...prior, journalState: "failed" };
      lastLogView = { key: lastLogView.key, view: next };
      try {
        renderLog(next, { nowMs: Date.now(), notice: lastNotice });
      } catch {
        // A failed retry draw keeps the last good page in place.
      }
    }
    return;
  }
  const entries = reply.data.entries;
  if (entries.length === 0) {
    return;
  }
  const start = olderStart(logJournalStart);
  const overlap = Math.max(0, start + entries.length - logJournalStart);
  const capped = capJournal(entries.concat(logJournal.slice(overlap)), start);
  logJournal = capped.entries;
  logJournalStart = capped.start;
  if (typeof reply.data.total === "number") {
    logJournalTotal = reply.data.total;
  }
  logHistory = true;
  logFp = null;
  const view = lastLogView ? lastLogView.view : null;
  if (view !== null && !view.unknown && view.project !== null) {
    const next = { ...view, journal: logJournal, journalStart: logJournalStart, journalTotal: logJournalTotal, history: true };
    lastLogView = { key: lastLogView.key, view: next };
    try {
      renderLog(next, { nowMs: Date.now(), notice: lastNotice });
    } catch {
      failures += 1;
      renderConnection({ live: false, seenMs });
    }
  }
}

function boardInput(id) {
  const node = document.getElementById(id);
  const value = node === null ? "" : node.value.trim();
  return value === "" ? null : value;
}

// The board mount: shell once, then rows per poll. Listeners close
// over the config but read the live token, so a token saved while
// the board is mounted applies to the next poll without a reload.
function mountBoard(config) {
  renderBoardShell(boardFilters);
  document.getElementById("board-filters").addEventListener("submit", (event) => {
    event.preventDefault();
    boardFilters = {
      repo: boardInput("board-repo"),
      state: boardInput("board-state"),
      label: boardInput("board-label"),
    };
    boardOffset = 0;
    failures = 0;
    void boardTick(config, liveToken);
  });
  document.getElementById("board-prev").addEventListener("click", () => {
    boardOffset = Math.max(0, boardOffset - BOARD_PAGE_SIZE);
    failures = 0;
    void boardTick(config, liveToken);
  });
  document.getElementById("board-next").addEventListener("click", () => {
    boardOffset += BOARD_PAGE_SIZE;
    failures = 0;
    void boardTick(config, liveToken);
  });
}

async function boardTick(config, token) {
  const gen = pollGen;
  const nowMs = Date.now();
  try {
    let versionOk = true;
    try {
      versionOk = await checkVersion(config, token, nowMs);
    } catch {
      versionOk = true;
    }
    if (gen !== pollGen) {
      return;
    }
    if (!versionOk) {
      return;
    }
    let reply = null;
    try {
      reply = await loadBoard(token, {
        repo: boardFilters.repo,
        state: boardFilters.state,
        label: boardFilters.label,
        limit: BOARD_PAGE_SIZE,
        offset: boardOffset,
      });
    } catch {
      reply = null;
    }
    if (gen !== pollGen) {
      return;
    }
    if (reply && reply.status === 200 && reply.data) {
      seenMs = Date.now();
      failures = 0;
      hideToken();
      renderShell({
        mode: config.mode,
        api: config.api,
        version: lastVersion,
        seenMs,
        serverNowMs: typeof reply.data.now === "number" ? reply.data.now * 1000 : null,
      });
      const boardTotal = typeof reply.data.total === "number" ? reply.data.total : boardOffset;
      const lastPageStart = Math.max(0, (Math.ceil(boardTotal / BOARD_PAGE_SIZE) - 1) * BOARD_PAGE_SIZE);
      if (boardOffset > lastPageStart) {
        boardOffset = lastPageStart;
      }
      renderBoardRows(reply.data, { limit: BOARD_PAGE_SIZE, offset: boardOffset, nowMs: seenMs });
      renderConnection({ live: true, seenMs });
    } else if (reply && reply.status === 401) {
      showToken({
        message: "This server needs a token.",
        help: helpFor(config.mode),
      });
      if (seenMs !== null) {
        renderConnection({ live: false, seenMs });
      }
    } else {
      // Any other status, a status-0 network failure, or a throw:
      // count the backoff, show stale-with-age when a last good poll
      // exists (or the never-connected state when none does).
      failures += 1;
      renderConnection({ live: false, seenMs });
    }
  } catch {
    // A render throw (a new server shape, an unexpected null) must
    // never kill the loop: count it, keep the last good data under
    // its age banner, and say the draw failed.
    failures += 1;
    renderConnection({ live: false, seenMs });
    const note = document.getElementById("notice");
    note.hidden = false;
    note.textContent = "";
    note.appendChild(document.createTextNode("Could not draw the board update; the next poll retries."));
  } finally {
    if (gen === pollGen) {
      scheduleBoard(config, token);
    }
  }
}

function scheduleBoard(config, token) {
  if (timer !== null) {
    window.clearTimeout(timer);
  }
  if (document.hidden) {
    return;
  }
  timer = window.setTimeout(() => {
    void boardTick(config, token);
  }, nextBoardDelay(failures));
}

function currentTick(config, token) {
  if (routeFromHash().name === "board") {
    void boardTick(config, token);
  } else {
    void tick(config, token);
  }
}

function bindToken(config, setToken) {
  const save = document.getElementById("token-save");
  const forget = document.getElementById("token-forget");
  const input = document.getElementById("token-input");
  save.addEventListener("click", async () => {
    const value = input.value.trim();
    if (!value) {
      showToken({ message: "Paste a token first." });
      return;
    }
    let reply = null;
    try {
      reply = await loadVersion(value);
    } catch {
      reply = null;
    }
    if (reply && reply.status === 200) {
      const kept = saveToken(value);
      setToken(value);
      input.value = "";
      hideToken();
      toast("token saved");
      failures = 0;
      // The tail timer chain holds its own token closure: drop it so
      // the tick below re-arms the tail on the pasted token.
      stopTail();
      await currentTick(config, value);
      if (!kept) {
        showToken({
          message:
            "This device cannot keep this token, so it works for this page only. Paste it again after a reload.",
        });
      }
    } else if (reply && reply.status === 401) {
      showToken({ message: "Wrong token: this server answered 401." });
    } else {
      showToken({ message: "Cannot reach the API. Showing nothing yet." });
    }
  });
  forget.addEventListener("click", () => {
    // A private-mode browser can refuse the remove: reloading then
    // would resurrect the token through loadToken, so a failed
    // forget says so on screen instead of reloading.
    if (forgetToken()) {
      setToken(null);
      input.value = "";
      window.location.reload();
    } else {
      showToken({
        message:
          "This device cannot forget this token, so it stays saved here. Clear this site's storage by hand to remove it.",
      });
    }
  });
  document.getElementById("token-btn").addEventListener("click", () => {
    const panel = document.getElementById("token");
    panel.hidden = !panel.hidden;
  });
}

async function boot() {
  let configReply = null;
  try {
    configReply = await loadConfig();
  } catch {
    configReply = null;
  }
  if (!configReply || configReply.status !== 200 || !configReply.data) {
    // The server is down at first load: say so and retry on the same
    // backoff as polls, instead of stranding the page on "connecting".
    failures += 1;
    renderConnection({ live: false, seenMs });
    timer = window.setTimeout(() => {
      void boot();
    }, nextDelay(failures));
    return;
  }
  const config = configReply.data;
  let token = loadToken();
  liveToken = token;
  const route = routeFromHash();
  // Instant restart: the last good snapshot renders at once under a
  // saved-copy header; the first good poll replaces it. The compare
  // cache stays empty so that poll always redraws and clears the note.
  try {
    const cached = loadCachedSnapshot();
    if (
      cached &&
      cached.data &&
      isSupportedDocument(cached.data, EXPECTED_SCHEMA_VERSION) &&
      (route.name === "overview" || route.name === "unknown")
    ) {
      const cachedAt = typeof cached.at === "number" ? cached.at : Date.now();
      lastDoc = cached.data;
      renderShell(shellFor(config, cached.data));
      renderOverview(cached.data, {
        project: route.project,
        nowMs: Date.now(),
        notice:
          "saved copy, " +
          formatAge(cachedAt, Date.now()) +
          " old; live data replaces it on the first good poll.",
      });
      restoreSelection();
      lastProject = route.project;
    }
  } catch {
    // A corrupt cache reads as no cache: the first poll draws fresh.
  }
  if (route.name === "unknown") {
    renderShell({ mode: config.mode, api: config.api, version: null, seenMs: Date.now() });
  }
  if (route.name === "board") {
    mountBoard(config);
  }
  bindToken(config, (value) => {
    token = value;
    liveToken = value;
  });
  bindTailButtons(config, () => token);
  bindViewActions(config, () => token);
  bindKeys();
  bindLogView(
    () => token,
    (current) => {
      void tick(config, current);
    },
  );
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      versionCheckForced = true;
      void currentTick(config, token);
    }
  });
  window.addEventListener("hashchange", () => {
    failures = 0;
    pollGen += 1;
    if (timer !== null) {
      window.clearTimeout(timer);
      timer = null;
    }
    if (routeFromHash().name === "board") {
      stopTail();
      mountBoard(config);
      void boardTick(config, token);
    } else if (!renderCurrent(config, token)) {
      void tick(config, token);
    }
  });
  await currentTick(config, token);
}

void boot();
