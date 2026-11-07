// View renderers. Untrusted strings enter the DOM as text only,
// never as markup; this module sets textContent and nothing else.
// Bar geometry rides on SVG attributes, never CSS: the policy
// allows no inline style, so a width can only be an attribute.

import {
  FINISHED_RETENTION_MS,
  LOG_EVENTS_CAP,
  LOG_JOURNAL_CAP,
  LOG_KINDS,
  agentKey,
  detailHash,
  hasTicket,
  isErrorSection,
  retainedFinished,
  snapshotProjects,
} from "./api.js";
import {
  bandFor,
  clamp01,
  cleanText,
  exitWord,
  formatAge,
  formatBytes,
  formatCpu,
  formatLoad,
  formatMem,
  formatMemUsed,
  formatQuotaRead,
  formatResetLine,
  formatRetentionWindow,
  parseEpoch,
  parseIso,
} from "./fmt.js";

// The element namespace for inline charts. A namespace identifier,
// not a fetch: no request ever goes to it.
const SVG_NS = "http://www.w3.org/2000/svg";

const BAR_WIDTH = 120;
const BAR_HEIGHT = 10;

// el() sets attributes unchecked: never pass a tracker href through it
// without a scheme check (W6 must allow http/https only).
export function el(tag, text, attrs) {
  const node = document.createElement(tag);
  if (text !== null && text !== undefined) {
    node.textContent = cleanText(String(text));
  }
  for (const [name, value] of Object.entries(attrs || {})) {
    node.setAttribute(name, value);
  }
  return node;
}

export function renderShell({ mode, api, version, seenMs, serverNowMs }) {
  const modeNode = document.getElementById("mode");
  modeNode.textContent = mode + " mode";
  const apiNode = document.getElementById("api");
  apiNode.textContent = "api " + String(api);
  const line = document.getElementById("status-line");
  const parts = ["serving api " + String(api)];
  if (version && typeof version.schema_version === "number") {
    parts.push("document schema " + String(version.schema_version));
  }
  if (typeof serverNowMs === "number") {
    parts.push("server time " + formatAge(serverNowMs, seenMs));
  }
  line.textContent = "";
  line.appendChild(document.createTextNode(parts.join(", ")));
}

export function renderConnection({ live, seenMs }) {
  const conn = document.getElementById("conn");
  const updated = document.getElementById("updated");
  const view = document.getElementById("view");
  const banner = document.getElementById("banner");
  if (live) {
    conn.textContent = "live " + formatAge(seenMs, Date.now());
    updated.textContent = "";
    banner.hidden = true;
    banner.textContent = "";
    view.classList.remove("stale");
    return;
  }
  if (seenMs === null || seenMs === undefined) {
    // No good poll yet (the server was down at first load): there is
    // no stale data to age, only the retry the poll loop scheduled.
    conn.textContent = "still connecting";
    updated.textContent = "";
    banner.hidden = false;
    banner.textContent = "";
    banner.appendChild(document.createTextNode("Cannot reach the API yet. Retrying."));
    return;
  }
  conn.textContent = "cannot reach the api";
  updated.textContent = "showing data from " + formatAge(seenMs, Date.now());
  banner.hidden = false;
  banner.textContent = "";
  banner.appendChild(
    document.createTextNode(
      "Cannot reach the API. Showing data from " +
        formatAge(seenMs, Date.now()) +
        ".",
    ),
  );
  view.classList.add("stale");
}

export function showToken({ message, help }) {
  document.getElementById("token").hidden = false;
  const msg = document.getElementById("token-msg");
  msg.textContent = "";
  if (message) {
    msg.appendChild(document.createTextNode(cleanText(message)));
  }
  if (help) {
    const helpNode = document.getElementById("token-help");
    helpNode.textContent = "";
    helpNode.appendChild(document.createTextNode(cleanText(help)));
  }
}

export function hideToken() {
  document.getElementById("token").hidden = true;
  document.getElementById("token-msg").textContent = "";
}

function card(title, desc, wrong) {
  const section = document.createElement("section");
  section.setAttribute("class", wrong ? "card error" : "card");
  section.appendChild(el("h2", title));
  if (desc !== null && desc !== undefined && String(desc) !== "") {
    section.appendChild(el("p", String(desc), { class: "desc" }));
  }
  return section;
}

// A failed section: what broke, plus what the operator can do about
// it. Degraded state is shown, never hidden behind a spinner.
function errorCard(title, desc, body, todo) {
  const text = body && typeof body.error === "string" ? body.error : "unreadable";
  const section = card(title, desc, true);
  section.appendChild(el("p", text));
  section.appendChild(el("p", String(todo), { class: "todo" }));
  return section;
}

// One labelled field: the label names the fact, the value states
// it, the help rides as a tooltip so the numbers lead. The help
// stays in the source for the suite to pin, never as visible prose.
function field(label, value, help) {
  const wrap = document.createElement("div");
  wrap.setAttribute("class", "field");
  wrap.setAttribute("title", String(help));
  wrap.appendChild(el("dt", label));
  const shown = document.createElement("dd");
  if (typeof value === "string") {
    shown.appendChild(document.createTextNode(cleanText(value)));
  } else {
    shown.appendChild(value);
  }
  wrap.appendChild(shown);
  const list = document.createElement("dl");
  list.setAttribute("class", "fields");
  list.appendChild(wrap);
  return list;
}

// A percentage meter: an SVG bar plus the number and its state as
// text, so the state reads without color too. The class names the
// state (meter ok, meter low, meter stale); the text says it.
const METER_CLASS = {
  ok: "meter ok",
  low: "meter low",
  high: "meter high",
  stale: "meter stale",
  unknown: "meter unknown",
};

function meter(frac, word, label, markerFrac) {
  const state = METER_CLASS[word] ? word : "unknown";
  const safe = clamp01(frac);
  const pct = safe === null ? null : Math.round(safe * 100);
  const wrap = document.createElement("span");
  wrap.setAttribute("class", METER_CLASS[state]);
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("width", String(BAR_WIDTH));
  svg.setAttribute("height", String(BAR_HEIGHT));
  svg.setAttribute("viewBox", "0 0 " + String(BAR_WIDTH) + " " + String(BAR_HEIGHT));
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("role", "img");
  svg.setAttribute(
    "aria-label",
    label + " " + (pct === null ? "unknown" : String(pct) + "%") + ", " + state,
  );
  const back = document.createElementNS(SVG_NS, "rect");
  back.setAttribute("x", "0");
  back.setAttribute("y", "0");
  back.setAttribute("width", String(BAR_WIDTH));
  back.setAttribute("height", String(BAR_HEIGHT));
  back.setAttribute("fill", "none");
  back.setAttribute("stroke", "currentColor");
  svg.appendChild(back);
  if (pct !== null) {
    const fill = document.createElementNS(SVG_NS, "rect");
    fill.setAttribute("x", "0");
    fill.setAttribute("y", "0");
    fill.setAttribute("width", String(Math.round((pct / 100) * BAR_WIDTH)));
    fill.setAttribute("height", String(BAR_HEIGHT));
    fill.setAttribute("fill", "currentColor");
    svg.appendChild(fill);
  }
  // The share the loop reserved for its agents, drawn as a tick inside
  // the host bar. A second geometry on the same bar, never a second
  // bar: the bar is the host, the tick is the reservation.
  const marker = clamp01(markerFrac);
  if (marker !== null) {
    const tick = document.createElementNS(SVG_NS, "rect");
    const x = Math.min(BAR_WIDTH - 3, Math.round(marker * BAR_WIDTH));
    tick.setAttribute("x", String(x));
    tick.setAttribute("y", "0");
    tick.setAttribute("width", "3");
    tick.setAttribute("height", String(BAR_HEIGHT));
    tick.setAttribute("fill", "var(--bg)");
    tick.setAttribute("stroke", "currentColor");
    tick.setAttribute("stroke-width", "1");
    svg.appendChild(tick);
  }
  wrap.appendChild(svg);
  wrap.appendChild(
    el("span", (pct === null ? "unknown" : String(pct) + "%") + ", " + state),
  );
  return wrap;
}

function age(value, nowMs) {
  return formatAge(parseEpoch(value), nowMs);
}

