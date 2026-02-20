import json
import threading
import time
from pathlib import Path

from asyncua.sync import Client as OpcUaClient
from flask import Flask, request
from influxdb import InfluxDBClient

OPCUA_URL = "opc.tcp://localhost:49320"
INFLUX_HOST = "localhost"
INFLUX_PORT = 8086
INFLUX_DB = "opcuadata"
RAW_MEASUREMENT = "selected_tag_data"
PID_MEASUREMENT = "pid_loop_data"
POLL_INTERVAL_SECONDS = 1.0
SELECTION_FILE = Path("test/tag_selection.json")
PID_TEMPLATES_FILE = Path("test/pid_templates.json")

app = Flask(__name__)

opc_lock = threading.Lock()
opc_client = None

raw_logger_lock = threading.Lock()
raw_logger_thread = None
raw_logger_running = False

pid_logger_lock = threading.Lock()
pid_logger_thread = None
pid_logger_running = False


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


def load_pid_templates() -> dict:
    return load_json(PID_TEMPLATES_FILE, {"templates": []})


def save_pid_templates(payload: dict) -> None:
    save_json(PID_TEMPLATES_FILE, payload)


def safe_float(value):
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    raise ValueError(f"Non-numeric value: {value!r}")


def raw_logging_loop() -> None:
    global raw_logger_running
    influx = get_influx_client()

    while True:
        with raw_logger_lock:
            if not raw_logger_running:
                break

        selection = load_selection().get("tags", [])
        points = []

        with opc_lock:
            client = ensure_opc_client()
            for item in selection:
                nodeid = item.get("nodeid", "")
                label = item.get("label", nodeid)
                if not nodeid:
                    continue
                try:
                    value = client.get_node(nodeid).read_value()
                    numeric_value = safe_float(value)
                except Exception:
                    continue

                points.append(
                    {
                        "measurement": RAW_MEASUREMENT,
                        "tags": {"nodeid": nodeid, "label": label},
                        "fields": {"value": numeric_value},
                    }
                )

        if points:
            influx.write_points(points)

        time.sleep(POLL_INTERVAL_SECONDS)


def pid_logging_loop() -> None:
    global pid_logger_running
    influx = get_influx_client()

    while True:
        with pid_logger_lock:
            if not pid_logger_running:
                break

        templates = load_pid_templates().get("templates", [])
        points = []

        with opc_lock:
            client = ensure_opc_client()
            for tpl in templates:
                template_id = str(tpl.get("id", "")).strip()
                pv_nodeid = str(tpl.get("pv_nodeid", "")).strip()
                sp_nodeid = str(tpl.get("sp_nodeid", "")).strip()
                co_nodeid = str(tpl.get("co_nodeid", "")).strip()

                if not template_id or not pv_nodeid or not sp_nodeid or not co_nodeid:
                    continue

                try:
                    pv_value = safe_float(client.get_node(pv_nodeid).read_value())
                    sp_value = safe_float(client.get_node(sp_nodeid).read_value())
                    co_value = safe_float(client.get_node(co_nodeid).read_value())
                except Exception:
                    continue

                points.append(
                    {
                        "measurement": PID_MEASUREMENT,
                        "tags": {
                            "loop_id": template_id,
                        },
                        "fields": {
                            "PV": pv_value,
                            "SP": sp_value,
                            "CO": co_value,
                        },
                    }
                )

        if points:
            influx.write_points(points)

        time.sleep(POLL_INTERVAL_SECONDS)


def tree_item(node) -> dict:
    browse_name = node.read_browse_name().Name
    node_class = str(node.read_node_class())
    return {
        "browse_name": browse_name,
        "nodeid": node.nodeid.to_string(),
        "node_class": node_class,
    }


