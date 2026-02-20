import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from asyncua.sync import Client as OpcUaClient
from flask import Flask, request
from influxdb import InfluxDBClient

OPCUA_URL = "opc.tcp://localhost:49320"
INFLUX_HOST = "localhost"
INFLUX_PORT = 8086
INFLUX_DB = "opcuadata"

RAW_TAG_MEASUREMENT = "selected_tag_data"
RAW_LOOP_MEASUREMENT = "pid_loop_hf_raw"
RAW_LOOP_RETENTION_POLICY = "hf_raw_400d"
POLL_INTERVAL_SECONDS = 1.0
WRITE_BATCH_SIZE = 5000
MAX_CACHE_AGE_SECONDS = 30
MAX_PENDING_POINTS = 120000
LOOP_CACHE_MAX_AGE_SECONDS = 300
LOOP_MAX_BACKFILL_SECONDS = 10

SELECTION_FILE = Path("state/tag_selection.json")
LOOP_ASSIGNMENTS_FILE = Path("state/loop_assignments.json")

GRAFANA_DASHBOARD_URL = (
    "http://localhost:3000/d/pid-hf-1s-benchmark/pid-high-frequency-benchmark"
)

app = Flask(__name__)

opc_lock = threading.Lock()
opc_client = None

raw_logger_lock = threading.Lock()
raw_logger_thread = None
raw_logger_running = False

loop_logger_lock = threading.Lock()
loop_logger_thread = None
loop_logger_running = False

raw_cache_lock = threading.Lock()
raw_value_cache = {}

loop_cache_lock = threading.Lock()
loop_value_cache = {}


def ensure_opc_client() -> OpcUaClient:
    global opc_client
    if opc_client is None:
        opc_client = OpcUaClient(url=OPCUA_URL)
        opc_client.connect()
    return opc_client


def get_influx_client() -> InfluxDBClient:
    influx = InfluxDBClient(host=INFLUX_HOST, port=INFLUX_PORT)
    influx.create_database(INFLUX_DB)
    influx.switch_database(INFLUX_DB)
    return influx


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return fallback
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def load_selection() -> dict:
    return load_json(SELECTION_FILE, {"tags": []})


def save_selection(selection: dict) -> None:
    save_json(SELECTION_FILE, selection)


def load_loop_assignments() -> dict:
    return load_json(LOOP_ASSIGNMENTS_FILE, {"loops": []})


def save_loop_assignments(payload: dict) -> None:
    save_json(LOOP_ASSIGNMENTS_FILE, payload)


def influx_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def parse_utc_time(value: str):
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def read_numeric_with_cache(client, nodeid: str, cache: dict, max_cache_age_seconds: int = MAX_CACHE_AGE_SECONDS):
    now = utc_now()
    try:
        value = safe_float(client.get_node(nodeid).read_value())
        cache[nodeid] = {"value": value, "ts": now}
        return value, False, 0, ""
    except Exception as exc:
        cached = cache.get(nodeid)
        if cached:
            age = int((now - cached["ts"]).total_seconds())
            if age <= max_cache_age_seconds:
                return float(cached["value"]), True, age, str(exc)
        return None, False, None, str(exc)


def write_pending_points(
    influx: InfluxDBClient,
    pending_points: list,
    retention_policy: str = "",
) -> tuple[int, str]:
    written_total = 0
    last_error = ""

    while pending_points:
        batch = pending_points[:WRITE_BATCH_SIZE]
        try:
            kwargs = {"time_precision": "s", "batch_size": WRITE_BATCH_SIZE}
            if retention_policy:
                kwargs["retention_policy"] = retention_policy
            influx.write_points(batch, **kwargs)
        except Exception as exc:
            last_error = str(exc)
            break
        del pending_points[: len(batch)]
        written_total += len(batch)

    return written_total, last_error


def safe_float(value):
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"Non-numeric value: {value!r}")


def raw_logging_loop() -> None:
    global raw_logger_running
    influx = get_influx_client()

    pending_points = []

    while True:
        with raw_logger_lock:
            if not raw_logger_running:
                break

        selection = load_selection().get("tags", [])
        new_points = []
        ts = int(utc_now().timestamp())

        with opc_lock:
            client = ensure_opc_client()
            for item in selection:
                nodeid = item.get("nodeid", "")
                label = item.get("label", nodeid)
                if not nodeid:
                    continue
                with raw_cache_lock:
                    numeric_value, from_cache, cache_age, read_error = read_numeric_with_cache(
                        client=client,
                        nodeid=nodeid,
                        cache=raw_value_cache,
                    )
                if numeric_value is None:
                    continue

                new_points.append(
                    {
                        "measurement": RAW_TAG_MEASUREMENT,
                        "tags": {"nodeid": nodeid, "label": label},
                        "time": ts,
                        "fields": {
                            "value": numeric_value,
                            "from_cache": 1 if from_cache else 0,
                            "cache_age_s": int(cache_age or 0),
                            "read_error": 1 if read_error else 0,
                        },
                    }
                )

        if new_points:
            pending_points.extend(new_points)

        if pending_points:
            _, _ = write_pending_points(influx=influx, pending_points=pending_points)
            if len(pending_points) > MAX_PENDING_POINTS:
                del pending_points[: len(pending_points) - MAX_PENDING_POINTS]

        time.sleep(POLL_INTERVAL_SECONDS)


