# Historian (InfluxDB + Grafana)

Simple local historian stack for high-frequency PID data:
- InfluxDB 1.8 for storage
- Grafana for visualization
- Python scripts in `test/` for data generation and dashboard updates

## Showcase

![Historian Dashboard Showcase](docs/assets/showcase.png)

## 1. Start Docker Services

From project root:

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

## 2. Open Grafana

- URL: `http://localhost:3000`
- Default user: `admin`
- Password: from `.env` (`GRAFANA_PASSWORD`)

## 3. Prepare / Update Dashboard

Push dashboard definition to Grafana:

```powershell
uv run python test/grafana_hf_trend_dashboard.py
```

If `uv` is not available in your shell, use:

```powershell
.venv\Scripts\python.exe test/grafana_hf_trend_dashboard.py
```

## 4. Generate Synthetic Historical Data

### High-frequency PID simulation (1 year, 1 second)

```powershell
uv run python test/influx_hf_seed_pid_sim.py
```

This writes to:
- measurement: `pid_loop_hf_raw`
- loop: `hf_test_loop_002`
- machine: `DeviceSyntheticPID`

### Build downsampled series (for `ds_1m` / `ds_auto`)

```powershell
$env:HF_LOOP_ID='hf_test_loop_002'
$env:HF_MACHINE_ID='DeviceSyntheticPID'
uv run python test/influx_hf_backfill_downsample.py
```

## 5. View Historical Data in Grafana

Open dashboard:

`http://localhost:3000/d/pid-hf-1s-benchmark/pid-high-frequency-benchmark`

Set variables:
- `Loop`: `hf_test_loop_002`
- `Machine`: `DeviceSyntheticPID`
- `Method`: choose one of:
  - `raw_1s` (true 1s raw)
  - `raw_auto` (raw aggregated by auto interval)
  - `ds_1m` (fixed 1-minute downsample)
  - `ds_auto` (downsample + auto interval)

Panels:
- Top: `SP/PV`
- Bottom: `CO`
- Info panel shows selected method + auto interval

## 6. Typical Workflow

1. `docker compose up -d`
2. Generate or update data (`test/influx_hf_seed_pid_sim.py`)
3. Backfill downsample (`test/influx_hf_backfill_downsample.py`)
4. Push dashboard (`test/grafana_hf_trend_dashboard.py`)
5. Open Grafana and inspect ranges (`7d`, `30d`, `90d`, `1y`)
