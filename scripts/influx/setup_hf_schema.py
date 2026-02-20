from influxdb import InfluxDBClient

INFLUX_HOST = "localhost"
INFLUX_PORT = 8086
INFLUX_DB = "opcuadata"

RAW_RETENTION_POLICY = "hf_raw_400d"
RAW_RETENTION_DURATION = "400d"
RAW_SHARD_DURATION = "1d"

DOWNSAMPLED_RETENTION_POLICY = "hf_ds_5y"
DOWNSAMPLED_RETENTION_DURATION = "1825d"
DOWNSAMPLED_SHARD_DURATION = "7d"

RAW_MEASUREMENT = "pid_loop_hf_raw"
DOWNSAMPLED_MEASUREMENT = "pid_loop_hf_1m"
CONTINUOUS_QUERY_NAME = "cq_pid_loop_hf_1m"


def run_query(client: InfluxDBClient, query: str) -> None:
    client.query(query)


def setup_retention_policies(client: InfluxDBClient) -> None:
    existing = {
        row.get("name")
        for row in client.query(f'SHOW RETENTION POLICIES ON "{INFLUX_DB}"').get_points()
    }

    if RAW_RETENTION_POLICY in existing:
        run_query(
            client,
            (
                f'ALTER RETENTION POLICY "{RAW_RETENTION_POLICY}" ON "{INFLUX_DB}" '
                f'DURATION {RAW_RETENTION_DURATION} REPLICATION 1 '
                f'SHARD DURATION {RAW_SHARD_DURATION}'
            ),
        )
    else:
        run_query(
            client,
            (
                f'CREATE RETENTION POLICY "{RAW_RETENTION_POLICY}" ON "{INFLUX_DB}" '
                f'DURATION {RAW_RETENTION_DURATION} REPLICATION 1 '
                f'SHARD DURATION {RAW_SHARD_DURATION}'
            ),
        )

    if DOWNSAMPLED_RETENTION_POLICY in existing:
        run_query(
            client,
            (
                f'ALTER RETENTION POLICY "{DOWNSAMPLED_RETENTION_POLICY}" ON "{INFLUX_DB}" '
                f'DURATION {DOWNSAMPLED_RETENTION_DURATION} REPLICATION 1 '
                f'SHARD DURATION {DOWNSAMPLED_SHARD_DURATION}'
            ),
        )
    else:
        run_query(
            client,
            (
                f'CREATE RETENTION POLICY "{DOWNSAMPLED_RETENTION_POLICY}" ON "{INFLUX_DB}" '
                f'DURATION {DOWNSAMPLED_RETENTION_DURATION} REPLICATION 1 '
                f'SHARD DURATION {DOWNSAMPLED_SHARD_DURATION}'
            ),
        )


def recreate_continuous_query(client: InfluxDBClient) -> None:
    try:
        run_query(client, f'DROP CONTINUOUS QUERY "{CONTINUOUS_QUERY_NAME}" ON "{INFLUX_DB}"')
    except Exception:
        pass

    cq = (
        f'CREATE CONTINUOUS QUERY "{CONTINUOUS_QUERY_NAME}" ON "{INFLUX_DB}" '
        "RESAMPLE EVERY 1m FOR 5m "
        "BEGIN "
        f'SELECT mean("PV") AS "PV_mean", '
        f'min("PV") AS "PV_min", '
        f'max("PV") AS "PV_max", '
        f'last("SP") AS "SP_last", '
        f'min("SP") AS "SP_min", '
        f'max("SP") AS "SP_max", '
        f'mean("CO") AS "CO_mean", '
        f'min("CO") AS "CO_min", '
        f'max("CO") AS "CO_max" '
        f'INTO "{INFLUX_DB}"."{DOWNSAMPLED_RETENTION_POLICY}"."{DOWNSAMPLED_MEASUREMENT}" '
        f'FROM "{INFLUX_DB}"."{RAW_RETENTION_POLICY}"."{RAW_MEASUREMENT}" '
        'GROUP BY time(1m), "loop_id", "machine_id" '
        "END"
    )
    run_query(client, cq)


def print_schema_status(client: InfluxDBClient) -> None:
    rp_result = client.query(f'SHOW RETENTION POLICIES ON "{INFLUX_DB}"')
    cq_result = client.query("SHOW CONTINUOUS QUERIES")

    print("Retention policies:")
    for row in rp_result.get_points():
        print(row)

    print("\nContinuous queries:")
    cq_map = cq_result.raw.get("series", [])
    for item in cq_map:
        if item.get("name") != INFLUX_DB:
            continue
        values = item.get("values", [])
        for value in values:
            print({"name": value[0], "query": value[1]})


def main() -> None:
    client = InfluxDBClient(host=INFLUX_HOST, port=INFLUX_PORT)
    try:
        client.create_database(INFLUX_DB)
        client.switch_database(INFLUX_DB)
    except Exception as exc:
        raise SystemExit(
            f"Cannot connect to InfluxDB at {INFLUX_HOST}:{INFLUX_PORT}. "
            f"Start Docker first. Details: {exc}"
        )

    setup_retention_policies(client)
    recreate_continuous_query(client)
    print_schema_status(client)


if __name__ == "__main__":
    main()