def loop_logging_loop() -> None:
    global loop_logger_running
    influx = get_influx_client()

    pending_points = []

    next_tick = int(time.time())

    while True:
        with loop_logger_lock:
            if not loop_logger_running:
                break

        now_sec = int(time.time())
        if now_sec < next_tick:
            time.sleep(0.05)
            continue

        # If processing lags, backfill recent missing seconds with cached/latest values.
        backfill_from = max(next_tick, now_sec - LOOP_MAX_BACKFILL_SECONDS + 1)
        tick_timestamps = list(range(backfill_from, now_sec + 1))
        next_tick = now_sec + 1

        loops = load_loop_assignments().get("loops", [])
        new_points = []

        with opc_lock:
            client = ensure_opc_client()
            for loop in loops:
                loop_id = str(loop.get("loop_id", "")).strip()
                machine_id = str(loop.get("machine_id", "")).strip() or "Kepware"
                pv_nodeid = str(loop.get("pv_nodeid", "")).strip()
                sp_nodeid = str(loop.get("sp_nodeid", "")).strip()
                co_nodeid = str(loop.get("co_nodeid", "")).strip()

                if not loop_id or not pv_nodeid or not sp_nodeid or not co_nodeid:
                    continue

                with loop_cache_lock:
                    pv_value, pv_cached, pv_age, _ = read_numeric_with_cache(
                        client=client,
                        nodeid=pv_nodeid,
                        cache=loop_value_cache,
                        max_cache_age_seconds=LOOP_CACHE_MAX_AGE_SECONDS,
                    )
                    sp_value, sp_cached, sp_age, _ = read_numeric_with_cache(
                        client=client,
                        nodeid=sp_nodeid,
                        cache=loop_value_cache,
                        max_cache_age_seconds=LOOP_CACHE_MAX_AGE_SECONDS,
                    )
                    co_value, co_cached, co_age, _ = read_numeric_with_cache(
                        client=client,
                        nodeid=co_nodeid,
                        cache=loop_value_cache,
                        max_cache_age_seconds=LOOP_CACHE_MAX_AGE_SECONDS,
                    )

                if pv_value is None or sp_value is None or co_value is None:
                    continue

                for ts in tick_timestamps:
                    new_points.append(
                        {
                            "measurement": RAW_LOOP_MEASUREMENT,
                            "tags": {"loop_id": loop_id, "machine_id": machine_id},
                            "time": ts,
                            "fields": {
                                "PV": pv_value,
                                "SP": sp_value,
                                "CO": co_value,
                                "PV_from_cache": 1 if pv_cached else 0,
                                "SP_from_cache": 1 if sp_cached else 0,
                                "CO_from_cache": 1 if co_cached else 0,
                                "PV_cache_age_s": int(pv_age or 0),
                                "SP_cache_age_s": int(sp_age or 0),
                                "CO_cache_age_s": int(co_age or 0),
                                "tick_backfill": 1 if ts < now_sec else 0,
                            },
                        }
                    )

        if new_points:
            pending_points.extend(new_points)

        if pending_points:
            _, _ = write_pending_points(
                influx=influx,
                pending_points=pending_points,
                retention_policy=RAW_LOOP_RETENTION_POLICY,
            )
            if len(pending_points) > MAX_PENDING_POINTS:
                del pending_points[: len(pending_points) - MAX_PENDING_POINTS]

        time.sleep(0.02)


def tree_item(node) -> dict:
    browse_name = node.read_browse_name().Name
    node_class = str(node.read_node_class())
    return {
        "browse_name": browse_name,
        "nodeid": node.nodeid.to_string(),
        "node_class": node_class,
    }


def grafana_url(loop_id: str, machine_id: str, method: str, range_from: str) -> str:
    query = urlencode(
        {
            "orgId": "1",
            "from": range_from,
            "to": "now",
            "timezone": "browser",
            "refresh": "10s",
            "var-loop_id": loop_id,
            "var-machine_id": machine_id,
            "var-method": method,
        }
    )
    return f"{GRAFANA_DASHBOARD_URL}?{query}"


