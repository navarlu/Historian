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


load_env_file()

GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:3000")
API_KEY = os.getenv("GRAFANA_API_KEY", "")
ADMIN_USER = os.getenv("GRAFANA_USER", "admin")
ADMIN_PASSWORD = os.getenv("GRAFANA_PASSWORD", "admin")


def build_payload() -> dict:
    return {
        "dashboard": {
            "uid": "test-dashboard",
            "title": "Test Dashboard",
            "panels": [
                {
                    "type": "timeseries",
                    "title": "Test Panel",
                    "datasource": "InfluxDB",
                    "targets": [
                        {
                            "measurement": "test_measurement",
                            "groupBy": [
                                {"type": "time", "params": ["$__interval"]},
                                {"type": "fill", "params": ["null"]},
                            ],
                            "select": [
                                [
                                    {"type": "field", "params": ["value"]},
                                    {"type": "mean", "params": []},
                                ]
                            ],
                            "refId": "A",
                        }
                    ],
                    "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
                }
            ],
            "time": {"from": "now-1h", "to": "now"},
            "schemaVersion": 39,
            "version": 1,
        },
        "overwrite": True,
    }


def main() -> None:
    headers = {"Content-Type": "application/json"}
    auth = None

    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    else:
        auth = (ADMIN_USER, ADMIN_PASSWORD)

    payload = build_payload()
    response = requests.post(
        f"{GRAFANA_URL}/api/dashboards/db",
        data=json.dumps(payload),
        headers=headers,
        auth=auth,
        timeout=20,
    )
    print("Create Status:", response.status_code)
    print("Create Response:", response.text)

    if response.status_code == 200:
        created = response.json()
        uid = created.get("uid", "test-dashboard")
        verify = requests.get(
            f"{GRAFANA_URL}/api/dashboards/uid/{uid}",
            headers=headers,
            auth=auth,
            timeout=20,
        )
        print("Verify Status:", verify.status_code)
        print("Verify Response:", verify.text)


if __name__ == "__main__":
    main()
