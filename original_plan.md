# Building a Local OPC UA PID Loop Monitoring MVP with Docker, Python, and Grafana

## Architecture Overview

This MVP system connects a Python OPC UA client to a local Kepware OPC UA server, collects selected tag values into InfluxDB, and visualizes them with Grafana.

**Components**

- **OPC UA Server (Kepware)** — already running on localhost, providing simulation tags.
- **OPC UA Client App (Python)** — runs in Docker, uses FreeOpcUa to browse and read tag values.
- **InfluxDB** — time-series database to store tag readings (1-second interval).
- **Grafana** — dashboards; accessed via HTTP API to auto-create a templated dashboard for PID loops.

**High-level flow**

1. User selects OPC UA tags and assigns them to PID loop roles (PV, SP, OUT) via a simple web UI (Flask/FastAPI).
2. Python polls those tags every second and writes the values to InfluxDB.
3. Grafana is preconfigured to use InfluxDB and has an auto-generated dashboard with template variables (e.g., `loop_id`) for real-time and historical trends.

---

## Step 1: Project Setup with Docker Compose

Create a project folder (e.g., `opcua_pid_mvp`) and add `docker-compose.yml`:

```yaml
version: "3.8"
services:
  influxdb:
    image: influxdb:1.8
    container_name: influxdb
    ports:
      - "8086:8086"
    environment:
      INFLUXDB_DB: opcuadata   # creates a database named 'opcuadata'
      INFLUXDB_HTTP_AUTH_ENABLED: "false"
    volumes:
      - influxdata:/var/lib/influxdb

  grafana:
    image: grafana/grafana:latest
    container_name: grafana
    ports:
      - "3000:3000"
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=admin   # default user is admin
    volumes:
      - grafanadata:/var/lib/grafana

  opcua_client_app:
    build: .
    container_name: opcua-client
    depends_on:
      - influxdb
      - grafana
    # If OPC UA server is on the host, use host networking for direct access (Linux)
    # network_mode: "host"
    # Otherwise, for Docker Desktop, use host.docker.internal to reach host
    environment:
      - OPCUA_ENDPOINT=opc.tcp://host.docker.internal:49320  # Kepware endpoint URL
      - INFLUXDB_URL=http://influxdb:8086
      - INFLUXDB_DB=opcuadata
      - GRAFANA_URL=http://grafana:3000
      - GRAFANA_API_KEY=YOUR_GRAFANA_API_KEY   # used to auth Grafana API
    ports:
      - "5000:5000"   # expose Flask app (UI)

volumes:
  influxdata:
  grafanadata:
```

### Notes

- This uses **InfluxDB 1.8** (no auth for simplicity) and **Grafana latest**.
- `depends_on` ensures InfluxDB and Grafana start before the app.
- Connectivity to Kepware (host machine):
  - **Docker Desktop (Windows/Mac):** use `host.docker.internal`.
  - **Linux:** you can use `network_mode: "host"` or otherwise adjust addressing/firewall.
- Grafana admin password is set to `admin` for convenience.
- Create a Grafana API key and set `GRAFANA_API_KEY` later.

### Dockerfile for the Python app

Create `Dockerfile` in project root:

```dockerfile
FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
CMD ["python", "app.py"]
```

### requirements.txt

Example dependencies:

```txt
opcua
flask
influxdb
requests
```

Start everything:

```bash
docker-compose up --build
```

---

## Step 2: Connecting to OPC UA and Browsing Tags

Implement OPC UA client logic in `app.py` (or a module). Using FreeOpcUa (`opcua.Client`), connect to Kepware and browse tags.

```python
import os
from opcua import Client, ua

opcua_url = os.environ.get("OPCUA_ENDPOINT", "opc.tcp://localhost:4840")

client = Client(opcua_url)
client.set_user("")      # if security is disabled
client.set_password("")
client.connect()
print(f"Connected to OPC UA server at {opcua_url}")

root = client.get_root_node()
objects = client.get_objects_node()
print("Root children:", root.get_children())

# Navigate to "Simulation Examples" > "Functions"
sim_folder = None
for child in objects.get_children():
    name = child.get_browse_name().Name
    if name == "Simulation Examples":
        sim_folder = child
        break

functions_folder = None
if sim_folder:
    for child in sim_folder.get_children():
        if child.get_browse_name().Name == "Functions":
            functions_folder = child
            break

available_tags = []
if functions_folder:
    for tag_node in functions_folder.get_children():
        tag_name = tag_node.get_browse_name().Name
        available_tags.append({
            "name": tag_name,
            "nodeid": tag_node.nodeid.to_string()
        })

print("Discovered tags:", [t["name"] for t in available_tags])
```