// The meter state for a provider: a stale reading overrides the
// quota state, so old numbers never wear a healthy color.
function quotaMeterState(info) {
  if (info.stale) {
    return "stale";
  }
  if (info.state === "ok") {
    return "ok";
  }
  if (info.state === "low") {
    return "low";
  }
  if (info.state === "exhausted") {
    return "high";
  }
  return "unknown";
}

// One quota window: its meter, or the reason there is no meter. The
// reset reads as local wall-clock time plus the wait in words.
function quotaWindow(id, window, info, nowMs) {
  const wrap = document.createElement("li");
  wrap.appendChild(el("span", String(id) + " "));
  if (window === null || window === undefined || typeof window !== "object") {
    wrap.appendChild(el("span", "no reading"));
    return wrap;
  }
  const used = typeof window.used_pct === "number" ? window.used_pct : null;
  if (used === null || !Number.isFinite(used)) {
    wrap.appendChild(el("span", "no reading, " + formatResetLine(window.resets_at, nowMs)));
    return wrap;
  }
  wrap.appendChild(meter(used / 100, quotaMeterState(info), "window " + String(id)));
  wrap.appendChild(el("span", " " + formatResetLine(window.resets_at, nowMs)));
  return wrap;
}

// One provider: its state in words, its reading's age, then one row
// per window. A stale provider keeps its last numbers on screen under
// a stale meter, never a zero bar. The stale reason renders the
// snapshot's stale_reason field as-is; the words come from the server.
function quotaProvider(name, info, nowMs) {
  const group = document.createElement("div");
  group.setAttribute("class", info.stale ? "provider stale" : "provider");
  if (info.stale) {
    // The stale badge carries the reading's age, so a stale provider
    // is spotted and dated in one glance, never by color alone.
    const readAt = parseEpoch(info.read_at);
    const badgeAge =
      typeof readAt === "number" ? formatAge(readAt, nowMs) : "never read";
    group.appendChild(el("span", "stale, " + badgeAge, { class: "badge stale" }));
  }
  group.appendChild(el("h3", String(name)));
  const line = document.createElement("p");
  let read = formatQuotaRead(info, nowMs);
  if (info.stale && typeof info.stale_reason === "string" && info.stale_reason !== "") {
    read += ": " + info.stale_reason;
  }
  line.appendChild(document.createTextNode(cleanText(String(info.state) + ", " + read)));
  group.appendChild(line);
  if (typeof info.error === "string" && info.error !== "") {
    group.appendChild(el("p", "reader error: " + info.error));
  }
  const windows = info.windows && typeof info.windows === "object" ? info.windows : null;
  if (windows === null) {
    group.appendChild(el("p", "no reading recorded yet; run korapy quota refresh to take one."));
    return group;
  }
  const ids = Object.keys(windows).sort();
  if (ids.length === 0) {
    group.appendChild(el("p", "no reading recorded yet; run korapy quota refresh to take one."));
    return group;
  }
  const list = document.createElement("ul");
  for (const id of ids) {
    list.appendChild(quotaWindow(id, windows[id], info, nowMs));
  }
  group.appendChild(list);
  return group;
}

function quotaCard(quota, nowMs) {
  const desc = "What the providers last reported. A reading older than its freshness bound counts as stale; refresh it with korapy quota refresh.";
  if (isErrorSection(quota)) {
    return errorCard(
      "quota",
      desc,
      quota,
      "Quota readings could not be collected. Check the host with korapy doctor.",
    );
  }
  const section = card("quota", desc);
  const providers = quota && quota.providers ? quota.providers : {};
  const names = Object.keys(providers).sort();
  if (names.length === 0) {
    section.appendChild(el("p", "no providers configured; add providers to the account pool."));
    return section;
  }
  for (const name of names) {
    section.appendChild(quotaProvider(name, providers[name] || {}, nowMs));
  }
  return section;
}

// One project's charged share in spelled-out units, with measured
// use where the kernel reports it. A null share on a new server
// shape reads as unknown instead of throwing inside the poll loop.
function projectShareLine(share, measured) {
  const info = share !== null && typeof share === "object" ? share : {};
  const charged = [
    info.cpu_m === null || info.cpu_m === undefined ? "CPU unknown" : formatCpu(info.cpu_m),
    info.memory_mb === null || info.memory_mb === undefined
      ? "memory unknown"
      : formatMem(info.memory_mb),
    info.pids === null || info.pids === undefined
      ? "processes unknown"
      : String(info.pids) + " processes",
  ];
  let text = "reserved " + charged.join(", ");
  const got = measured !== null && typeof measured === "object" ? measured : null;
  if (got !== null) {
    const bits = [];
    if (got.cpu_usage_usec !== null && got.cpu_usage_usec !== undefined) {
      bits.push("cpu " + (got.cpu_usage_usec / 1000000).toFixed(1) + " seconds");
    }
    if (got.memory_mb !== null && got.memory_mb !== undefined) {
      bits.push(formatMem(got.memory_mb));
    }
    if (got.pids !== null && got.pids !== undefined) {
      bits.push(String(got.pids) + " processes");
    }
    if (bits.length > 0) {
      text += "; measured " + bits.join(", ");
    }
  }
  return text;
}

// A pressure reading outside the contract degrades to its raw value
// as text, the timestamp formatters' rule, instead of NaN.
function pressureValue(value, digits) {
  const num = Number(value);
  return Number.isFinite(num) ? num.toFixed(digits) : String(value);
}

function pressureText(pressure) {
  const info = pressure && typeof pressure === "object" ? pressure : {};
  const bits = [];
  if (info.load_per_cpu !== null && info.load_per_cpu !== undefined) {
    bits.push("load " + pressureValue(info.load_per_cpu, 2) + "/cpu");
  }
  if (info.mem_available_pct !== null && info.mem_available_pct !== undefined) {
    bits.push("mem " + pressureValue(info.mem_available_pct, 1) + "% avail");
  }
  if (bits.length === 0) {
    return "pressure unknown";
  }
  bits.push(info.saturated ? "saturated" : "ok");
  return bits.join(", ");
}

// One resource dimension: a labelled bar of charged against the host
// budget, with both units spelled out. Without a budget there is no
// bar, only the charged number and the reason. The help rides as a
// tooltip so the numbers lead.
function resourceDim(labelText, abbrTitle, valueText, frac, help) {
  const wrap = document.createElement("div");
  wrap.setAttribute("class", "field");
  wrap.setAttribute("title", String(help));
  const term = document.createElement("dt");
  if (abbrTitle) {
    term.appendChild(el("abbr", labelText, { title: abbrTitle }));
  } else {
    term.appendChild(document.createTextNode(cleanText(labelText)));
  }
  wrap.appendChild(term);
  const shown = document.createElement("dd");
  shown.appendChild(document.createTextNode(cleanText(valueText)));
  if (frac !== null) {
    const barRow = document.createElement("div");
    barRow.appendChild(meter(frac, bandFor(frac), labelText));
    shown.appendChild(barRow);
  }
  wrap.appendChild(shown);
  const list = document.createElement("dl");
  list.setAttribute("class", "fields");
  list.appendChild(wrap);
  return list;
}

// One host gauge: the actual host value as a bar with the reserved
// share as a tick inside. `term` builds the dt node (CPU keeps its
// abbreviation), `help` is the tooltip, `fracText` the numbers line,
// `frac` the bar, `marker` the tick, `reserved` the reservation line.
// Severity rides the bar band; overload itself is the server's
// `saturated` verdict in the attention band, never a second cut here.
function hostGauge(term, help, fracText, frac, label, marker, reserved) {
  const wrap = document.createElement("div");
  wrap.setAttribute("class", "field");
  wrap.setAttribute("title", help);
  wrap.appendChild(term);
  const shown = document.createElement("dd");
  shown.appendChild(document.createTextNode(cleanText(fracText)));
  const barRow = document.createElement("div");
  barRow.appendChild(meter(frac, frac === null ? "unknown" : bandFor(frac), label, marker));
  shown.appendChild(barRow);
  shown.appendChild(document.createTextNode(cleanText(" " + reserved)));
  wrap.appendChild(shown);
  const list = document.createElement("dl");
  list.setAttribute("class", "fields");
  list.appendChild(wrap);
  return list;
}

function cpuTerm() {
  const term = document.createElement("dt");
  term.appendChild(el("abbr", "CPU", { title: "central processing unit" }));
  term.appendChild(document.createTextNode(", host load"));
  return term;
}

