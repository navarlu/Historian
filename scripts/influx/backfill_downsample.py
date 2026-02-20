import time
from datetime import datetime, timedelta, timezone
import os

from influxdb import InfluxDBClient

INFLUX_HOST = "localhost"
INFLUX_PORT = 8086
INFLUX_DB = "opcuadata"

RAW_RETENTION_POLICY = "hf_raw_400d"
RAW_MEASUREMENT = "pid_loop_hf_raw"
DOWNSAMPLED_RETENTION_POLICY = "hf_ds_5y"
DOWNSAMPLED_MEASUREMENT = "pid_loop_hf_1m"

LOOP_ID = os.getenv("HF_LOOP_ID", "hf_test_loop_001")
MACHINE_ID = os.getenv("HF_MACHINE_ID", "DeviceSynthetic1")

DELETE_EXISTING_DOWNSAMPLED_FIRST = True
BACKFILL_CHUNK_DAYS = 14


def to_rfc3339_seconds(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_rfc3339(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)


def get_raw_bounds(client: InfluxDBClient) -> tuple[datetime, datetime]:
    first_query = (
        'SELECT FIRST("PV") AS v '
        f'FROM "{RAW_RETENTION_POLICY}"."{RAW_MEASUREMENT}" '
        f"WHERE \"loop_id\" = '{LOOP_ID}' AND \"machine_id\" = '{MACHINE_ID}'"
    )
    last_query = (
        'SELECT LAST("PV") AS v '
        f'FROM "{RAW_RETENTION_POLICY}"."{RAW_MEASUREMENT}" '
        f"WHERE \"loop_id\" = '{LOOP_ID}' AND \"machine_id\" = '{MACHINE_ID}'"
    )
    first_points = list(client.query(first_query).get_points())
    last_points = list(client.query(last_query).get_points())
    if not first_points or not last_points:
        raise SystemExit("No raw synthetic data found for selected loop_id/machine_id.")

    first_time = parse_rfc3339(first_points[0]["time"])
    last_time = parse_rfc3339(last_points[0]["time"])
    return first_time, last_time


def backfill_chunk(
    client: InfluxDBClient,
    chunk_start: datetime,
    chunk_end: datetime,
) -> None:
    query = (
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
        f"WHERE \"loop_id\" = '{LOOP_ID}' "
        f"AND \"machine_id\" = '{MACHINE_ID}' "
        f"AND time >= '{to_rfc3339_seconds(chunk_start)}' "
        f"AND time < '{to_rfc3339_seconds(chunk_end)}' "
        'GROUP BY time(1m), "loop_id", "machine_id"'
    )
    client.query(query)


def main() -> None:
    client = InfluxDBClient(host=INFLUX_HOST, port=INFLUX_PORT, database=INFLUX_DB)

    first_time, last_time = get_raw_bounds(client)
    end_exclusive = last_time + timedelta(seconds=1)
    print({"raw_first": to_rfc3339_seconds(first_time), "raw_last": to_rfc3339_seconds(last_time)})

    if DELETE_EXISTING_DOWNSAMPLED_FIRST:
        delete_query = (
            f'DELETE FROM "{DOWNSAMPLED_MEASUREMENT}" '
            f"WHERE \"loop_id\" = '{LOOP_ID}' AND \"machine_id\" = '{MACHINE_ID}'"
        )
        client.query(delete_query)
        print("Deleted existing downsampled points for selected loop_id/machine_id.")

    chunk_seconds = BACKFILL_CHUNK_DAYS * 24 * 60 * 60
    total_seconds = int((end_exclusive - first_time).total_seconds())
    completed = 0
    chunk_index = 0

    current = first_time
    overall_started = time.perf_counter()
    while current < end_exclusive:
        chunk_index += 1
        chunk_end = min(current + timedelta(seconds=chunk_seconds), end_exclusive)
        started = time.perf_counter()
        backfill_chunk(client, current, chunk_end)
        elapsed = time.perf_counter() - started
        completed += int((chunk_end - current).total_seconds())
        percent = round((completed / max(total_seconds, 1)) * 100.0, 1)
        print(
            f"Chunk {chunk_index}: {to_rfc3339_seconds(current)} -> "
            f"{to_rfc3339_seconds(chunk_end)} in {round(elapsed, 2)}s ({percent}%)"
        )
        current = chunk_end

    print(f"Backfill completed in {round(time.perf_counter() - overall_started, 2)}s.")


if __name__ == "__main__":
    main()
