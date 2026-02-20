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


def build_payload() -> dict:
    return {
        "dashboard": {
            "uid": "pid-loops-template",
            "title": "PID Loops Template",
            "schemaVersion": 39,
            "version": 1,
            "refresh": "5s",
            "time": {"from": "now-30m", "to": "now"},
            "templating": {
                "list": [
                    {
                        "name": "loop_id",
                        "label": "Loop",
                        "type": "query",
                        "datasource": "InfluxDB",
                        "query": 'SHOW TAG VALUES FROM "pid_loop_data" WITH KEY = "loop_id"',
                        "refresh": 1,
                        "sort": 1,
                    },
                ]
            },
            "panels": [
                {
                    "id": 1,
                    "type": "timeseries",
                    "title": "PV vs SP",
                    "datasource": "InfluxDB",
                    "gridPos": {"x": 0, "y": 0, "w": 24, "h": 9},
                    "targets": [
                        {
                            "refId": "A",
                            "measurement": "pid_loop_data",
                            "resultFormat": "time_series",
                            "groupBy": [
                                {"type": "time", "params": ["$__interval"]},
                                {"type": "fill", "params": ["null"]},
                            ],
                            "tags": [
                                {"key": "loop_id", "operator": "=", "value": "$loop_id"},
                            ],
                            "select": [
                                [
                                    {"type": "field", "params": ["PV"]},
                                    {"type": "mean", "params": []},
                                ]
                            ],
                        },
                        {
                            "refId": "B",
                            "measurement": "pid_loop_data",
                            "resultFormat": "time_series",
                            "groupBy": [
                                {"type": "time", "params": ["$__interval"]},
                                {"type": "fill", "params": ["null"]},
                            ],
                            "tags": [
                                {"key": "loop_id", "operator": "=", "value": "$loop_id"},
                            ],
                            "select": [
                                [
                                    {"type": "field", "params": ["SP"]},
                                    {"type": "mean", "params": []},
                                ]
                            ],
                        },
                    ],
                },
                {
                    "id": 2,
                    "type": "timeseries",
                    "title": "CO",
                    "datasource": "InfluxDB",
                    "gridPos": {"x": 0, "y": 9, "w": 24, "h": 7},
                    "targets": [
                        {
                            "refId": "A",
                            "measurement": "pid_loop_data",
                            "resultFormat": "time_series",
                            "groupBy": [
                                {"type": "time", "params": ["$__interval"]},
                                {"type": "fill", "params": ["null"]},
                            ],
                            "tags": [
                                {"key": "loop_id", "operator": "=", "value": "$loop_id"},
                            ],
                            "select": [
                                [
                                    {"type": "field", "params": ["CO"]},
                                    {"type": "mean", "params": []},
                                ]
                            ],
                        }
                    ],
                },
            ],
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

    payload = build_payload()
    response = requests.post(
        f"{grafana_url}/api/dashboards/db",
        data=json.dumps(payload),
        headers=headers,
        auth=auth,
        timeout=20,
    )
    print("Create Status:", response.status_code)
    print("Create Response:", response.text)


if __name__ == "__main__":
    main()