### Tip

- This assumes Kepware demo tags are under `Simulation Examples/Functions`.
- If your server organizes tags differently:
  - print `objects.get_children()` and explore
  - use UAExpert to inspect namespaces and NodeIds
  - if you know namespaces, you can also try:
    `root.get_child(["0:Objects", "2:Simulation Examples", "2:Functions"])`

---

## Step 3: Building a Simple UI for PID Loop Configuration

A minimal Flask UI lets the user map tags to loop roles: **PV**, **SP**, **OUT**.

```python
import json
from flask import Flask, request

app = Flask(__name__)
loop_config = {}  # {loop_id: {"PV": nodeid, "SP": nodeid, "OUT": nodeid}}

@app.route("/")
def index():
    options = "".join(
        [f"<option value='{t['nodeid']}'>{t['name']}</option>" for t in available_tags]
    )
    return f"""
        <h3>Configure PID Loop</h3>
        <form method='POST' action='/configure'>
            Loop ID: <input name='loop_id' value='loop1'><br><br>
            PV Tag: <select name='pv'>{options}</select><br><br>
            SP Tag: <select name='sp'>{options}</select><br><br>
            OUT Tag: <select name='out'>{options}</select><br><br>
            <button type='submit'>Save Configuration</button>
        </form>
    """

@app.route("/configure", methods=["POST"])
def configure_loop():
    loop_id = request.form["loop_id"]
    loop_config[loop_id] = {
        "PV": request.form["pv"],
        "SP": request.form["sp"],
        "OUT": request.form["out"],
    }

    with open("loop_config.json", "w", encoding="utf-8") as f:
        json.dump(loop_config, f, ensure_ascii=False, indent=2)

    return f"Configured loop '{loop_id}' with selected tags. You can now start data collection."
```

This is intentionally basic (HTML strings). You can replace it with Jinja2 templates or a small frontend later.

---

## Step 4: Storing Tag Data in InfluxDB at 1s Intervals

After configuration, poll tag values every second and write to InfluxDB.

```python
import os
import threading
import time
from influxdb import InfluxDBClient

# Docker service name "influxdb" is resolvable inside the compose network
influx = InfluxDBClient(
    host="influxdb",
    port=8086,
    database=os.environ.get("INFLUXDB_DB", "opcuadata"),
)

def poll_loop_values():
    while True:
        if not loop_config:
            time.sleep(1)
            continue

        for loop_id, tags in loop_config.items():
            try:
                pv_val = client.get_node(tags["PV"]).get_value()
                sp_val = client.get_node(tags["SP"]).get_value()
                out_val = client.get_node(tags["OUT"]).get_value()
            except Exception as e:
                print(f"OPC UA read error: {e}")
                continue

            point = {
                "measurement": "pid_loop_data",
                "tags": {"loop_id": loop_id},
                "fields": {
                    "PV": float(pv_val),
                    "SP": float(sp_val),
                    "OUT": float(out_val),
                },
            }

            try:
                influx.write_points([point])
            except Exception as e:
                print(f"InfluxDB write error: {e}")

        time.sleep(1)

threading.Thread(target=poll_loop_values, daemon=True).start()
```

### Implementation notes

- Values are cast to `float(...)` assuming numeric tags.
- All loops share the same measurement (`pid_loop_data`) and are separated by a tag (`loop_id`), which is ideal for Grafana templating.
- Polling is fine for an MVP; OPC UA subscriptions would be more efficient.

### Alternative: Telegraf

Instead of custom polling, you can use **Telegraf** (OPC UA input plugin → InfluxDB output). For this MVP, Python is kept for clarity.

---

## Step 5: Generating Grafana Dashboards via API

With data in InfluxDB, use Grafana’s HTTP API to create/update a dashboard automatically.

### Grafana data source

Ensure Grafana has an InfluxDB data source named **`InfluxDB`** pointing to:

- URL: `http://influxdb:8086`
- Database: `opcuadata`
- Auth: off (for MVP)

You can configure this via UI or provisioning.

### API key

Create a Grafana API key (Admin) and set it in:

- `GRAFANA_API_KEY` env var (compose)

### Python dashboard creation

