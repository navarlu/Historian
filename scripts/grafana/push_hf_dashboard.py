import json
import os

import requests


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


def make_raw_target(ref_id: str, query: str, alias: str) -> dict:
    return {
        "refId": ref_id,
        "rawQuery": True,
        "resultFormat": "time_series",
        "query": query,
        "alias": alias,
    }


def method_guard(method_name: str) -> str:
    return f"AND '{method_name}' = '$method'"


def build_payload() -> dict:
    targets = [
        make_raw_target(
            "A",
            (
                'SELECT last("PV") AS "PV" FROM "hf_raw_400d"."pid_loop_hf_raw" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('raw_1s')} GROUP BY time(1s) fill(null)"
            ),
            "PV",
        ),
        make_raw_target(
            "B",
            (
                'SELECT last("SP") AS "SP" FROM "hf_raw_400d"."pid_loop_hf_raw" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('raw_1s')} GROUP BY time(1s) fill(null)"
            ),
            "SP",
        ),
        make_raw_target(
            "C",
            (
                'SELECT last("CO") AS "CO" FROM "hf_raw_400d"."pid_loop_hf_raw" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('raw_1s')} GROUP BY time(1s) fill(null)"
            ),
            "CO",
        ),
        make_raw_target(
            "D",
            (
                'SELECT mean("PV") AS "PV" FROM "hf_raw_400d"."pid_loop_hf_raw" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('raw_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "PV",
        ),
        make_raw_target(
            "E",
            (
                'SELECT last("SP") AS "SP" FROM "hf_raw_400d"."pid_loop_hf_raw" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('raw_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "SP",
        ),
        make_raw_target(
            "F",
            (
                'SELECT mean("CO") AS "CO" FROM "hf_raw_400d"."pid_loop_hf_raw" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('raw_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "CO",
        ),
        make_raw_target(
            "G",
            (
                'SELECT last("PV_mean") AS "PV" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_1m')} GROUP BY time(1m) fill(null)"
            ),
            "PV",
        ),
        make_raw_target(
            "H",
            (
                'SELECT last("SP_last") AS "SP" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_1m')} GROUP BY time(1m) fill(null)"
            ),
            "SP",
        ),
        make_raw_target(
            "I",
            (
                'SELECT last("CO_mean") AS "CO" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_1m')} GROUP BY time(1m) fill(null)"
            ),
            "CO",
        ),
        make_raw_target(
            "I2",
            (
                'SELECT min("PV_mean") AS "PV min" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_1m')} GROUP BY time(1m) fill(null)"
            ),
            "PV min",
        ),
        make_raw_target(
            "I3",
            (
                'SELECT max("PV_mean") AS "PV max" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_1m')} GROUP BY time(1m) fill(null)"
            ),
            "PV max",
        ),
        make_raw_target(
            "I4",
            (
                'SELECT min("SP_last") AS "SP min" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_1m')} GROUP BY time(1m) fill(null)"
            ),
            "SP min",
        ),
        make_raw_target(
            "I5",
            (
                'SELECT max("SP_last") AS "SP max" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_1m')} GROUP BY time(1m) fill(null)"
            ),
            "SP max",
        ),
        make_raw_target(
            "I6",
            (
                'SELECT min("CO_mean") AS "CO min" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_1m')} GROUP BY time(1m) fill(null)"
            ),
            "CO min",
        ),
        make_raw_target(
            "I7",
            (
                'SELECT max("CO_mean") AS "CO max" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_1m')} GROUP BY time(1m) fill(null)"
            ),
            "CO max",
        ),
        make_raw_target(
            "J",
            (
                'SELECT mean("PV_mean") AS "PV" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "PV",
        ),
        make_raw_target(
            "K",
            (
                'SELECT last("SP_last") AS "SP" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "SP",
        ),
        make_raw_target(
            "L",
            (
                'SELECT mean("CO_mean") AS "CO" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "CO",
        ),
        make_raw_target(
            "M",
            (
                'SELECT min("PV") AS "PV min" FROM "hf_raw_400d"."pid_loop_hf_raw" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('raw_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "PV min",
        ),
        make_raw_target(
            "N",
            (
                'SELECT max("PV") AS "PV max" FROM "hf_raw_400d"."pid_loop_hf_raw" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('raw_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "PV max",
        ),
        make_raw_target(
            "O",
            (
                'SELECT min("SP") AS "SP min" FROM "hf_raw_400d"."pid_loop_hf_raw" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('raw_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "SP min",
        ),
        make_raw_target(
            "P",
            (
                'SELECT max("SP") AS "SP max" FROM "hf_raw_400d"."pid_loop_hf_raw" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('raw_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "SP max",
        ),
        make_raw_target(
            "Q",
            (
                'SELECT min("CO") AS "CO min" FROM "hf_raw_400d"."pid_loop_hf_raw" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('raw_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "CO min",
        ),
        make_raw_target(
            "R",
            (
                'SELECT max("CO") AS "CO max" FROM "hf_raw_400d"."pid_loop_hf_raw" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('raw_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "CO max",
        ),
        make_raw_target(
            "S",
            (
                'SELECT min("PV_mean") AS "PV min" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "PV min",
        ),
        make_raw_target(
            "T",
            (
                'SELECT max("PV_mean") AS "PV max" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "PV max",
        ),
        make_raw_target(
            "U",
            (
                'SELECT min("SP_last") AS "SP min" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "SP min",
        ),
        make_raw_target(
            "V",
            (
                'SELECT max("SP_last") AS "SP max" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "SP max",
        ),
        make_raw_target(
            "W",
            (
                'SELECT min("CO_mean") AS "CO min" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "CO min",
        ),
        make_raw_target(
            "X",
            (
                'SELECT max("CO_mean") AS "CO max" FROM "hf_ds_5y"."pid_loop_hf_1m" '
                "WHERE $timeFilter AND \"loop_id\"='$loop_id' AND \"machine_id\"='$machine_id' "
                f"{method_guard('ds_auto')} GROUP BY time($__interval) fill(null)"
            ),
            "CO max",
        ),
    ]

    panel_sp_pv = {
        "id": 1,
        "type": "timeseries",
        "title": "PID SP/PV (Select Method)",
        "datasource": "InfluxDB",
        "maxDataPoints": 2000,
        "gridPos": {"x": 0, "y": 0, "w": 24, "h": 14},
        "targets": [
            t
            for t in targets
            if t["alias"] in {"SP", "PV", "PV min", "PV max"}
        ],
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2,
                    "showPoints": "never",
                }
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "PV"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "blue"}}
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "SP"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "green"}}
                    ],
                },
                {
                    "matcher": {"id": "byRegexp", "options": "^PV (min|max)$"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "#6CA6FF"}},
                        {"id": "custom.lineWidth", "value": 1},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "PV min"},
                    "properties": [
                        {"id": "custom.lineWidth", "value": 0},
                        {"id": "custom.fillOpacity", "value": 0},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "PV max"},
                    "properties": [
                        {"id": "custom.fillBelowTo", "value": "PV min"},
                        {"id": "custom.fillOpacity", "value": 20},
                        {"id": "custom.lineWidth", "value": 0},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "CO"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}
                    ],
                },
            ],
        },
    }

    panel_co = {
        "id": 2,
        "type": "timeseries",
        "title": "PID CO (Select Method)",
        "datasource": "InfluxDB",
        "maxDataPoints": 2000,
        "gridPos": {"x": 0, "y": 16, "w": 24, "h": 12},
        "targets": [t for t in targets if t["alias"] in {"CO", "CO min", "CO max"}],
        "fieldConfig": {
            "defaults": {
                "custom": {
                    "drawStyle": "line",
                    "lineWidth": 2,
                    "showPoints": "never",
                }
            },
            "overrides": [
                {
                    "matcher": {"id": "byName", "options": "CO"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "red"}}
                    ],
                },
                {
                    "matcher": {"id": "byRegexp", "options": "^CO (min|max)$"},
                    "properties": [
                        {"id": "color", "value": {"mode": "fixed", "fixedColor": "#FF8A8A"}},
                        {"id": "custom.lineWidth", "value": 1},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "CO min"},
                    "properties": [
                        {"id": "custom.lineWidth", "value": 0},
                        {"id": "custom.fillOpacity", "value": 0},
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "CO max"},
                    "properties": [
                        {"id": "custom.fillBelowTo", "value": "CO min"},
                        {"id": "custom.fillOpacity", "value": 20},
                        {"id": "custom.lineWidth", "value": 0},
                    ],
                },
            ],
        },
    }

    panel_interval_info = {
        "id": 3,
        "type": "text",
        "title": "Auto Interval Info",
        "gridPos": {"x": 0, "y": 28, "w": 24, "h": 3},
        "options": {
            "mode": "markdown",
            "content": (
                "**Method:** `${method}`  \n"
                "**Auto interval used in `raw_auto`/`ds_auto`:** `${auto_interval}`"
            ),
        },
    }

    return {
        "dashboard": {
            "uid": "pid-hf-1s-benchmark",
            "title": "PID High-Frequency Benchmark",
            "schemaVersion": 39,
            "version": 1,
            "refresh": "10s",
            "time": {"from": "now-60d", "to": "now"},
            "templating": {
                "list": [
                    {
                        "name": "loop_id",
                        "label": "Loop",
                        "type": "query",
                        "datasource": "InfluxDB",
                        "query": 'SHOW TAG VALUES FROM "pid_loop_hf_raw" WITH KEY = "loop_id"',
                        "refresh": 1,
                        "sort": 1,
                    },
                    {
                        "name": "machine_id",
                        "label": "Machine",
                        "type": "query",
                        "datasource": "InfluxDB",
                        "query": 'SHOW TAG VALUES FROM "pid_loop_hf_raw" WITH KEY = "machine_id"',
                        "refresh": 1,
                        "sort": 1,
                    },
                    {
                        "name": "method",
                        "label": "Method",
                        "type": "custom",
                        "query": "raw_1s,raw_auto,ds_1m,ds_auto",
                        "current": {"text": "raw_auto", "value": "raw_auto"},
                    },
                    {
                        "name": "auto_interval",
                        "label": "Auto Interval",
                        "type": "interval",
                        "query": "1s,5s,10s,30s,1m,5m,10m,30m,1h,6h,12h,1d",
                        "auto": True,
                        "auto_count": 1500,
                        "auto_min": "1s",
                        "current": {"text": "auto", "value": "$__auto_interval_auto"},
                    },
                ]
            },
            "panels": [panel_sp_pv, panel_co, panel_interval_info],
        },
        "overwrite": True,
    }


def main() -> None:
    load_env_file()

    grafana_url = os.getenv("GRAFANA_URL", "http://localhost:3000")
    api_key = os.getenv("GRAFANA_API_KEY", "")
    user = os.getenv("GRAFANA_USER", "admin")
    password = os.getenv("GRAFANA_PASSWORD", "admin")

    headers = {"Content-Type": "application/json"}
    auth = None
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        auth = (user, password)

    response = requests.post(
        f"{grafana_url}/api/dashboards/db",
        data=json.dumps(build_payload()),
        headers=headers,
        auth=auth,
        timeout=20,
    )
    print("Create Status:", response.status_code)
    print("Create Response:", response.text)


if __name__ == "__main__":
    main()
