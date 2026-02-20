import json
import time
from pathlib import Path

from asyncua.sync import Client as OpcUaClient
from influxdb import InfluxDBClient

CONFIG_PATH = Path("test/loop_template_config.json")


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def build_node_id(nodeid_template: str, device: str, signal: str) -> str:
    return nodeid_template.format(device=device, signal=signal)


def as_float(value):
    return float(value)


def main() -> None:
    cfg = load_config()

    opcua_url = cfg["opcua_url"]
    influx_host = cfg["influx_host"]
    influx_port = int(cfg["influx_port"])
    influx_db = cfg["influx_db"]
    measurement = cfg["measurement"]
    poll_interval = float(cfg["poll_interval_seconds"])
    iterations = int(cfg["iterations"])
    loop_id = cfg["loop_id"]
    devices = cfg["devices"]
    nodeid_template = cfg["nodeid_template"]
    field_to_signal = cfg["field_to_signal"]

    opc_client = OpcUaClient(url=opcua_url)
    influx_client = InfluxDBClient(host=influx_host, port=influx_port)

    print(f"Connecting OPC UA: {opcua_url}")
    opc_client.connect()
    print("OPC UA connected.")

    print(f"Connecting InfluxDB: {influx_host}:{influx_port} db={influx_db}")
    influx_client.create_database(influx_db)
    influx_client.switch_database(influx_db)
    print("InfluxDB connected.")

    # Resolve all nodes once so each poll only performs value reads.
    nodes = {}
    for device in devices:
        nodes[device] = {}
        for field_name, signal_name in field_to_signal.items():
            node_id = build_node_id(nodeid_template, device, signal_name)
            nodes[device][field_name] = opc_client.get_node(node_id)
            print(f"Mapped {device}.{field_name} -> {node_id}")

    try:
        max_iterations = iterations if iterations > 0 else None
        i = 0
        while max_iterations is None or i < max_iterations:
            i += 1
            points = []

            for device in devices:
                try:
                    fields = {}
                    for field_name, node in nodes[device].items():
                        fields[field_name] = as_float(node.read_value())
                except Exception as exc:
                    if max_iterations is None:
                        print(f"[{i}/live] Read error for {device}: {exc}")
                    else:
                        print(f"[{i}/{iterations}] Read error for {device}: {exc}")
                    continue

                point = {
                    "measurement": measurement,
                    "tags": {
                        "machine_id": device,
                        "loop_id": loop_id,
                    },
                    "fields": fields,
                }
                points.append(point)
                if max_iterations is None:
                    print(f"[{i}/live] {device} -> {fields}")
                else:
                    print(f"[{i}/{iterations}] {device} -> {fields}")

            if points:
                influx_client.write_points(points)

            time.sleep(poll_interval)
    finally:
        opc_client.disconnect()
        print("OPC UA disconnected.")

    result = influx_client.query(
        f'SELECT LAST("PV") AS pv, LAST("SP") AS sp, LAST("CO") AS co '
        f'FROM "{measurement}" GROUP BY "machine_id"'
    )
    print("Last values by machine:")
    for item in result.get_points():
        print(item)


if __name__ == "__main__":
    main()