function cpuGauge(pressure, charged) {
  const info = pressure && typeof pressure === "object" ? pressure : {};
  const load = typeof info.load_per_cpu === "number" ? info.load_per_cpu : null;
  const ncpu = typeof info.ncpu === "number" ? info.ncpu : null;
  const load1min = typeof info.load_1min === "number" ? info.load_1min : null;
  let marker = null;
  if (
    charged !== null &&
    typeof charged.cpu_m === "number" &&
    typeof ncpu === "number" &&
    ncpu > 0
  ) {
    marker = charged.cpu_m / (ncpu * 1000);
  }
  const budgetCpu =
    typeof charged === "object" && charged !== null ? charged.cpu_m : null;
  return hostGauge(
    cpuTerm(),
    "Host load from the kernel, per core. The tick inside is the processor share the loop reserved for its agents.",
    formatLoad(load1min, ncpu),
    load,
    "host CPU load",
    marker,
    budgetCpu === null || budgetCpu === undefined
      ? "reserved for agents: not reported"
      : "reserved for agents: " + formatCpu(budgetCpu) + " of the host",
  );
}

function memoryGauge(pressure, charged) {
  const info = pressure && typeof pressure === "object" ? pressure : {};
  const total = typeof info.mem_total_mb === "number" ? info.mem_total_mb : null;
  const avail = typeof info.mem_available_mb === "number" ? info.mem_available_mb : null;
  const frac = total !== null && avail !== null && total > 0 ? (total - avail) / total : null;
  let marker = null;
  if (
    charged !== null &&
    typeof charged.memory_mb === "number" &&
    typeof total === "number" &&
    total > 0
  ) {
    marker = charged.memory_mb / total;
  }
  const term = document.createElement("dt");
  term.appendChild(document.createTextNode("Memory, host used"));
  const budgetMem =
    typeof charged === "object" && charged !== null ? charged.memory_mb : null;
  return hostGauge(
    term,
    "Host memory used, from MemTotal and MemAvailable. The tick inside is the memory the loop reserved for its agents.",
    formatMemUsed(total, avail),
    frac,
    "host memory used",
    marker,
    budgetMem === null || budgetMem === undefined
      ? "reserved for agents: not reported"
      : "reserved for agents: " + formatMem(budgetMem) + " of the host",
  );
}

// The host gauges: the actual host values as bars, each with the
// share the loop reserved for its agents drawn as a tick inside and
// labelled as such. A missing reading renders unknown, never zero.
function resourcesCard(resources) {
  const desc =
    "The host as the kernel reports it. Each bar is the actual host value; " +
    "the tick inside is the share the loop reserved for its agents.";
  if (isErrorSection(resources)) {
    return errorCard(
      "host",
      desc,
      resources,
      "Resource use could not be measured. Check the host with korapy doctor.",
    );
  }
  const section = card("host", desc);
  const info = resources && typeof resources === "object" ? resources : {};
  const charged = info.charged && typeof info.charged === "object" ? info.charged : null;
  const budget = info.host_budget && typeof info.host_budget === "object" ? info.host_budget : null;
  if (charged === null && (info.pressure === null || info.pressure === undefined)) {
    section.appendChild(el("p", "no host readings in this snapshot"));
    return section;
  }
  section.appendChild(cpuGauge(info.pressure, charged));
  section.appendChild(memoryGauge(info.pressure, charged));
  const pidsText =
    charged === null || charged.pids === null || charged.pids === undefined
      ? "not reported"
      : String(charged.pids);
  const pidsBudget =
    budget === null || budget.pids === null || budget.pids === undefined
      ? null
      : String(budget.pids);
  section.appendChild(
    resourceDim(
      "PIDs",
      "process identifiers",
      pidsBudget === null
        ? pidsText + " processes reserved for agents (no host budget)"
        : pidsText + " of " + pidsBudget + " process slots reserved for agents",
      null,
      "Process slots reserved for live agents, against the host budget. The kernel reports no host total for processes.",
    ),
  );
  const shares = info.by_project && typeof info.by_project === "object" ? info.by_project : {};
  const measured =
    info.measured_by_project && typeof info.measured_by_project === "object"
      ? info.measured_by_project
      : {};
  const names = Object.keys(shares).sort();
  if (names.length > 0) {
    section.appendChild(
      el("p", "Reserved shares by project, with measured use where the kernel reports it:"),
    );
  }
  const lines = document.createElement("ul");
  for (const name of names) {
    const item = document.createElement("li");
    item.appendChild(el("span", (name === "" ? "-" : name) + ": " + projectShareLine(shares[name], measured[name])));
    lines.appendChild(item);
  }
  if (lines.childNodes.length > 0) {
    section.appendChild(lines);
  }
  section.appendChild(el("p", pressureText(info.pressure)));
  const notes = Array.isArray(info.notes) ? info.notes : [];
  for (const note of notes) {
    section.appendChild(el("p", "note: " + String(note)));
  }
  return section;
}

function capacityCard(capacity) {
  const desc = "How many agents this host may run.";
  if (isErrorSection(capacity)) {
    return errorCard(
      "capacity",
      desc,
      capacity,
      "Admission settings or the registry could not be read. Check the host with korapy doctor.",
    );
  }
  const section = card("capacity", desc);
  const info = capacity && typeof capacity === "object" ? capacity : {};
  const live =
    typeof info.live === "number" && Number.isFinite(info.live)
      ? String(info.live)
      : "not reported";
  const totalCap =
    typeof info.total_cap === "number" && Number.isFinite(info.total_cap)
      ? String(info.total_cap)
      : "not reported";
  const repoCap =
    typeof info.repo_cap !== "number" || !Number.isFinite(info.repo_cap)
      ? "not reported"
      : info.repo_cap === 0
        ? "none"
        : String(info.repo_cap) + " agents";
  section.appendChild(
    field(
      "Total capacity",
      live + " of " + totalCap + " tickets live",
      "How many agents the host admits in total.",
    ),
  );
  section.appendChild(
    field(
      "Per-repo cap",
      repoCap,
      "How many agents one repo may hold. None means no per-repo limit.",
    ),
  );
  section.appendChild(
    field(
      "Live now",
      live === "not reported" ? "not reported" : live + " tickets",
      "Tickets with a live agent right now: one ticket holds one slot.",
    ),
  );
  const pending = Array.isArray(info.pending) ? info.pending : null;
  section.appendChild(
    field(
      "Pending",
      pending === null
        ? "not reported"
        : pending.length === 0
          ? "none"
          : pending.map((name) => String(name)).join(", "),
      "Launches waiting for a slot. The host does not track a queue yet.",
    ),
  );
  const shares = info.by_project && typeof info.by_project === "object" ? info.by_project : {};
  const names = Object.keys(shares).sort();
  if (names.length > 0) {
    section.appendChild(
      field(
        "By project",
        names.map((name) => (name === "" ? "-" : name) + ": " + String(shares[name])).join("  "),
        "Counted tickets per project, the same count admission decides from.",
      ),
    );
  }
  return section;
}

function diskCard(disk) {
  const desc =
    "Free space on the state and cache disks. Each bar is the actual filesystem use; " +
    "no reservation is tracked for disk, so these bars carry no tick.";
  if (isErrorSection(disk)) {
    return errorCard(
      "disk",
      desc,
      disk,
      "Disk use could not be read. Check the host with korapy doctor.",
    );
  }
  const section = card("disk", desc);
  const info = disk && typeof disk === "object" ? disk : {};
  const paths = Array.isArray(info.paths) ? info.paths : [];
  if (paths.length === 0) {
    section.appendChild(el("p", "no disk paths reported"));
    return section;
  }
  const list = document.createElement("ul");
  for (const item of paths) {
    const row = document.createElement("li");
    if (item && typeof item.error === "string") {
      row.appendChild(el("span", String(item.path) + ": " + item.error));
    } else if (item && typeof item.total === "number" && item.total > 0) {
      const used = typeof item.used === "number" ? item.used / item.total : null;
      if (used !== null) {
        row.appendChild(meter(used, bandFor(used), String(item.path)));
        row.appendChild(document.createTextNode(" "));
      }
      row.appendChild(
        el(
          "span",
          formatBytes(item.free) +
            " free of " +
            formatBytes(item.total) +
            " (" +
            String(item.path) +
            ")",
        ),
      );
    } else if (item) {
      row.appendChild(
        el("span", formatBytes(item.free) + " free (" + String(item.path) + ")"),
      );
    }
    list.appendChild(row);
  }
  section.appendChild(list);
  return section;
}

