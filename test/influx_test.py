import time

from influxdb import InfluxDBClient

INFLUX_HOST = "localhost"
INFLUX_PORT = 8086
INFLUX_DB = "opcuadata"


def main() -> None:
    client = InfluxDBClient(host=INFLUX_HOST, port=INFLUX_PORT)
    try:
        client.create_database(INFLUX_DB)
        client.switch_database(INFLUX_DB)
    except Exception as exc:
        raise SystemExit(
            f"Cannot connect to InfluxDB at {INFLUX_HOST}:{INFLUX_PORT}. "
            f"Start Docker/InfluxDB first. Details: {exc}"
        )

    point = [
        {
            "measurement": "test_measurement",
            "tags": {"test_tag": "demo"},
            "fields": {"value": 123.45},
        }
    ]
    client.write_points(point)
    print("Point written.")

    time.sleep(1)
    result = client.query("SELECT * FROM test_measurement ORDER BY time DESC LIMIT 5")
    print("Query Result:")
    for item in result.get_points():
        print(item)


if __name__ == "__main__":
    main()
