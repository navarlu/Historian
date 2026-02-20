# Historian MVP Plan (Kepware OPC UA -> InfluxDB -> Grafana)

## Objective
Build an MVP historian that:
1. Reads tags from Kepware OPC UA.
2. Stores values in InfluxDB.
3. Visualizes trends in Grafana.

The approach is test-first in the local `test/` folder before wiring the full app.

## Current Status (validated on February 13, 2026)

### Step 1: OPC UA Client Smoke Test
Status: `completed`

What was implemented:
- `test/read_opcua.py`
- Dependency switched to maintained FreeOpcUa package: `asyncua==1.1.8` (sync client API).

What was observed:
- Connection to Kepware endpoint `opc.tcp://localhost:49320` works.
- Hardcoded NodeIds with `Channel1...` were not valid in this local server.
- Tag discovery found working NodeIds:
  - `Ramp1`: `ns=2;s=Simulation Examples.Functions.Ramp1`
  - `Sine1`: `ns=2;s=Simulation Examples.Functions.Sine1`

Notes:
- NodeIds can differ across Kepware projects/devices.
- Keep discovery fallback in early development to avoid brittle config.

## Step 2: InfluxDB Write/Read Smoke Test
Status: `completed` (validated on February 13, 2026)

Goal:
- Verify Python can write points to InfluxDB and read them back.

What is implemented:
- `test/influx_test.py`
  - Creates DB if missing (`opcuadata`)
  - Writes one point to `test_measurement`
  - Reads latest rows back with `SELECT`
- Dependency pinned: `influxdb==5.3.2`

Expected output:
- `Point written.`
- Printed row(s) containing `value: 123.45`

Observed output:
- `Point written.`
- `{'time': '2026-02-13T08:53:10.161133Z', 'test_tag': 'demo', 'value': 123.45}`

## Local Infrastructure (Docker)

`docker-compose.yaml` now contains:
- `influxdb` (`influxdb:1.8`) on `localhost:8086`
- `grafana` (`grafana/grafana:latest`) on `localhost:3000`
- Persistent volumes for both services

Default Grafana login for MVP:
- user: `admin`
- password: `admin`

## Next Steps

### Step 3: Wire OPC UA -> InfluxDB Collector
Status: `pending`

Build a loop collector module that:
- Polls selected OPC UA tags every 1s.
- Writes normalized points into measurement `pid_loop_data`.
- Uses tags:
  - `loop_id`
  - `signal` (optional design decision)
- Uses numeric fields:
  - `PV`, `SP`, `OUT` (or one `value` field per signal)

### Step 4: Minimal Loop Configuration
Status: `pending`

Implement basic config source (file or in-memory) for mapping:
- `loop_id -> {PV nodeid, SP nodeid, OUT nodeid}`

Start simple with file-backed JSON.

### Step 5: Grafana Integration
Status: `pending`

1. Create InfluxDB data source in Grafana.
2. Create MVP dashboard:
   - Panel 1: PV vs SP
   - Panel 2: OUT
   - Variable: `loop_id`
3. Optionally automate dashboard creation by API after core pipeline is stable.

## Risks and Decisions

1. OPC UA addressing variance:
   - NodeIds are environment-specific; discovery + persisted mapping is required.
2. InfluxDB version path:
   - Current plan uses InfluxDB 1.8 + `influxdb` Python client for MVP speed.
3. Robustness:
   - Add reconnect logic for both OPC UA and InfluxDB before production use.

## Runbook (Smoke Tests)

1. Start infra:
```bash
docker compose up -d influxdb grafana
```

2. Install Python deps into `.venv`:
```bash
uv pip install -r requirements.txt
```

3. OPC UA smoke test:
```bash
uv run python test/read_opcua.py
```

4. InfluxDB smoke test:
```bash
uv run python test/influx_test.py
```