function driveCard(drive, nowMs) {
  const desc = "The drive pass: sync boards, claim tickets, start agents.";
  if (isErrorSection(drive)) {
    return errorCard(
      "dispatch loop",
      desc,
      drive,
      "The drive journal could not be read. Check the host with korapy doctor.",
    );
  }
  const section = card("dispatch loop", desc);
  const info = drive && typeof drive === "object" ? drive : {};
  if (info.paused) {
    const reason =
      typeof info.pause_reason === "string" && info.pause_reason !== ""
        ? info.pause_reason
        : "no reason recorded";
    const pauseFile =
      typeof info.pause_file === "string" && info.pause_file !== ""
        ? info.pause_file
        : "the drive pause file";
    section.appendChild(
      field(
        "Status",
        "paused: " + reason,
        "The loop starts nothing while paused. It resumes when the " +
          pauseFile +
          " is removed from the state dir.",
      ),
    );
  } else {
    // A missing lock reading is not reported, never a healthy idle:
    // the adjacent last-pass age is the signal when the lock is mute.
    const lock =
      info.lock_held === true
        ? "running: a pass holds the lock"
        : info.lock_held === false
          ? "running"
          : "not reported";
    section.appendChild(
      field(
        "Status",
        lock,
        "The loop is active: each pass syncs boards, claims tickets and starts agents.",
      ),
    );
  }
  section.appendChild(
    field(
      "Last pass",
      age(info.last_pass_at, nowMs),
      "When the last pass started. A growing age means the timer may have stopped.",
    ),
  );
  section.appendChild(
    field(
      "Last result",
      exitWord(info.last_exit),
      "What the last pass reported. Exit 0 means the pass finished cleanly.",
    ),
  );
  const starts =
    info.starts_today === null || info.starts_today === undefined
      ? "not reported"
      : String(info.starts_today);
  const budget =
    info.max_starts_per_day === null || info.max_starts_per_day === undefined
      ? "not reported"
      : String(info.max_starts_per_day);
  section.appendChild(
    field(
      "Daily budget",
      "starts today " + starts + " of " + budget,
      "Agent starts used today of the daily maximum. Resumes and plans never count.",
    ),
  );
  const skipped = info.skipped_by_reason && typeof info.skipped_by_reason === "object"
    ? info.skipped_by_reason
    : {};
  const codes = Object.keys(skipped).sort();
  if (codes.length > 0) {
    section.appendChild(
      el(
        "p",
        "Skipped this pass: " +
          codes.map((code) => String(skipped[code]) + " " + code).join(", ") +
          ".",
        { class: "desc" },
      ),
    );
  }
  return section;
}

function agentText(row, nowMs) {
  const ticket =
    row.repo === null || row.repo === undefined || row.number === null || row.number === undefined
      ? "no ticket"
      : String(row.repo) + "#" + String(row.number);
  const bits = [ticket, String(row.state)];
  if (row.runtime !== null && row.runtime !== undefined) {
    bits.push(String(row.runtime));
  }
  if (row.model !== null && row.model !== undefined) {
    bits.push(String(row.model));
  }
  if (row.role !== null && row.role !== undefined) {
    bits.push(String(row.role));
  }
  if (row.started_at !== null && row.started_at !== undefined) {
    bits.push("started " + age(row.started_at, nowMs));
  }
  bits.push("seen " + age(row.last_seen, nowMs));
  return { line: bits.join("  "), note: row.note };
}

// One agent row as table cells: id, ticket, role, runtime, state,
// started, last seen, note. The id links to the detail when the row
// has a key; every cell carries its header name for narrow screens.
function agentRow(row, nowMs) {
  const cells = [];
  const key = agentKey(row);
  if (key === null) {
    cells.push([String(row.id), el("span", String(row.id))]);
  } else {
    const link = document.createElement("a");
    link.setAttribute("href", detailHash(key));
    link.textContent = cleanText(String(row.id));
    cells.push([String(row.id), link]);
  }
  const ticket =
    row.repo === null || row.repo === undefined || row.number === null || row.number === undefined
      ? "no ticket"
      : String(row.repo) + "#" + String(row.number);
  cells.push([ticket, el("span", ticket)]);
  const role = row.role === null || row.role === undefined ? "-" : String(row.role);
  cells.push([role, el("span", role)]);
  const runtime = row.runtime === null || row.runtime === undefined ? "-" : String(row.runtime);
  cells.push([runtime, el("span", runtime)]);
  cells.push([String(row.state), el("span", String(row.state))]);
  const started =
    row.started_at === null || row.started_at === undefined
      ? "not reported"
      : age(row.started_at, nowMs);
  cells.push([started, el("span", started)]);
  const seen =
    row.last_seen === null || row.last_seen === undefined ? "not reported" : age(row.last_seen, nowMs);
  cells.push([seen, el("span", seen)]);
  const note =
    row.note === null || row.note === undefined || String(row.note) === ""
      ? "-"
      : String(row.note);
  cells.push([note, el("span", note)]);
  const tr = document.createElement("tr");
  tr.setAttribute("data-state", String(row.state));
  const heads = ["id", "ticket", "role", "runtime", "state", "started", "last seen", "note"];
  cells.forEach(([_, node], index) => {
    const cell = document.createElement("td");
    cell.setAttribute("data-th", heads[index]);
    if (index === 0) {
      cell.setAttribute("class", "id");
    }
    cell.appendChild(node);
    tr.appendChild(cell);
  });
  return tr;
}

// One agents table over live and stale rows: header plus one row per
// agent, in server order so rows never jump between polls.
function agentTable(rows, nowMs) {
  const table = document.createElement("table");
  table.setAttribute("class", "agents");
  const head = document.createElement("thead");
  const headerRow = document.createElement("tr");
  for (const name of ["id", "ticket", "role", "runtime", "state", "started", "last seen", "note"]) {
    headerRow.appendChild(el("th", name));
  }
  head.appendChild(headerRow);
  table.appendChild(head);
  const body = document.createElement("tbody");
  for (const row of rows) {
    body.appendChild(agentRow(row, nowMs));
  }
  table.appendChild(body);
  return table;
}

// Whether a row is history: only an explicit finished state sits
// behind the toggle. Unknown states stay visible in the list rather
// than wearing the finished label.
function isFinishedRow(row) {
  return row.state === "finished";
}

// Finished rows, collapsed by default. The open set lives in this
// module so a poll re-render keeps the operator's toggle: the poll
// loop rebuilds the tree, and the rebuild re-opens what was open.
const openFinished = new Set();

function finishedDetails(key, rows, nowMs) {
  const box = document.createElement("details");
  box.setAttribute("class", "finished");
  const head = document.createElement("summary");
  head.textContent = cleanText(String(rows.length) + " finished (history)");
  box.appendChild(head);
  box.appendChild(
    el("p", "Finished entries are history, not agents. They stay here for " + formatRetentionWindow(FINISHED_RETENTION_MS) + " after their last sighting, then drop out.", {
      class: "desc",
    }),
  );
  if (openFinished.has(key)) {
    box.open = true;
  }
  box.addEventListener("toggle", () => {
    if (box.open) {
      openFinished.add(key);
    } else {
      openFinished.delete(key);
    }
  });
  box.appendChild(agentTable(rows, nowMs));
  return box;
}

function agentGroup(name, group, nowMs) {
  const wrap = document.createElement("div");
  wrap.setAttribute("class", "agent-group");
  const active = group.filter((row) => !isFinishedRow(row));
  // Finished runs stay visible, dimmed, for the retention window after
  // their last sighting, then drop out of the list.
  const finished = group.filter(
    (row) => isFinishedRow(row) && retainedFinished(row, nowMs),
  );
  if (name !== "") {
    wrap.appendChild(el("h3", name + " (" + String(active.length) + " active)"));
  } else {
    wrap.appendChild(el("h3", "no project (" + String(active.length) + " active)"));
  }
  if (active.length > 0) {
    wrap.appendChild(agentTable(active, nowMs));
  }
  if (finished.length > 0) {
    wrap.appendChild(finishedDetails(name, finished, nowMs));
  }
  return wrap;
}