@app.get("/")
def index():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Historian Operations Console</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css">
  <style>
    :root {
      --bg: #eef2f4;
      --card: #ffffff;
      --ink: #13212f;
      --muted: #5f6f80;
      --line: #d7dde5;
      --nav-1: #142231;
      --nav-2: #203447;
      --accent: #2f7fbc;
      --good: #2e8b57;
      --warn: #b44b3c;
      --steel: #8192a3;
    }
    body {
      font-family: "IBM Plex Sans", "Segoe UI", Tahoma, sans-serif;
      background: radial-gradient(circle at 20% 10%, #f9fbfc 0%, var(--bg) 48%, #e4eaef 100%);
      color: var(--ink);
      min-height: 100vh;
    }
    .app-shell {
      max-width: 1700px;
      margin: 0 auto;
      padding: 14px;
    }
    .topbar {
      background: linear-gradient(120deg, var(--nav-1) 0%, var(--nav-2) 100%);
      border: 1px solid rgba(255, 255, 255, 0.07);
      border-radius: 16px;
      padding: 14px 18px;
      box-shadow: 0 12px 24px rgba(10, 20, 33, 0.28);
      margin-bottom: 14px;
    }
    .topbar .title {
      color: #f0f6fb;
      font-weight: 700;
      letter-spacing: 0.25px;
    }
    .subtitle {
      color: #bdd2e5;
      font-size: 12px;
      margin-top: 2px;
    }
    .panel-card {
      border: 1px solid var(--line);
      border-radius: 12px;
      box-shadow: 0 6px 18px rgba(17, 35, 52, 0.08);
      background: var(--card);
    }
    .panel-card .card-header {
      border-bottom: 1px solid #e6ecf2;
      background: linear-gradient(180deg, #fcfeff 0%, #f2f6fa 100%);
      font-weight: 700;
      color: #162738;
    }
    .muted {
      color: var(--muted);
      font-size: 12px;
    }
    .meta-row {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
    }
    .meta-badge {
      background: #f0f4f8;
      border: 1px solid #d9e1ea;
      color: #385068;
      border-radius: 999px;
      font-size: 11px;
      padding: 3px 8px;
      font-weight: 600;
    }
    .status-pill {
      font-size: 12px;
      font-weight: 700;
      padding: 6px 12px;
      border-radius: 999px;
      border: 1px solid transparent;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .status-pill.on {
      color: #c7f4dc;
      border-color: rgba(74, 173, 120, 0.55);
      background: rgba(41, 120, 82, 0.45);
    }
    .status-pill.off {
      color: #dde6ef;
      border-color: rgba(184, 201, 218, 0.45);
      background: rgba(255, 255, 255, 0.08);
    }
    .tree-wrap {
      max-height: 420px;
      overflow: auto;
      border: 1px solid #dbe3ec;
      border-radius: 8px;
      padding: 10px;
      background: linear-gradient(180deg, #fbfdff 0%, #f3f7fb 100%);
    }
    .tree-list {
      list-style: none;
      margin: 0;
      padding-left: 0;
    }
    .tree-list .tree-list {
      margin-top: 6px;
      margin-left: 12px;
      padding-left: 10px;
      border-left: 1px dashed #cbd5e0;
    }
    .tree-item {
      margin-bottom: 6px;
    }
    .tree-row {
      display: flex;
      align-items: center;
      gap: 8px;
      background: #ffffff;
      border: 1px solid #e2eaf2;
      border-radius: 8px;
      padding: 6px 8px;
    }
    .tree-title {
      flex: 1;
      min-width: 0;
      border: 0;
      background: transparent;
      text-align: left;
      color: #1e3851;
      font-weight: 600;
      padding: 2px 4px;
      border-radius: 6px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }
    .tree-title:hover {
      background: #edf4fb;
    }
    .tree-class {
      font-size: 10px;
      font-weight: 700;
      letter-spacing: 0.3px;
      color: #5b7390;
      text-transform: uppercase;
      background: #edf2f8;
      border: 1px solid #d9e2ee;
      padding: 2px 6px;
      border-radius: 999px;
    }
    .btn-xs {
      --bs-btn-padding-y: .18rem;
      --bs-btn-padding-x: .45rem;
      --bs-btn-font-size: .70rem;
      --bs-btn-border-radius: .35rem;
      font-weight: 700;
    }
    .btn-industrial {
      --bs-btn-color: #fff;
      --bs-btn-bg: var(--accent);
      --bs-btn-border-color: #2b74ab;
      --bs-btn-hover-color: #fff;
      --bs-btn-hover-bg: #276da0;
      --bs-btn-hover-border-color: #22618f;
    }
    .data-table td, .data-table th {
      font-size: 12px;
      vertical-align: middle;
      border-color: #e4ebf2;
    }
    .data-table th {
      color: #385068;
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.35px;
    }
    .data-table td code {
      font-size: 11px;
      color: #3f5570;
    }
    .ok {
      color: var(--good);
      font-weight: 700;
    }
    .bad {
      color: var(--warn);
      font-weight: 700;
    }
    .state-chip {
      font-size: 11px;
      font-weight: 700;
      border-radius: 999px;
      padding: 2px 8px;
      border: 1px solid transparent;
      display: inline-block;
    }
    .state-chip.saving {
      background: rgba(46, 139, 87, 0.13);
      border-color: rgba(46, 139, 87, 0.32);
      color: #206a42;
    }
    .state-chip.stale {
      background: rgba(180, 75, 60, 0.11);
      border-color: rgba(180, 75, 60, 0.28);
      color: #9e4034;
    }
    .viewer-url {
      font-size: 12px;
      color: #334d68;
      background: #f6f9fc;
      border: 1px solid #d5e1ed;
      font-family: "IBM Plex Mono", "Consolas", monospace;
    }
    .method-help {
      border: 1px solid #dce5ee;
      border-radius: 8px;
      background: #f7fafd;
      padding: 8px;
      font-size: 12px;
      color: #45607a;
      display: grid;
      gap: 4px;
    }
    @media (max-width: 992px) {
      .app-shell {
        padding: 8px;
      }
    }
  </style>
</head>
<body>
  <div class="app-shell">
    <nav class="topbar">
      <div class="d-flex flex-wrap gap-2 align-items-center justify-content-between">
        <div>
          <div class="title h5 mb-0">Historian Operations Console</div>
          <div class="subtitle">Factory-style tag logging and loop diagnostics</div>
        </div>
        <div class="d-flex gap-2 align-items-center">
          <span class="status-pill off" id="rawLoggerState">Raw Logger OFF</span>
          <span class="status-pill off" id="loopLoggerState">Loop Logger OFF</span>
          <span class="subtitle">OPC UA -> InfluxDB -> Grafana</span>
        </div>
      </div>
    </nav>

    <div class="row g-3">
      <div class="col-12 col-xl-4">
        <div class="card panel-card">
          <div class="card-header d-flex justify-content-between align-items-center">
            <span>Tag Browser</span>
            <button class="btn btn-sm btn-industrial" onclick="loadRoot()">Refresh Root</button>
          </div>
          <div class="card-body">
            <div class="meta-row mb-2">
              <span class="meta-badge">OPC UA Source</span>
              <span class="muted">Current node: <span id="currentNode">root</span></span>
            </div>
            <div class="tree-wrap" id="tree"></div>
          </div>
        </div>

        <div class="card panel-card mt-3">
          <div class="card-header">Selected Tags for Logging</div>
          <div class="card-body">
            <div class="d-flex flex-wrap gap-2 mb-2">
              <button class="btn btn-sm btn-industrial" onclick="saveSelection()">Save Selection</button>
              <button class="btn btn-sm btn-outline-success" onclick="startRawLogging()">Start Raw Tag Logging</button>
              <button class="btn btn-sm btn-outline-danger" onclick="stopRawLogging()">Stop Raw Tag Logging</button>
            </div>
            <div class="muted mb-2" id="status"></div>
            <div class="table-responsive">
              <table class="table table-sm data-table align-middle" id="selectionTable">
                <thead class="table-light">
                  <tr><th>Label</th><th>NodeId</th><th>Live OPC</th><th>Last Saved</th><th>Status</th><th></th></tr>
                </thead>
                <tbody></tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <div class="col-12 col-xl-8">
        <div class="card panel-card">
          <div class="card-header">Loop Mapping (PV/SP/CO)</div>
          <div class="card-body">
            <div class="d-flex flex-wrap gap-2 mb-2">
              <button class="btn btn-sm btn-industrial" onclick="addLoop()">Add Loop</button>
              <button class="btn btn-sm btn-outline-primary" onclick="saveLoops()">Save Loops</button>
              <button class="btn btn-sm btn-outline-success" onclick="startLoopLogging()">Start Loop Logging</button>
              <button class="btn btn-sm btn-outline-danger" onclick="stopLoopLogging()">Stop Loop Logging</button>
            </div>
            <div class="table-responsive">
              <table class="table table-sm data-table align-middle" id="loopTable">
                <thead class="table-light">
                  <tr><th>Loop ID</th><th>Machine</th><th>PV</th><th>SP</th><th>CO</th><th></th></tr>
                </thead>
                <tbody></tbody>
              </table>
            </div>
          </div>
        </div>

        <div class="card panel-card mt-3">
          <div class="card-header">Grafana Fullscreen Viewer</div>
          <div class="card-body">
            <div class="row g-2 align-items-end mb-2">
              <div class="col-12 col-md-3">
                <label class="form-label mb-1">Loop</label>
                <select class="form-select form-select-sm" id="viewerLoop"></select>
              </div>
              <div class="col-12 col-md-3">
                <label class="form-label mb-1">Machine</label>
                <select class="form-select form-select-sm" id="viewerMachine"></select>
              </div>
              <div class="col-12 col-md-2">
                <label class="form-label mb-1">Method</label>
                <select class="form-select form-select-sm" id="viewerMethod">
                  <option value="raw_auto">raw_auto</option>
                  <option value="raw_1s">raw_1s</option>
                  <option value="ds_1m">ds_1m</option>
                  <option value="ds_auto">ds_auto</option>
                </select>
              </div>
              <div class="col-12 col-md-2">
                <label class="form-label mb-1">Range</label>
                <select class="form-select form-select-sm" id="viewerRange">
                  <option value="now-24h">24h</option>
                  <option value="now-7d">7d</option>
                  <option value="now-30d">30d</option>
                  <option value="now-90d">90d</option>
                  <option value="now-365d">365d</option>
                </select>
              </div>
              <div class="col-12 col-md-2 d-grid">
                <button class="btn btn-sm btn-industrial" onclick="refreshViewer()">Build Link</button>
              </div>
            </div>
            <div class="method-help mb-2">
              <div><strong>raw_auto</strong>: raw query with Grafana auto bucket (best general mode).</div>
              <div><strong>raw_1s</strong>: full 1-second samples (heaviest query).</div>
              <div><strong>ds_1m / ds_auto</strong>: pre-downsampled view for long horizons.</div>
            </div>
            <div class="d-flex flex-wrap gap-2 mb-2">
              <button class="btn btn-sm btn-success" onclick="openViewerSameTab()">Open Fullscreen</button>
              <button class="btn btn-sm btn-outline-secondary" onclick="openViewerNewTab()">Open in New Tab</button>
            </div>
            <input class="form-control form-control-sm viewer-url" id="viewerUrlInput" readonly />
            <div class="muted mt-2">
              Tip: Fullscreen Grafana experience is preferred for analysis and avoids iframe layout limits.
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

<script>
let selected = [];
let loops = [];
let tagStatusByNode = {};

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

function optionHtmlFromSelected(selectedNodeid = "") {
  const items = ['<option value="">-- choose tag --</option>'];
  for (const tag of selected) {
    const sel = tag.nodeid === selectedNodeid ? " selected" : "";
    items.push(`<option value="${tag.nodeid}"${sel}>${tag.label} | ${tag.nodeid}</option>`);
  }
  return items.join("");
}

function labelForNodeid(nodeid) {
  const hit = selected.find(x => x.nodeid === nodeid);
  return hit ? hit.label : "";
}

function renderSelection() {
  const tbody = document.querySelector("#selectionTable tbody");
  tbody.innerHTML = "";
  for (const item of selected) {
    const status = tagStatusByNode[item.nodeid] || {};
    const liveOk = status.live_error ? `<span class="bad">${status.live_error}</span>` : `<span class="ok">${status.live_value ?? "-"}</span>`;
    const savedValue = status.saved_value === undefined || status.saved_value === null ? "-" : status.saved_value;
    const savedTime = status.saved_time || "-";
    const age = status.saved_age_seconds === undefined || status.saved_age_seconds === null ? "-" : `${status.saved_age_seconds}s ago`;
    const state = status.saved_age_seconds !== undefined && status.saved_age_seconds !== null && status.saved_age_seconds <= 5
      ? '<span class="state-chip saving">saving</span>'
      : '<span class="state-chip stale">stale</span>';
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${item.label}</td><td><code>${item.nodeid}</code></td><td>${liveOk}</td><td>${savedValue}<div class="small muted">${savedTime} (${age})</div></td><td>${state}</td><td><button class="btn btn-sm btn-outline-danger">Remove</button></td>`;
    tr.querySelector("button").onclick = () => {
      selected = selected.filter(x => x.nodeid !== item.nodeid);
      renderSelection();
      renderLoops();
    };
    tbody.appendChild(tr);
  }
}

function renderLoops() {
  const tbody = document.querySelector("#loopTable tbody");
  tbody.innerHTML = "";
  for (const loop of loops) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input class="form-control form-control-sm" value="${loop.loop_id || ""}" /></td>
      <td><input class="form-control form-control-sm" value="${loop.machine_id || "Kepware"}" /></td>
      <td><select class="form-select form-select-sm">${optionHtmlFromSelected(loop.pv_nodeid || "")}</select></td>
      <td><select class="form-select form-select-sm">${optionHtmlFromSelected(loop.sp_nodeid || "")}</select></td>
      <td><select class="form-select form-select-sm">${optionHtmlFromSelected(loop.co_nodeid || "")}</select></td>
      <td><button class="btn btn-sm btn-outline-danger">Remove</button></td>
    `;
    const idInput = tr.querySelectorAll("input")[0];
    const machineInput = tr.querySelectorAll("input")[1];
    const pvSel = tr.querySelectorAll("select")[0];
    const spSel = tr.querySelectorAll("select")[1];
    const coSel = tr.querySelectorAll("select")[2];
    const delBtn = tr.querySelector("button");

    idInput.oninput = e => loop.loop_id = e.target.value.trim();
    machineInput.oninput = e => loop.machine_id = e.target.value.trim();
    pvSel.onchange = e => loop.pv_nodeid = e.target.value;
    spSel.onchange = e => loop.sp_nodeid = e.target.value;
    coSel.onchange = e => loop.co_nodeid = e.target.value;
    delBtn.onclick = () => {
      loops = loops.filter(x => x !== loop);
      renderLoops();
      renderViewerOptions();
    };
    tbody.appendChild(tr);
  }
  renderViewerOptions();
}

function addLoop() {
  loops.push({
    loop_id: "",
    machine_id: "Kepware",
    pv_nodeid: "",
    sp_nodeid: "",
    co_nodeid: "",
  });
  renderLoops();
}

function renderViewerOptions() {
  const loopSelect = document.getElementById("viewerLoop");
  const machineSelect = document.getElementById("viewerMachine");
  loopSelect.innerHTML = "";
  machineSelect.innerHTML = "";

  const uniqueLoops = [...new Set(loops.map(x => x.loop_id).filter(Boolean))];
  const uniqueMachines = [...new Set(loops.map(x => x.machine_id).filter(Boolean))];
  for (const value of uniqueLoops) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = value;
    loopSelect.appendChild(opt);
  }
  for (const value of uniqueMachines) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = value;
    machineSelect.appendChild(opt);
  }
}

function makeNodeItem(item) {
  const li = document.createElement("li");
  li.className = "tree-item";
  const row = document.createElement("div");
  row.className = "tree-row";

  const name = document.createElement("button");
  name.type = "button";
  name.className = "tree-title";
  name.textContent = item.browse_name;
  name.onclick = () => loadChildren(item.nodeid, childWrap);

  const cls = document.createElement("span");
  cls.className = "tree-class";
  cls.textContent = item.node_class.replace("NodeClass.", "");

  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "btn btn-outline-primary btn-xs";
  addBtn.textContent = "Add";
  addBtn.onclick = () => {
    if (!selected.some(x => x.nodeid === item.nodeid)) {
      selected.push({ label: item.browse_name, nodeid: item.nodeid });
      renderSelection();
      renderLoops();
    }
  };

  const readBtn = document.createElement("button");
  readBtn.type = "button";
  readBtn.className = "btn btn-outline-secondary btn-xs";
  readBtn.textContent = "Read";
  readBtn.onclick = async () => {
    try {
      const data = await api(`/api/read?nodeid=${encodeURIComponent(item.nodeid)}`);
      document.getElementById("status").textContent = `${item.browse_name}: ${data.value}`;
    } catch (e) {
      document.getElementById("status").textContent = e.message;
    }
  };

  row.appendChild(name);
  row.appendChild(cls);
  row.appendChild(addBtn);
  row.appendChild(readBtn);
  const childWrap = document.createElement("ul");
  childWrap.className = "tree-list";
  li.appendChild(row);
  li.appendChild(childWrap);
  return li;
}

async function loadChildren(nodeid, target) {
  try {
    document.getElementById("currentNode").textContent = nodeid || "root";
    const data = await api(`/api/browse?nodeid=${encodeURIComponent(nodeid)}`);
    target.innerHTML = "";
    for (const item of data.children) {
      target.appendChild(makeNodeItem(item));
    }
  } catch (e) {
    document.getElementById("status").textContent = e.message;
  }
}

async function loadRoot() {
  const tree = document.getElementById("tree");
  tree.innerHTML = '<ul class="tree-list" id="rootList"></ul>';
  await loadChildren("", document.getElementById("rootList"));
}

async function refreshSelection() {
  const data = await api("/api/selection");
  selected = data.tags || [];
  renderSelection();
}

function setLoggerBadge(id, isRunning, label) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = `${label} ${isRunning ? "ON" : "OFF"}`;
  el.className = `status-pill ${isRunning ? "on" : "off"}`;
}

async function refreshTagStatus() {
  try {
    const data = await api("/api/tags/status");
    tagStatusByNode = {};
    for (const item of (data.items || [])) {
      tagStatusByNode[item.nodeid] = item;
    }
    setLoggerBadge("rawLoggerState", !!data.raw_logging_running, "Raw Logger");
    setLoggerBadge("loopLoggerState", !!data.loop_logging_running, "Loop Logger");
    renderSelection();
  } catch (e) {
    document.getElementById("status").textContent = `Status refresh failed: ${e.message}`;
  }
}

async function saveSelection() {
  try {
    await api("/api/selection", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({tags: selected}),
    });
    document.getElementById("status").textContent = "Selection saved.";
  } catch (e) {
    document.getElementById("status").textContent = e.message;
  }
}