@app.get("/")
def index():
    return """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Historian Tag Selector</title>
  <style>
    body { font-family: Segoe UI, sans-serif; margin: 0; background: #f2f4f8; }
    .wrap { display: grid; grid-template-columns: 1.3fr 1fr; gap: 12px; padding: 12px; }
    .card { background: #fff; border-radius: 10px; padding: 12px; box-shadow: 0 1px 6px rgba(0,0,0,0.08); margin-bottom: 12px; }
    .row { display: flex; gap: 8px; align-items: center; margin-bottom: 8px; flex-wrap: wrap; }
    button { border: none; background: #0a66c2; color: #fff; padding: 8px 10px; border-radius: 8px; cursor: pointer; }
    button.alt { background: #7a869a; }
    button.danger { background: #c62828; }
    ul { list-style: none; padding-left: 18px; }
    li { margin: 4px 0; }
    .node { cursor: pointer; padding: 2px 4px; border-radius: 4px; }
    .node:hover { background: #e9f2ff; }
    table { width: 100%; border-collapse: collapse; }
    td, th { border-bottom: 1px solid #e9edf3; padding: 6px; text-align: left; font-size: 13px; }
    .muted { color: #606a7a; font-size: 12px; }
    input, select { padding: 6px; border: 1px solid #ccd5e3; border-radius: 6px; min-width: 120px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h3>OPC UA Browse</h3>
      <div class="row">
        <button onclick="loadRoot()">Load Root</button>
        <span class="muted" id="currentNode"></span>
      </div>
      <div id="tree"></div>
    </div>
    <div>
      <div class="card">
        <h3>Selected Tags</h3>
        <div class="row">
          <button onclick="saveSelection()">Save Selection</button>
          <button class="alt" onclick="startRawLogging()">Start Raw Logging</button>
          <button class="danger" onclick="stopRawLogging()">Stop Raw Logging</button>
        </div>
        <div class="muted" id="status"></div>
        <table id="selectionTable">
          <thead><tr><th>Label</th><th>NodeId</th><th></th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
      <div class="card">
        <h3>PID Templates</h3>
        <div class="row">
          <button onclick="addTemplate()">Add Template</button>
          <button onclick="saveTemplates()">Save Templates</button>
          <button class="alt" onclick="startPidLogging()">Start PID Logging</button>
          <button class="danger" onclick="stopPidLogging()">Stop PID Logging</button>
        </div>
        <table id="templateTable">
          <thead><tr><th>Loop ID</th><th>PV</th><th>SP</th><th>CO</th><th></th></tr></thead>
          <tbody></tbody>
        </table>
      </div>
    </div>
  </div>

<script>
let selected = [];
let templates = [];

async function api(path, options = {}) {
  const res = await fetch(path, options);
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || 'Request failed');
  return data;
}

function tagOptionsHtml(currentNodeid = '') {
  const opts = ['<option value="">-- choose --</option>'];
  for (const t of selected) {
    const sel = t.nodeid === currentNodeid ? ' selected' : '';
    opts.push(`<option value="${t.nodeid}"${sel}>${t.label} | ${t.nodeid}</option>`);
  }
  return opts.join('');
}

function renderSelection() {
  const tbody = document.querySelector('#selectionTable tbody');
  tbody.innerHTML = '';
  for (const item of selected) {
    const tr = document.createElement('tr');
    tr.innerHTML = `<td>${item.label}</td><td>${item.nodeid}</td><td><button class="danger">X</button></td>`;
    tr.querySelector('button').onclick = () => {
      selected = selected.filter(x => x.nodeid !== item.nodeid);
      renderSelection();
      renderTemplates();
    };
    tbody.appendChild(tr);
  }
}

function renderTemplates() {
  const tbody = document.querySelector('#templateTable tbody');
  tbody.innerHTML = '';

  for (const tpl of templates) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><input value="${tpl.id || ''}" /></td>
      <td><select>${tagOptionsHtml(tpl.pv_nodeid || '')}</select></td>
      <td><select>${tagOptionsHtml(tpl.sp_nodeid || '')}</select></td>
      <td><select>${tagOptionsHtml(tpl.co_nodeid || '')}</select></td>
      <td><button class="danger">X</button></td>
    `;

    const [idInput, pvSel, spSel, coSel, delBtn] = [
      tr.querySelectorAll('input')[0],
      tr.querySelectorAll('select')[0],
      tr.querySelectorAll('select')[1],
      tr.querySelectorAll('select')[2],
      tr.querySelector('button')
    ];

    idInput.oninput = (e) => tpl.id = e.target.value;
    pvSel.onchange = (e) => tpl.pv_nodeid = e.target.value;
    spSel.onchange = (e) => tpl.sp_nodeid = e.target.value;
    coSel.onchange = (e) => tpl.co_nodeid = e.target.value;
    delBtn.onclick = () => {
      templates = templates.filter(x => x !== tpl);
      renderTemplates();
    };

    tbody.appendChild(tr);
  }
}

function addTemplate() {
  templates.push({ id: '', pv_nodeid: '', sp_nodeid: '', co_nodeid: '' });
  renderTemplates();
}

function makeNodeItem(item) {
  const li = document.createElement('li');
  const row = document.createElement('div');
  row.className = 'row';

  const n = document.createElement('span');
  n.className = 'node';
  n.textContent = `${item.browse_name} (${item.node_class})`;
  n.onclick = () => loadChildren(item.nodeid, childWrap);

  const add = document.createElement('button');
  add.textContent = 'Add';
  add.onclick = () => {
    if (!selected.some(x => x.nodeid === item.nodeid)) {
      selected.push({ label: item.browse_name, nodeid: item.nodeid });
      renderSelection();
      renderTemplates();
    }
  };

  const read = document.createElement('button');
  read.className = 'alt';
  read.textContent = 'Read';
  read.onclick = async () => {
    try {
      const data = await api(`/api/read?nodeid=${encodeURIComponent(item.nodeid)}`);
      document.getElementById('status').textContent = `${item.browse_name}: ${data.value}`;
    } catch (e) {
      document.getElementById('status').textContent = e.message;
    }
  };

  row.appendChild(n);
  row.appendChild(add);
  row.appendChild(read);

  const childWrap = document.createElement('ul');
  li.appendChild(row);
  li.appendChild(childWrap);
  return li;
}

async function loadChildren(nodeid, target) {
  try {
    document.getElementById('currentNode').textContent = nodeid;
    const data = await api(`/api/browse?nodeid=${encodeURIComponent(nodeid)}`);
    target.innerHTML = '';
    for (const item of data.children) target.appendChild(makeNodeItem(item));
  } catch (e) {
    document.getElementById('status').textContent = e.message;
  }
}

async function loadRoot() {
  const tree = document.getElementById('tree');
  tree.innerHTML = '<ul id="rootList"></ul>';
  await loadChildren('', document.getElementById('rootList'));
}

async function saveSelection() {
  try {
    await api('/api/selection', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tags: selected}),
    });
    document.getElementById('status').textContent = 'Selection saved.';
  } catch (e) {
    document.getElementById('status').textContent = e.message;
  }
}

async function refreshSelection() {
  const data = await api('/api/selection');
  selected = data.tags || [];
  renderSelection();
}

async function saveTemplates() {
  try {
    await api('/api/pid/templates', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({templates}),
    });
    document.getElementById('status').textContent = 'PID templates saved.';
  } catch (e) {
    document.getElementById('status').textContent = e.message;
  }
}

async function refreshTemplates() {
  const data = await api('/api/pid/templates');
  templates = data.templates || [];
  renderTemplates();
}

async function startRawLogging() {
  const data = await api('/api/logging/start', { method: 'POST' });
  document.getElementById('status').textContent = data.message;
}

async function stopRawLogging() {
  const data = await api('/api/logging/stop', { method: 'POST' });
  document.getElementById('status').textContent = data.message;
}

async function startPidLogging() {
  const data = await api('/api/pid/logging/start', { method: 'POST' });
  document.getElementById('status').textContent = data.message;
}

async function stopPidLogging() {
  const data = await api('/api/pid/logging/stop', { method: 'POST' });
  document.getElementById('status').textContent = data.message;
}

refreshSelection().then(refreshTemplates).then(loadRoot);
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
        for ch in node.get_children():
            try:
                children.append(tree_item(ch))
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


@app.get("/api/pid/templates")
def api_get_pid_templates():
    return load_pid_templates()


@app.post("/api/pid/templates")
def api_save_pid_templates():
    payload = request.get_json(silent=True) or {}
    templates = payload.get("templates", [])
    clean_templates = []
    for tpl in templates:
        template_id = str(tpl.get("id", "")).strip()
        pv_nodeid = str(tpl.get("pv_nodeid", "")).strip()
        sp_nodeid = str(tpl.get("sp_nodeid", "")).strip()
        co_nodeid = str(tpl.get("co_nodeid", "")).strip()
        clean_templates.append(
            {
                "id": template_id,
                "pv_nodeid": pv_nodeid,
                "sp_nodeid": sp_nodeid,
                "co_nodeid": co_nodeid,
            }
        )
    save_pid_templates({"templates": clean_templates})
    return {"saved": len(clean_templates)}


@app.get("/api/logging/status")
def api_raw_logging_status():
    with raw_logger_lock:
        return {"running": raw_logger_running}


@app.post("/api/logging/start")
def api_raw_logging_start():
    global raw_logger_running, raw_logger_thread
    with raw_logger_lock:
        if raw_logger_running:
            return {"message": "Raw logging already running."}
        raw_logger_running = True
        raw_logger_thread = threading.Thread(target=raw_logging_loop, daemon=True)
        raw_logger_thread.start()
    return {"message": "Raw logging started."}


@app.post("/api/logging/stop")
def api_raw_logging_stop():
    global raw_logger_running
    with raw_logger_lock:
        raw_logger_running = False
    return {"message": "Raw logging stopped."}


@app.get("/api/pid/logging/status")
def api_pid_logging_status():
    with pid_logger_lock:
        return {"running": pid_logger_running}


@app.post("/api/pid/logging/start")
def api_pid_logging_start():
    global pid_logger_running, pid_logger_thread
    with pid_logger_lock:
        if pid_logger_running:
            return {"message": "PID logging already running."}
        pid_logger_running = True
        pid_logger_thread = threading.Thread(target=pid_logging_loop, daemon=True)
        pid_logger_thread.start()
    return {"message": "PID logging started."}


@app.post("/api/pid/logging/stop")
def api_pid_logging_stop():
    global pid_logger_running
    with pid_logger_lock:
        pid_logger_running = False
    return {"message": "PID logging stopped."}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, debug=False)