// The agent filter text, kept in this module so a poll re-render keeps
// what the operator typed: the rebuild re-applies it instead of
// clobbering the input.
let agentFilter = "";

export function setAgentFilter(value) {
  agentFilter = typeof value === "string" ? value : "";
}

export function getAgentFilter() {
  return agentFilter;
}

function agentFilterInput() {
  const wrap = document.createElement("div");
  wrap.setAttribute("class", "agent-filter");
  const caption = document.createElement("label");
  caption.setAttribute("for", "agent-search");
  caption.textContent = "filter";
  const input = document.createElement("input");
  input.setAttribute("id", "agent-search");
  input.setAttribute("type", "search");
  input.setAttribute("autocomplete", "off");
  input.setAttribute("placeholder", "filter by id, ticket, role, runtime, note");
  input.value = agentFilter;
  wrap.appendChild(caption);
  wrap.appendChild(input);
  return wrap;
}

// Whether a row matches the filter text: id, ticket, role, runtime,
// state and note, case-insensitive. An empty filter matches all.
function matchesAgentFilter(row, needle) {
  if (needle === "") {
    return true;
  }
  const ticket =
    row.repo === null || row.repo === undefined || row.number === null || row.number === undefined
      ? "no ticket"
      : String(row.repo) + "#" + String(row.number);
  const hay = [
    row.id,
    ticket,
    row.role,
    row.runtime,
    row.state,
    row.model,
    row.note,
  ]
    .map((part) => (part === null || part === undefined ? "" : String(part)))
    .join(" ")
    .toLowerCase();
  return hay.includes(needle);
}

// The empty state names the loop, never a blank table: what runs now
// (nothing), when the loop last passed, and whether it is paused.
function emptyAgentsText(doc, project, nowMs) {
  if (project !== null) {
    return "No agents in this project yet.";
  }
  const loop = okSection(doc, "drive");
  if (loop !== null && loop.paused) {
    const reason =
      typeof loop.pause_reason === "string" && loop.pause_reason !== ""
        ? loop.pause_reason
        : "no reason recorded";
    return (
      "Nothing running right now. The loop is paused (" + reason + "); " +
      "it starts nothing until the pause lifts."
    );
  }
  const last =
    loop !== null ? formatAge(parseEpoch(loop.last_pass_at), nowMs) : "not reported";
  return (
    "Nothing running right now. The loop last passed " + last + "; " +
    "the next pass starts agents whose tickets are ready. " +
    "Start one by hand with korapy launch REPO NUMBER."
  );
}

function agentsSection(doc, project, nowMs) {
  const agents = doc ? doc.agents : null;
  const desc = "Live and stale agents first; finished entries sit behind a toggle.";
  if (isErrorSection(agents)) {
    return errorCard(
      "agents",
      desc,
      agents,
      "The agent registry could not be read, so no agent rows can be shown. Check the host with korapy doctor.",
    );
  }
  const section = card("agents", desc);
  section.appendChild(agentFilterInput());
  const rows = Array.isArray(agents) ? agents : [];
  const needle = agentFilter.trim().toLowerCase();
  const kept = rows.filter(
    (row) =>
      row !== null &&
      typeof row === "object" &&
      (project === null || row.project === project) &&
      matchesAgentFilter(row, needle),
  );
  if (kept.length === 0) {
    section.appendChild(
      el(
        "p",
        needle !== ""
          ? "No agents match this filter. Clear it to see the full list."
          : emptyAgentsText(doc, project, nowMs),
      ),
    );
    return section;
  }
  // Server order is already (project, repo, number, id); regroup
  // without re-sorting so rows never jump between polls.
  const groups = new Map();
  for (const row of kept) {
    const key = project === null ? String(row.project || "") : project;
    if (!groups.has(key)) {
      groups.set(key, []);
    }
    groups.get(key).push(row);
  }
  for (const [name, group] of [...groups.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1))) {
    section.appendChild(agentGroup(name, group, nowMs));
  }
  return section;
}

// One chip row builder for both views: the log view links back to
// its own route, the overview to its own. The base stays explicit
// so the two link shapes cannot drift apart unnoticed.
function chipsNodes(projects, selected, base) {
  const nodes = [];
  const all = document.createElement("a");
  if (base === "#/log") {
    all.setAttribute("href", "#/log");
  } else {
    all.setAttribute("href", "#/");
  }
  all.textContent = "All";
  if (selected === null) {
    all.setAttribute("aria-current", "true");
  }
  nodes.push(all);
  for (const name of projects) {
    const chip = document.createElement("a");
    if (base === "#/log") {
      chip.setAttribute("href", "#/log?project=" + encodeURIComponent(name));
    } else {
      chip.setAttribute("href", "#/?project=" + encodeURIComponent(name));
    }
    chip.textContent = cleanText(name);
    if (name === selected) {
      chip.setAttribute("aria-current", "true");
    }
    nodes.push(chip);
  }
  return nodes;
}

export function renderChips(projects, selected, base) {
  document.getElementById("chips").replaceChildren(...chipsNodes(projects, selected, base));
}

// The view tabs. The board arrives with its own view and hub mode
// adds hosts; each view renders its own tab as current.
export function renderTabs(current) {
  const nav = document.getElementById("tabs");
  nav.textContent = "";
  const tabs = [
    ["#/", "Overview", "overview"],
    ["#/board", "Board", "board"],
    ["#/log", "Log", "log"],
  ];
  for (const [href, label, name] of tabs) {
    const link = document.createElement("a");
    link.setAttribute("href", href);
    link.textContent = label;
    if (name === current) {
      link.setAttribute("aria-current", "true");
    }
    nav.appendChild(link);
  }
}

// The fleet strip: live agents as the headline number, stale beside
// it, finished as a collapsed secondary count. Finished entries are
// history, not agents, so they never share the headline.
function headerLine(header) {
  const desc = "Agents on this host right now. Finished entries are history, not agents.";
  if (isErrorSection(header)) {
    return errorCard(
      "fleet",
      desc,
      header,
      "The agent registry could not be read, so no counts can be shown. Check the host with korapy doctor.",
    );
  }
  const info = header && typeof header === "object" ? header : {};
  const live =
    typeof info.agents_live === "number" && Number.isFinite(info.agents_live)
      ? String(info.agents_live) + " live"
      : "live not reported";
  const stale =
    typeof info.agents_stale === "number" && Number.isFinite(info.agents_stale)
      ? String(info.agents_stale) + " stale"
      : "stale not reported";
  const finished =
    typeof info.agents_finished === "number" && Number.isFinite(info.agents_finished)
      ? info.agents_finished
      : null;
  const section = card("fleet", desc);
  section.setAttribute("class", "card strip");
  const line = document.createElement("p");
  line.setAttribute("class", "headline");
  const count = document.createElement("strong");
  count.textContent = cleanText(live);
  line.appendChild(count);
  line.appendChild(document.createTextNode(cleanText(", " + stale)));
  section.appendChild(line);
  if (finished === null) {
    section.appendChild(el("p", "finished not reported"));
  } else if (finished > 0) {
    const box = document.createElement("details");
    box.setAttribute("class", "finished");
    const head = document.createElement("summary");
    head.textContent = cleanText(String(finished) + " finished (history)");
    box.appendChild(head);
    box.appendChild(
      el("p", "Finished entries are history, not agents. They sit behind a toggle in the agents list below.", {
        class: "desc",
      }),
    );
    section.appendChild(box);
  }
  return section;
}

// The overview: header counts, one card per snapshot section
// (failed sections as error cards, never hidden), then agents
// grouped by project under the selected filter. Every node builds
// off-DOM and the live sections swap once, so a throw below leaves
// the last good page in place for the poll loop's catch instead of
// a half-drawn update.

// A readable snapshot section, or null when it errored. One resolver
// for the strip, the attention band and the empty state, so a section
// error reads the same everywhere.
function okSection(doc, name) {
  if (!doc || typeof doc !== "object") {
    return null;
  }
  const section = doc[name];
  return isErrorSection(section) ? null : section;
}

// One fleet count in words: a missing count reads as not reported,
// never as zero agents.
function countWord(value, singular) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return singular + " not reported";
  }
  return String(value) + " " + singular;
}