async function refreshLoops() {
  const data = await api("/api/loops");
  loops = data.loops || [];
  renderLoops();
}

function sanitizeLoopsForSave() {
  const cleaned = [];
  for (const loop of loops) {
    const loopId = (loop.loop_id || "").trim();
    const machineId = (loop.machine_id || "").trim() || "Kepware";
    const pv = loop.pv_nodeid || "";
    const sp = loop.sp_nodeid || "";
    const co = loop.co_nodeid || "";
    if (!loopId) continue;
    cleaned.push({
      loop_id: loopId,
      machine_id: machineId,
      pv_nodeid: pv,
      sp_nodeid: sp,
      co_nodeid: co,
      pv_label: labelForNodeid(pv),
      sp_label: labelForNodeid(sp),
      co_label: labelForNodeid(co),
    });
  }
  return cleaned;
}

async function saveLoops() {
  try {
    const payload = { loops: sanitizeLoopsForSave() };
    await api("/api/loops", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    document.getElementById("status").textContent = "Loop assignments saved.";
    await refreshLoops();
  } catch (e) {
    document.getElementById("status").textContent = e.message;
  }
}

async function startRawLogging() {
  const data = await api("/api/logging/start", { method: "POST" });
  document.getElementById("status").textContent = data.message;
  await refreshTagStatus();
}

async function stopRawLogging() {
  const data = await api("/api/logging/stop", { method: "POST" });
  document.getElementById("status").textContent = data.message;
  await refreshTagStatus();
}

async function startLoopLogging() {
  const data = await api("/api/loops/logging/start", { method: "POST" });
  document.getElementById("status").textContent = data.message;
  await refreshTagStatus();
}

async function stopLoopLogging() {
  const data = await api("/api/loops/logging/stop", { method: "POST" });
  document.getElementById("status").textContent = data.message;
  await refreshTagStatus();
}

async function refreshViewer() {
  const loopId = document.getElementById("viewerLoop").value || "";
  const machineId = document.getElementById("viewerMachine").value || "";
  const method = document.getElementById("viewerMethod").value || "raw_auto";
  const rangeFrom = document.getElementById("viewerRange").value || "now-24h";
  if (!loopId || !machineId) {
    document.getElementById("viewerUrlInput").value = "";
    return "";
  }

  const data = await api(
    `/api/viewer/url?loop_id=${encodeURIComponent(loopId)}&machine_id=${encodeURIComponent(machineId)}&method=${encodeURIComponent(method)}&range_from=${encodeURIComponent(rangeFrom)}`
  );
  document.getElementById("viewerUrlInput").value = data.url;
  return data.url;
}

async function openViewerNewTab() {
  const url = await refreshViewer();
  if (url) window.open(url, "_blank");
}

async function openViewerSameTab() {
  const url = await refreshViewer();
  if (url) window.location.href = url;
}

refreshSelection()
  .then(refreshLoops)
  .then(loadRoot)
  .then(refreshTagStatus)
  .then(refreshViewer)
  .catch(err => {
    document.getElementById("status").textContent = err.message;
  });

setInterval(refreshTagStatus, 3000);
document.getElementById("viewerMethod").addEventListener("change", refreshViewer);
document.getElementById("viewerRange").addEventListener("change", refreshViewer);
document.getElementById("viewerLoop").addEventListener("change", refreshViewer);
document.getElementById("viewerMachine").addEventListener("change", refreshViewer);
</script>
</body>
</html>
"""



@app.get("/api/browse")
def api_browse():
    nodeid = request.args.get("nodeid", "")
    with opc_lock:
        client = ensure_opc_client()
        node = client.nodes.objects if not nodeid else client.get_node(nodeid)
        children = []
        for child in node.get_children():
            try:
                children.append(tree_item(child))
            except Exception:
                continue
    return {"children": children}


@app.get("/api/read")
def api_read():
    nodeid = request.args.get("nodeid", "")
    if not nodeid:
        return {"error": "Missing nodeid"}, 400
    with opc_lock:
        client = ensure_opc_client()
        value = client.get_node(nodeid).read_value()
    return {"nodeid": nodeid, "value": value}


@app.get("/api/selection")
def api_get_selection():
    return load_selection()


@app.post("/api/selection")
def api_save_selection():
    payload = request.get_json(silent=True) or {}
    tags = payload.get("tags", [])
    clean_tags = []
    for item in tags:
        nodeid = str(item.get("nodeid", "")).strip()
        label = str(item.get("label", "")).strip() or nodeid
        if nodeid:
            clean_tags.append({"label": label, "nodeid": nodeid})
    save_selection({"tags": clean_tags})
    return {"saved": len(clean_tags)}


@app.get("/api/tags/status")
def api_tags_status():
    now = datetime.now(timezone.utc)
    influx = get_influx_client()
    tags = load_selection().get("tags", [])

    items = []
    with opc_lock:
        client = ensure_opc_client()
        for item in tags:
            nodeid = str(item.get("nodeid", "")).strip()
            label = str(item.get("label", "")).strip() or nodeid
            if not nodeid:
                continue

            live_value = None
            live_error = ""
            try:
                live_value = client.get_node(nodeid).read_value()
            except Exception as exc:
                live_error = str(exc)

            query = (
                f'SELECT LAST("value") AS value '
                f'FROM "{RAW_TAG_MEASUREMENT}" '
                f"WHERE \"nodeid\" = '{influx_escape(nodeid)}'"
            )
            saved_value = None
            saved_time = ""
            saved_age_seconds = None
            try:
                points = list(influx.query(query).get_points())
                if points:
                    saved_value = points[0].get("value")
                    saved_time = points[0].get("time", "")
                    ts = parse_utc_time(saved_time)
                    if ts is not None:
                        saved_age_seconds = int((now - ts).total_seconds())
            except Exception:
                pass

            items.append(
                {
                    "label": label,
                    "nodeid": nodeid,
                    "live_value": live_value,
                    "live_error": live_error,
                    "saved_value": saved_value,
                    "saved_time": saved_time,
                    "saved_age_seconds": saved_age_seconds,
                }
            )

    with raw_logger_lock:
        raw_running = raw_logger_running
    with loop_logger_lock:
        loop_running = loop_logger_running

    return {"raw_logging_running": raw_running, "loop_logging_running": loop_running, "items": items}


@app.get("/api/loops")
def api_get_loops():
    return load_loop_assignments()


@app.post("/api/loops")
def api_save_loops():
    payload = request.get_json(silent=True) or {}
    loops = payload.get("loops", [])
    clean = []
    for loop in loops:
        loop_id = str(loop.get("loop_id", "")).strip()
        machine_id = str(loop.get("machine_id", "")).strip() or "Kepware"
        pv_nodeid = str(loop.get("pv_nodeid", "")).strip()
        sp_nodeid = str(loop.get("sp_nodeid", "")).strip()
        co_nodeid = str(loop.get("co_nodeid", "")).strip()
        pv_label = str(loop.get("pv_label", "")).strip()
        sp_label = str(loop.get("sp_label", "")).strip()
        co_label = str(loop.get("co_label", "")).strip()
        if loop_id:
            clean.append(
                {
                    "loop_id": loop_id,
                    "machine_id": machine_id,
                    "pv_nodeid": pv_nodeid,
                    "sp_nodeid": sp_nodeid,
                    "co_nodeid": co_nodeid,
                    "pv_label": pv_label,
                    "sp_label": sp_label,
                    "co_label": co_label,
                }
            )
    save_loop_assignments({"loops": clean})
    return {"saved": len(clean)}


@app.get("/api/logging/status")
def api_raw_logging_status():
    with raw_logger_lock:
        return {"running": raw_logger_running}


@app.post("/api/logging/start")
def api_raw_logging_start():
    global raw_logger_running, raw_logger_thread
    with raw_logger_lock:
        if raw_logger_running:
            return {"message": "Raw tag logging already running."}
        raw_logger_running = True
        raw_logger_thread = threading.Thread(target=raw_logging_loop, daemon=True)
        raw_logger_thread.start()
    return {"message": "Raw tag logging started."}


@app.post("/api/logging/stop")
def api_raw_logging_stop():
    global raw_logger_running
    with raw_logger_lock:
        raw_logger_running = False
    return {"message": "Raw tag logging stopped."}


@app.get("/api/loops/logging/status")
def api_loop_logging_status():
    with loop_logger_lock:
        return {"running": loop_logger_running}


@app.post("/api/loops/logging/start")
def api_loop_logging_start():
    global loop_logger_running, loop_logger_thread
    with loop_logger_lock:
        if loop_logger_running:
            return {"message": "Loop logging already running."}
        loop_logger_running = True
        loop_logger_thread = threading.Thread(target=loop_logging_loop, daemon=True)
        loop_logger_thread.start()
    return {"message": "Loop logging started."}


@app.post("/api/loops/logging/stop")
def api_loop_logging_stop():
    global loop_logger_running
    with loop_logger_lock:
        loop_logger_running = False
    return {"message": "Loop logging stopped."}


@app.get("/api/viewer/url")
def api_viewer_url():
    loop_id = str(request.args.get("loop_id", "")).strip()
    machine_id = str(request.args.get("machine_id", "")).strip()
    method = str(request.args.get("method", "raw_auto")).strip() or "raw_auto"
    range_from = str(request.args.get("range_from", "now-24h")).strip() or "now-24h"

    if not loop_id or not machine_id:
        return {"error": "loop_id and machine_id are required"}, 400
    return {
        "url": grafana_url(
            loop_id=loop_id,
            machine_id=machine_id,
            method=method,
            range_from=range_from,
        )
    }


# Backward-compatible aliases for older tooling.
@app.get("/api/pid/templates")
def api_pid_templates_alias_get():
    return api_get_loops()


@app.post("/api/pid/templates")
def api_pid_templates_alias_save():
    return api_save_loops()


@app.get("/api/pid/logging/status")
def api_pid_logging_alias_status():
    return api_loop_logging_status()


@app.post("/api/pid/logging/start")
def api_pid_logging_alias_start():
    return api_loop_logging_start()


@app.post("/api/pid/logging/stop")
def api_pid_logging_alias_stop():
    return api_loop_logging_stop()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False)
