# Historian (InfluxDB + Grafana + OPC UA UI)

Local historian stack for high-frequency PID data:
- InfluxDB 1.8 for storage
- Grafana for visualization
- Python app/scripts for synthetic data, downsampling, and loop/tag management

## Showcase

![Historian Dashboard Showcase](docs/assets/showcase.png)

## Repository Structure

- `app/historian_ui.py`: Historian web UI (OPC UA browse -> tag selection -> loop mapping -> logging -> Grafana link)
- `scripts/influx/setup_hf_schema.py`: Influx schema + retention policies + CQ setup
- `scripts/influx/seed_pid_sim.py`: 1s synthetic PID data generator/writer
- `scripts/influx/backfill_downsample.py`: backfill downsample series from raw data
- `scripts/influx/query_benchmark.py`: benchmark queries (raw vs downsample)
- `scripts/grafana/push_hf_dashboard.py`: push/update Grafana dashboard JSON via API
- `grafana/`: Grafana provisioning files
- `state/`: runtime UI state (`tag_selection.json`, `loop_assignments.json`)

## 1. Start Docker Services

```powershell
docker compose up -d
```

Check status:

```powershell
docker compose ps
```

Stop services:

```powershell
docker compose down
```

## 2. Install Python Dependencies

If `.venv` does not exist:

```powershell
uv venv
uv pip install -r requirements.txt
```

If `.venv` already exists:

```powershell
uv pip install -r requirements.txt
```

## 3. Open Grafana

- URL: `http://localhost:3000`
- User: `admin`
- Password: from `.env` (`GRAFANA_PASSWORD`)

## 4. Prepare Schema and Dashboard

```powershell
uv run python scripts/influx/setup_hf_schema.py
uv run python scripts/grafana/push_hf_dashboard.py
```

Fallback (without `uv` in shell):

```powershell
.venv\Scripts\python.exe scripts/influx/setup_hf_schema.py
.venv\Scripts\python.exe scripts/grafana/push_hf_dashboard.py
```

## 5. Generate Historical Data

1 year at 1-second rate:

```powershell
uv run python scripts/influx/seed_pid_sim.py
```

Writes to:
- measurement: `pid_loop_hf_raw`
- retention policy: `hf_raw_400d`
- loop: `hf_test_loop_002`
- machine: `DeviceSyntheticPID`

Backfill downsample:

```powershell
$env:HF_LOOP_ID='hf_test_loop_002'
$env:HF_MACHINE_ID='DeviceSyntheticPID'
uv run python scripts/influx/backfill_downsample.py
```

## 6. View in Grafana

Dashboard URL:

`http://localhost:3000/d/pid-hf-1s-benchmark/pid-high-frequency-benchmark`

Use variables:
- `Loop`: `hf_test_loop_002`
- `Machine`: `DeviceSyntheticPID`
- `Method`: `raw_auto`, `raw_1s`, `ds_1m`, `ds_auto`

## 7. Run Historian Web UI

```powershell
uv run python app/historian_ui.py
```

Open:

`http://localhost:5050`

Flow:
1. Browse OPC UA and add tags.
2. Save selected tags.
3. Assign selected tags to loops (`PV`/`SP`/`CO`).
4. Start loop logging.
5. Open selected loop in fullscreen Grafana.

## 8. Typical Workflow

1. `docker compose up -d`
2. `uv run python scripts/influx/setup_hf_schema.py`
3. `uv run python scripts/influx/seed_pid_sim.py`
4. `uv run python scripts/influx/backfill_downsample.py`
5. `uv run python scripts/grafana/push_hf_dashboard.py`
6. `uv run python app/historian_ui.py`