// The top status strip: one line that reads at a glance, always
// visible. Fleet counts, the loop state, quota freshness, host load
// per core, disk free. Every number traces to a snapshot field;
// a missing source reads as unknown, never as a healthy zero.
function stripText(doc, nowMs) {
  const bits = [];
  const header = okSection(doc, "fleet_header");
  if (header !== null) {
    bits.push(
      countWord(header.agents_live, "live") +
        ", " +
        countWord(header.agents_stale, "stale") +
        ", " +
        countWord(header.agents_finished, "finished"),
    );
  } else {
    bits.push("fleet unknown");
  }
  const loop = okSection(doc, "drive");
  if (loop !== null) {
    if (loop.paused) {
      const reason =
        typeof loop.pause_reason === "string" && loop.pause_reason !== ""
          ? loop.pause_reason
          : "no reason recorded";
      bits.push("loop paused: " + reason);
    } else {
      bits.push("loop running, last pass " + age(loop.last_pass_at, nowMs));
    }
    if (
      typeof loop.starts_today === "number" &&
      typeof loop.max_starts_per_day === "number"
    ) {
      bits.push("starts " + String(loop.starts_today) + "/" + String(loop.max_starts_per_day));
    }
  } else {
    bits.push("loop unknown");
  }
  const quota = okSection(doc, "quota");
  if (quota !== null) {
    const providers = quota.providers && typeof quota.providers === "object" ? quota.providers : {};
    const names = Object.keys(providers).sort();
    if (names.length === 0) {
      bits.push("quota: no providers");
    } else {
      const staleNames = names.filter((name) => (providers[name] || {}).stale);
      if (staleNames.length > 0) {
        bits.push("quota stale: " + staleNames.join(", "));
      } else {
        bits.push("quota fresh");
      }
    }
  } else {
    bits.push("quota unknown");
  }
  const resources = okSection(doc, "resources");
  const pressure = resources && resources.pressure ? resources.pressure : null;
  if (pressure !== null && typeof pressure.load_per_cpu === "number") {
    bits.push("load " + pressure.load_per_cpu.toFixed(2) + "/cpu");
  } else {
    bits.push("load unknown");
  }
  const disk = okSection(doc, "disk");
  const paths = disk !== null && Array.isArray(disk.paths) ? disk.paths : [];
  let free = null;
  for (const item of paths) {
    if (item && typeof item.free === "number" && (free === null || item.free < free)) {
      free = item.free;
    }
  }
  bits.push(free === null ? "disk unknown" : "disk " + formatBytes(free) + " free");
  return bits.join("  ·  ");
}

export function renderStrip(doc, { nowMs }) {
  const node = document.getElementById("strip");
  if (node === null) {
    return;
  }
  node.textContent = "";
  node.appendChild(document.createTextNode(cleanText(stripText(doc, nowMs))));
}

// The attention band: only items that need a human. Built from the
// snapshot the page already polls, never a new GitHub call. The
// empty state says "nothing needs you".
export function attentionItems(doc, nowMs) {
  const items = [];
  const loop = okSection(doc, "drive");
  if (loop === null) {
    items.push("the dispatch loop state could not be read");
  } else {
    if (loop.paused) {
      const reason =
        typeof loop.pause_reason === "string" && loop.pause_reason !== ""
          ? loop.pause_reason
          : "no reason recorded";
      items.push("the loop is paused: " + reason);
    }
    if (typeof loop.last_exit === "number" && loop.last_exit !== 0 && loop.last_exit !== 1) {
      items.push("the last pass ended " + exitWord(loop.last_exit));
    }
  }
  const quota = okSection(doc, "quota");
  if (quota !== null) {
    const providers = quota.providers && typeof quota.providers === "object" ? quota.providers : {};
    for (const name of Object.keys(providers).sort()) {
      const info = providers[name] || {};
      if (info.stale) {
        items.push("quota " + String(name) + " is stale (" + formatQuotaRead(info, nowMs) + ")");
      }
    }
  }
  const resources = okSection(doc, "resources");
  const pressure = resources && resources.pressure ? resources.pressure : null;
  if (pressure !== null && pressure.saturated) {
    items.push("the host is saturated: " + pressureText(pressure));
  }
  for (const [section, label] of [
    ["fleet_header", "fleet"],
    ["agents", "agents"],
    ["quota", "quota"],
    ["capacity", "capacity"],
    ["resources", "resources"],
    ["disk", "disk"],
    ["drive", "drive"],
  ]) {
    if (doc && isErrorSection(doc[section])) {
      items.push(label + " could not be read");
    }
  }
  return items;
}

function attentionCard(doc, nowMs) {
  const section = card(
    "needs you",
    "Only items that need a human. The coordinator owns the rest.",
  );
  const items = attentionItems(doc, nowMs);
  if (items.length === 0) {
    section.appendChild(el("p", "nothing needs you"));
    return section;
  }
  const list = document.createElement("ul");
  for (const item of items) {
    const row = document.createElement("li");
    row.appendChild(el("span", item));
    list.appendChild(row);
  }
  section.appendChild(list);
  return section;
}
export function renderOverview(doc, { project, nowMs, notice }) {
  renderTabs("overview");
  renderStrip(doc, { nowMs });
  const chips = chipsNodes(snapshotProjects(doc), project);
  const cards = [
    attentionCard(doc, nowMs),
    headerLine(doc.fleet_header),
    resourcesCard(doc.resources),
    quotaCard(doc.quota, nowMs),
    capacityCard(doc.capacity),
    diskCard(doc.disk),
    driveCard(doc.drive, nowMs),
  ];
  const agents = agentsSection(doc, project, nowMs);
  document.getElementById("detail").hidden = true;
  document.getElementById("board").hidden = true;
  const nav = document.getElementById("chips");
  nav.hidden = false;
  nav.replaceChildren(...chips);
  const note = document.getElementById("notice");
  note.textContent = notice ? cleanText(notice) : "";
  note.hidden = !notice;
  const cardsNode = document.getElementById("cards");
  cardsNode.hidden = false;
  cardsNode.replaceChildren(...cards);
  const agentsNode = document.getElementById("agents");
  agentsNode.hidden = false;
  agentsNode.replaceChildren(agents);
}

function ticketTitle(row) {
  if (!hasTicket(row)) {
    return "no ticket";
  }
  return String(row.repo) + "#" + String(row.number);
}

function fieldValue(value) {
  return value === null || value === undefined || value === "" ? "not reported" : String(value);
}

// The agent detail: every row field, then the tail pane for rows
// with a ticket. Fields build off-DOM and swap once like the
// overview; the tail pane is owned by the tail poll and stays put
// across field re-renders.
export function renderDetail(row, { nowMs }) {
  // No tab is current on a detail: the tabs name the list views only.
  renderTabs(null);
  const info = row && typeof row === "object" ? row : {};
  const project =
    typeof info.project === "string" && info.project !== "" ? info.project : null;
  const backHref = project === null ? "#/" : "#/?project=" + encodeURIComponent(project);
  const shown = agentText(info, nowMs);
  const lines = [
    el("h2", ticketTitle(info) + "  " + String(info.state)),
    el("p", "One agent: every field the host records, then its activity tail.", {
      class: "desc",
    }),
    el("p", shown.line),
  ];
  if (shown.note !== null && shown.note !== undefined && String(shown.note) !== "") {
    lines.push(el("p", "note: " + String(shown.note)));
  }
  lines.push(el("p", "id: " + fieldValue(info.id)));
  lines.push(el("p", "project: " + (project === null ? "none" : project)));
  lines.push(el("p", "profile: " + fieldValue(info.profile)));
  const copyButton = document.createElement("button");
  copyButton.setAttribute("type", "button");
  copyButton.setAttribute("data-copy-link", "");
  copyButton.textContent = "copy link";
  lines.push(copyButton);
  const ticketed = hasTicket(info);
  if (!ticketed) {
    lines.push(el("p", "no ticket, no tail"));
  }
  document.getElementById("chips").hidden = true;
  document.getElementById("cards").hidden = true;
  document.getElementById("agents").hidden = true;
  document.getElementById("board").hidden = true;
  const back = document.getElementById("detail-back");
  back.setAttribute("href", backHref);
  document.getElementById("detail-fields").replaceChildren(...lines);
  document.getElementById("tail-controls").hidden = !ticketed;
  document.getElementById("tail").hidden = !ticketed;
  if (!ticketed) {
    document.getElementById("tail").textContent = "";
    document.getElementById("tail-state").textContent = "";
  }
  document.getElementById("detail").hidden = false;
}