```python
import os
import requests

def create_grafana_dashboard():
    api_url = os.environ.get("GRAFANA_URL", "http://localhost:3000")
    api_key = os.environ.get("GRAFANA_API_KEY")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    dashboard = {
      "uid": "pid-loops",
      "title": "PID Loops Overview",
      "time": {"from": "now-1h", "to": "now"},
      "templating": {
        "list": [
          {
            "name": "loop_id",
            "type": "query",
            "datasource": "InfluxDB",
            "refresh": 1,
            "query": 'SHOW TAG VALUES FROM "pid_loop_data" WITH KEY = "loop_id"'
          }
        ]
      },
      "panels": [
        {
          "type": "timeseries",
          "title": "Process Variable (PV) vs Setpoint (SP)",
          "datasource": "InfluxDB",
          "targets": [
            {
              "measurement": "pid_loop_data",
              "groupBy": [
                {"type": "time", "params": ["$__interval"]},
                {"type": "tag", "params": ["loop_id"]}
              ],
              "tags": [{"key": "loop_id", "operator": "=", "value": "$loop_id"}],
              "select": [[{"type": "field", "params": ["PV"]}, {"type": "mean", "params": []}]],
              "refId": "A"
            },
            {
              "measurement": "pid_loop_data",
              "groupBy": [
                {"type": "time", "params": ["$__interval"]},
                {"type": "tag", "params": ["loop_id"]}
              ],
              "tags": [{"key": "loop_id", "operator": "=", "value": "$loop_id"}],
              "select": [[{"type": "field", "params": ["SP"]}, {"type": "mean", "params": []}]],
              "refId": "B"
            }
          ],
          "fieldConfig": {"defaults": {"unit": "none"}},
          "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8}
        },
        {
          "type": "timeseries",
          "title": "Controller Output (OUT)",
          "datasource": "InfluxDB",
          "targets": [
            {
              "measurement": "pid_loop_data",
              "groupBy": [
                {"type": "time", "params": ["$__interval"]},
                {"type": "tag", "params": ["loop_id"]}
              ],
              "tags": [{"key": "loop_id", "operator": "=", "value": "$loop_id"}],
              "select": [[{"type": "field", "params": ["OUT"]}, {"type": "mean", "params": []}]],
              "refId": "A"
            }
          ],
          "fieldConfig": {"defaults": {"unit": "none"}},
          "gridPos": {"x": 0, "y": 9, "w": 24, "h": 6}
        }
      ]
    }

    payload = {"dashboard": dashboard, "overwrite": True}
    resp = requests.post(f"{api_url}/api/dashboards/db", json=payload, headers=headers)

    if resp.status_code == 200:
        print("Grafana dashboard created successfully.")
    else:
        print("Failed to create dashboard:", resp.status_code, resp.text)

# Call at startup or after saving configuration
create_grafana_dashboard()
```

### What this dashboard does

- Uses a template variable `loop_id` populated from:
  - `SHOW TAG VALUES FROM "pid_loop_data" WITH KEY = "loop_id"`
- Panel 1 plots **PV vs SP** for the selected loop.
- Panel 2 plots **OUT** for the selected loop.
- `overwrite=True` makes repeated calls idempotent (updates the same dashboard).

---

## Future Improvements and Expansion

- **Multiple loops & dynamic updates:** allow adding many loops; compare multiple `loop_id` values.
- **Alarms & events:** PV/SP deviation, OUT limits; alerting via Grafana or app logic.
- **OPC UA subscriptions:** push-based updates reduce load and latency vs polling.
- **Better config storage:** robust schema, versioning, DB-backed config, API endpoints.
- **Telegraf pipeline:** offload data collection to Telegraf (OPC UA → InfluxDB).
- **Grafana provisioning:** dashboards and data sources as code via mounted provisioning files.
- **Derived metrics:** error (SP–PV), performance KPIs, controller diagnostics.
- **Security & robustness:** OPC UA security (certs/users), InfluxDB auth, run Flask behind Gunicorn, reconnect handling.

---

## Quick demo checklist

1. Kepware OPC UA server running and reachable (endpoint and port correct).
2. `docker-compose up --build`
3. Open Flask UI: `http://localhost:5000` and configure `loop_id`, PV/SP/OUT.
4. Confirm points are written to InfluxDB (optional).
5. Open Grafana: `http://localhost:3000` (admin / password from compose).
6. Dashboard appears; select `loop_id` and view PV/SP/OUT trends.