// A detail key that names nothing in the fresh document. States the
// miss in the detail pane; never a guessed neighbour.
export function renderDetailMissing() {
  renderTabs(null);
  document.getElementById("chips").hidden = true;
  document.getElementById("cards").hidden = true;
  document.getElementById("agents").hidden = true;
  document.getElementById("board").hidden = true;
  const back = document.getElementById("detail-back");
  back.setAttribute("href", "#/");
  document
    .getElementById("detail-fields")
    .replaceChildren(el("p", "this agent is no longer listed"));
  document.getElementById("tail-controls").hidden = true;
  document.getElementById("tail").hidden = true;
  document.getElementById("tail").textContent = "";
  document.getElementById("tail-state").textContent = "";
  document.getElementById("detail").hidden = false;
}

// The tail pane is one text node, replaced per poll: memory stays
// flat and no per-line nodes ever exist.
export function renderTail(text) {
  document.getElementById("tail").textContent = cleanText(text === null ? "" : String(text));
  document.getElementById("tail-state").textContent = "";
}

export function renderTailMissing() {
  document.getElementById("tail").textContent = "nothing to show";
  document.getElementById("tail-state").textContent = "";
}

export function renderTailFailed() {
  document.getElementById("tail-state").textContent = "tail unavailable, retrying";
}

export function renderTailLoading() {
  document.getElementById("tail").textContent = "";
  document.getElementById("tail-state").textContent = "loading the tail";
}

export function renderTailPaused(paused) {
  document.getElementById("tail-state").textContent = paused ? "tail paused" : "";
}

export function renderTailButtons(paused, follow) {
  document.getElementById("tail-pause").textContent = paused ? "tail: resume" : "tail: pause";
  document.getElementById("tail-follow").textContent = follow ? "follow: on" : "follow: off";
}

function eventLine(row, nowMs) {
  const kind = row.kind === null || row.kind === undefined ? "unknown" : String(row.kind);
  const ref = row.ref === null || row.ref === undefined ? "no ref" : String(row.ref);
  const raw = row.evidence === null || row.evidence === undefined ? "" : String(row.evidence);
  const evidence = raw === "" ? "no evidence" : raw;
  return kind + " " + ref + ": " + evidence + " (" + formatAge(parseIso(row.first_seen), nowMs) + ")";
}

function eventsCard(view, nowMs) {
  const total = typeof view.eventsTotal === "number" ? view.eventsTotal : view.events.length;
  const section = card(
    "needs attention (" + String(total) + ")",
    "Unresolved events for this project, oldest first.",
  );
  if (view.eventsState === "missing") {
    section.appendChild(el("p", "Choose a project to see its log."));
    return section;
  }
  if (view.eventsState === "unknown") {
    section.appendChild(el("p", "Unknown project " + String(view.project) + "."));
    return section;
  }
  if (view.eventsState === "failed") {
    section.appendChild(el("p", "Cannot load events right now; the next poll retries."));
    return section;
  }
  if (view.events.length === 0) {
    section.appendChild(el("p", "No unresolved events."));
    return section;
  }
  const list = document.createElement("ul");
  for (const row of view.events) {
    const item = document.createElement("li");
    item.appendChild(el("span", eventLine(row, nowMs)));
    list.appendChild(item);
  }
  section.appendChild(list);
  if (total > view.events.length) {
    section.appendChild(
      el("p", "Showing " + String(view.events.length) + " of " + String(total) + "."),
    );
  }
  return section;
}

function kindChips(selected) {
  const wrap = document.createElement("div");
  wrap.setAttribute("class", "kinds");
  const all = document.createElement("button");
  all.setAttribute("type", "button");
  all.setAttribute("data-log-kind", "");
  all.textContent = "all";
  if (selected === null) {
    all.setAttribute("aria-current", "true");
  }
  wrap.appendChild(all);
  for (const kind of LOG_KINDS) {
    const chip = document.createElement("button");
    chip.setAttribute("type", "button");
    chip.setAttribute("data-log-kind", kind);
    chip.textContent = cleanText(kind);
    if (kind === selected) {
      chip.setAttribute("aria-current", "true");
    }
    wrap.appendChild(chip);
  }
  return wrap;
}

function journalEntryNode(row, nowMs) {
  const kind = row.kind === null || row.kind === undefined ? "unknown" : String(row.kind);
  const holder = row.holder === null || row.holder === undefined ? "unknown" : String(row.holder);
  const item = document.createElement("li");
  item.appendChild(el("span", formatAge(parseIso(row.at), nowMs) + " " + kind + " " + holder));
  const refs = Array.isArray(row.refs) ? row.refs : [];
  if (refs.length > 0) {
    item.appendChild(el("span", " refs: " + refs.map((ref) => String(ref)).join(", ")));
  }
  const text = row.text === null || row.text === undefined ? "" : String(row.text);
  item.appendChild(el("p", text === "" ? "(empty entry)" : text));
  return item;
}

function journalCard(view, nowMs) {
  const total = typeof view.journalTotal === "number" ? view.journalTotal : 0;
  const section = card(
    "journal (" + String(total) + ")",
    "The project's logbook: what the coordinator recorded, newest window last.",
  );
  if (view.journalState === "missing") {
    section.appendChild(el("p", "Choose a project above to read its logbook."));
    return section;
  }
  if (view.journalState === "unknown") {
    section.appendChild(el("p", "Unknown project " + String(view.project) + "."));
    return section;
  }
  section.appendChild(kindChips(view.kind));
  if (view.journalState === "failed") {
    section.appendChild(el("p", "Cannot load the journal right now; the next poll retries."));
    return section;
  }
  if (view.journal.length === 0) {
    section.appendChild(el("p", "No entries yet."));
    return section;
  }
  const list = document.createElement("ul");
  for (const row of view.journal) {
    list.appendChild(journalEntryNode(row, nowMs));
  }
  section.appendChild(list);
  section.appendChild(
    el(
      "p",
      "Showing " +
        String(view.journalStart + 1) +
        "-" +
        String(view.journalStart + view.journal.length) +
        " of " +
        String(total) +
        ".",
    ),
  );
  if (view.history) {
    section.appendChild(el("p", "Browsing history; the newest entries pause here."));
    const latest = document.createElement("button");
    latest.setAttribute("type", "button");
    latest.setAttribute("data-log-latest", "");
    latest.textContent = "back to latest";
    section.appendChild(latest);
  }
  if (view.journalStart > 0 && view.journal.length < LOG_JOURNAL_CAP) {
    const older = document.createElement("button");
    older.setAttribute("type", "button");
    older.setAttribute("data-log-older", "");
    older.textContent = "load older";
    section.appendChild(older);
  } else if (view.journal.length >= LOG_JOURNAL_CAP) {
    section.appendChild(
      el(
        "p",
        "Showing the newest " +
          String(LOG_JOURNAL_CAP) +
          " entries; narrow the kind filter to see older ones.",
      ),
    );
  }
  return section;
}

// The log view: one project at a time, its unresolved stack over
// its logbook. "All" shows the project picker, never a merged log.
export function renderLog(view, { nowMs, notice }) {
  renderTabs("log");
  // The detail view hides these sections; coming back unhides them.
  document.getElementById("detail").hidden = true;
  document.getElementById("board").hidden = true;
  const nav = document.getElementById("chips");
  nav.hidden = false;
  renderChips(view.choices, view.project, "#/log");
  const note = document.getElementById("notice");
  note.textContent = "";
  if (notice) {
    note.hidden = false;
    note.appendChild(document.createTextNode(cleanText(notice)));
  } else {
    note.hidden = true;
  }
  const cards = document.getElementById("cards");
  cards.textContent = "";
  if (view.derivedOnly) {
    cards.appendChild(el("p", "Project list is derived from activity only."));
  }
  if (view.project === null) {
    const section = card("log", "One project at a time: pick one above to read its log.");
    section.appendChild(el("p", "Choose a project to see its log."));
    cards.appendChild(section);
  } else if (view.unknown) {
    const section = card("log", "One project at a time.");
    section.appendChild(el("p", "Unknown project " + String(view.project) + "."));
    cards.appendChild(section);
  } else {
    cards.appendChild(eventsCard(view, nowMs));
    cards.appendChild(journalCard(view, nowMs));
  }
  cards.hidden = false;
  const agents = document.getElementById("agents");
  agents.hidden = false;
  agents.textContent = "";
}

// A fatal gate: the API major moved or the document schema is too
// old. Data stops rendering until the operator upgrades; the last
// good cards are cleared so old numbers never pose as current.
export function renderStopped(message) {
  // The strip holds last good numbers: with rendering stopped those
  // would pose as current, so it degrades to all-unknown beside them.
  try {
    renderStrip(null, { nowMs: Date.now() });
  } catch {
    // A missing strip node never blocks the stopped gate.
  }
  const nav = document.getElementById("chips");
  nav.textContent = "";
  const note = document.getElementById("notice");
  note.textContent = "";
  note.hidden = false;
  note.appendChild(document.createTextNode(cleanText(message)));
  document.getElementById("cards").textContent = "";
  document.getElementById("agents").textContent = "";
  document.getElementById("board").textContent = "";
  document.getElementById("board").hidden = true;
  document.getElementById("detail").hidden = true;
}

// Toasts: small confirmations for copy and open outcomes, bottom
// right, never in the footer. Text only, auto-removed after a few
// seconds, capped so a burst never stacks past three.
export function toast(message) {
  const region = document.getElementById("toasts");
  if (region === null) {
    return;
  }
  const node = document.createElement("div");
  node.setAttribute("class", "toast");
  node.appendChild(document.createTextNode(cleanText(message)));
  region.appendChild(node);
  while (region.childNodes.length > 3 && region.firstChild !== null) {
    region.firstChild.remove();
  }
  window.setTimeout(() => {
    node.remove();
  }, 4000);
}

// A tracker reference, or the raw value as text. Only absolute http
// and https URLs become links (with rel, never a new window: the
// operator chose to leave); anything else renders as text, never
// markup, per the text-node rule at the top of this module.
export function trackerLink(url) {
  let link = null;
  if (typeof url === "string" && url !== "") {
    let scheme = null;
    try {
      scheme = new URL(url).protocol;
    } catch {
      scheme = null;
    }
    if (scheme === "http:" || scheme === "https:") {
      link = document.createElement("a");
      link.setAttribute("href", url);
      link.setAttribute("rel", "noopener noreferrer");
      link.textContent = "tracker";
    }
  }
  if (link !== null) {
    return link;
  }
  return el("span", typeof url === "string" && url !== "" ? url : "no tracker link");
}

// The board shell: filter controls and empty containers. Built once
// per mount (not per poll) so typed filters survive the 15s poll;
// rows and the pager fill in through renderBoardRows below.
export function renderBoardShell({ repo, state, label }) {
  renderTabs("board");
  document.getElementById("chips").hidden = true;
  document.getElementById("cards").hidden = true;
  document.getElementById("agents").hidden = true;
  document.getElementById("detail").hidden = true;
  const board = document.getElementById("board");
  board.hidden = false;
  board.textContent = "";
  board.appendChild(el("h2", "board"));
  board.appendChild(
    el("p", "Every ticket on the board: filter, page through, open the tracker.", {
      class: "desc",
    }),
  );
  const form = document.createElement("form");
  form.setAttribute("id", "board-filters");
  const fields = [
    ["board-repo", "repo", repo],
    ["board-state", "state", state],
    ["board-label", "label", label],
  ];
  for (const [id, name, value] of fields) {
    const caption = document.createElement("label");
    caption.setAttribute("for", id);
    caption.textContent = name;
    const input = document.createElement("input");
    input.setAttribute("id", id);
    input.setAttribute("name", name);
    input.setAttribute("type", "text");
    input.setAttribute("autocomplete", "off");
    input.value = value === null || value === undefined ? "" : String(value);
    form.appendChild(caption);
    form.appendChild(input);
  }
  const apply = document.createElement("button");
  apply.setAttribute("type", "submit");
  apply.textContent = "apply";
  form.appendChild(apply);
  board.appendChild(form);
  const list = document.createElement("ul");
  list.setAttribute("id", "board-rows");
  board.appendChild(list);
  const pager = document.createElement("div");
  pager.setAttribute("id", "board-pager");
  const prev = document.createElement("button");
  prev.setAttribute("id", "board-prev");
  prev.setAttribute("type", "button");
  prev.textContent = "prev";
  const count = document.createElement("span");
  count.setAttribute("id", "board-count");
  const next = document.createElement("button");
  next.setAttribute("id", "board-next");
  next.setAttribute("type", "button");
  next.textContent = "next";
  pager.appendChild(prev);
  pager.appendChild(count);
  pager.appendChild(next);
  board.appendChild(pager);
}

// The board rows and pager for one poll reply. Only the list and the
// pager rebuild, so the filter controls stand. Row lines come from
// the boardRow helper below (holder, trackerLink, fetched age with
// fresh, confidence), and the pager counts against total.
export function renderBoardRows(payload, { limit, offset, nowMs }) {
  const info = payload !== null && typeof payload === "object" ? payload : {};
  const rows = Array.isArray(info.board) ? info.board : [];
  const total = typeof info.total === "number" ? info.total : rows.length;
  const page = typeof info.limit === "number" ? info.limit : limit;
  const list = document.getElementById("board-rows");
  list.textContent = "";
  if (rows.length === 0) {
    list.appendChild(el("li", "No rows match these filters. Clear a filter and apply again."));
  }
  for (const row of rows) {
    if (row !== null && typeof row === "object") {
      list.appendChild(boardRow(row, nowMs));
    }
  }
  // A stale offset past the end (rows deleted under an open page)
  // never inverts the count: the range clamps into [0, total].
  const clampedOffset = Math.max(0, Math.min(offset, total));
  const first = total === 0 || rows.length === 0 ? 0 : clampedOffset + 1;
  const last = Math.min(clampedOffset + rows.length, total);
  const count = document.getElementById("board-count");
  count.textContent = "";
  count.appendChild(document.createTextNode(first + "-" + last + " of " + total));
  // The drift count rides the same reply as the rows: the board
  // step's verdicts, the number the doctor reports for the repo.
  const drifted = typeof info.drift === "number" ? info.drift : 0;
  if (drifted > 0) {
    count.appendChild(
      document.createTextNode(" (" + drifted + " drifted: sweep the repo to fix)"),
    );
  }
  document.getElementById("board-prev").disabled = clampedOffset <= 0;
  document.getElementById("board-next").disabled = clampedOffset + page >= total;
}

function claimText(claim) {
  // A missing or foreign holder shape reads as unheld instead of
  // throwing inside the poll loop.
  if (claim === null || claim === undefined || typeof claim !== "object") {
    return "unheld";
  }
  const agent = claim.agent === null || claim.agent === undefined ? "?" : String(claim.agent);
  const host = claim.host === null || claim.host === undefined ? "?" : String(claim.host);
  const epoch = claim.epoch === null || claim.epoch === undefined ? "?" : String(claim.epoch);
  return agent + " on " + host + " (" + epoch + ")";
}

function boardRow(row, nowMs) {
  const item = document.createElement("li");
  item.setAttribute("class", "card");
  const head = document.createElement("div");
  head.appendChild(
    el("strong", String(row.repo) + "#" + String(row.number) + "  " + String(row.title)),
  );
  head.appendChild(document.createTextNode("  "));
  head.appendChild(trackerLink(row.url));
  item.appendChild(head);
  const bits = [String(row.state)];
  if (row.priority !== null && row.priority !== undefined && String(row.priority) !== "") {
    bits.push("priority:" + String(row.priority));
  }
  if (Array.isArray(row.labels) && row.labels.length > 0) {
    bits.push(row.labels.map((name) => String(name)).join(", "));
  }
  bits.push(claimText(row.claim));
  item.appendChild(el("div", bits.join(", ")));
  item.appendChild(
    el(
      "div",
      "fetched " +
        formatAge(parseEpoch(row.fetched_at), nowMs) +
        ", " +
        String(row.confidence) +
        ", " +
        (row.fresh ? "fresh" : "stale"),
    ),
  );
  return item;
}
