import os
import random
import time
from datetime import datetime, timedelta, timezone

from influxdb import InfluxDBClient

INFLUX_HOST = "localhost"
INFLUX_PORT = 8086
INFLUX_DB = "opcuadata"
RETENTION_POLICY = "hf_raw_400d"
MEASUREMENT = "pid_loop_hf_raw"
DOWNSAMPLED_RETENTION_POLICY = "hf_ds_5y"
DOWNSAMPLED_MEASUREMENT = "pid_loop_hf_1m"

LOOP_ID = "hf_test_loop_001"
MACHINE_ID = "DeviceSynthetic1"

YEARS_TO_GENERATE = 1
SAMPLE_INTERVAL_SECONDS = 1
WRITE_BATCH_SIZE = 5000
DELETE_EXISTING_SERIES_FIRST = True
RANDOM_SEED = 42
SECONDS_TO_GENERATE_OVERRIDE = int(os.getenv("HF_SECONDS_OVERRIDE", "0"))
RUN_DOWNSAMPLE_BACKFILL = True
DELETE_EXISTING_DOWNSAMPLED_FIRST = True
BACKFILL_CHUNK_DAYS = 14


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def generate_process_signal_values(
    rng: random.Random,
    total_points: int,
    start_epoch_seconds: int,
):
    sp = 60.0
    pv = 57.0
    co = 58.0
    integral = 0.0
    step_every_seconds = 6 * 60 * 60

    for idx in range(total_points):
        if idx % step_every_seconds == 0 and idx > 0:
            sp = clamp(sp + rng.uniform(-10.0, 10.0), 20.0, 90.0)

        error = sp - pv
        integral = clamp(integral + error * 0.02, -30.0, 30.0)
        co = clamp(50.0 + 1.2 * error + integral + rng.gauss(0.0, 1.2), 0.0, 100.0)
        pv = clamp(pv + 0.06 * (co - pv) + rng.gauss(0.0, 0.18), 0.0, 100.0)

        yield {
            "measurement": MEASUREMENT,
            "tags": {
                "loop_id": LOOP_ID,
                "machine_id": MACHINE_ID,
            },
            "time": start_epoch_seconds + idx,
            "fields": {
                "PV": round(pv, 3),
                "SP": round(sp, 3),
                "CO": round(co, 3),
            },
        }


def to_rfc3339_seconds(ts: datetime) -> str:
    return ts.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def run_downsample_backfill_in_chunks(
    client: InfluxDBClient,
    start_time: datetime,
    end_time: datetime,
) -> None:
    total_seconds = int((end_time - start_time).total_seconds())
    chunk_seconds = BACKFILL_CHUNK_DAYS * 24 * 60 * 60
    done_seconds = 0
    chunk_index = 0

    current_start = start_time
    while current_start < end_time:
        chunk_index += 1
        current_end = min(current_start + timedelta(seconds=chunk_seconds), end_time)
        start_time_rfc3339 = to_rfc3339_seconds(current_start)
        end_time_rfc3339 = to_rfc3339_seconds(current_end)

        backfill_query = (
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
            f'FROM "{INFLUX_DB}"."{RETENTION_POLICY}"."{MEASUREMENT}" '
            f"WHERE \"loop_id\" = '{LOOP_ID}' "
            f"AND \"machine_id\" = '{MACHINE_ID}' "
            f"AND time >= '{start_time_rfc3339}' "
            f"AND time < '{end_time_rfc3339}' "
            'GROUP BY time(1m), "loop_id", "machine_id"'
        )
        chunk_started = time.perf_counter()
        client.query(backfill_query)
        chunk_elapsed = time.perf_counter() - chunk_started

        done_seconds += int((current_end - current_start).total_seconds())
        completion = round((done_seconds / max(total_seconds, 1)) * 100.0, 1)
        print(
            f"Backfill chunk {chunk_index} "
            f"[{start_time_rfc3339} -> {end_time_rfc3339}) done in "
            f"{round(chunk_elapsed, 2)}s ({completion}%)."
        )
        current_start = current_end


def main() -> None:
    client = InfluxDBClient(host=INFLUX_HOST, port=INFLUX_PORT)
    try:
        client.switch_database(INFLUX_DB)
    except Exception as exc:
        raise SystemExit(
            f"Cannot connect to InfluxDB at {INFLUX_HOST}:{INFLUX_PORT}. "
            f"Run Docker and schema setup first. Details: {exc}"
        )

    total_seconds = int(365 * YEARS_TO_GENERATE * 24 * 60 * 60 / SAMPLE_INTERVAL_SECONDS)
    if SECONDS_TO_GENERATE_OVERRIDE > 0:
        total_seconds = SECONDS_TO_GENERATE_OVERRIDE
    if SAMPLE_INTERVAL_SECONDS != 1:
        raise SystemExit("This script is designed for 1-second sampling. Keep SAMPLE_INTERVAL_SECONDS=1.")

    start_time = datetime.now(timezone.utc) - timedelta(seconds=total_seconds)
    start_epoch_seconds = int(start_time.timestamp())

    if DELETE_EXISTING_SERIES_FIRST:
        delete_query = (
            f'DELETE FROM "{MEASUREMENT}" '
            f"WHERE \"loop_id\" = '{LOOP_ID}' AND \"machine_id\" = '{MACHINE_ID}'"
        )
        client.query(delete_query)
        print("Deleted previous synthetic series for selected loop_id/machine_id.")

    rng = random.Random(RANDOM_SEED)
    generator = generate_process_signal_values(
        rng=rng,
        total_points=total_seconds,
        start_epoch_seconds=start_epoch_seconds,
    )

    written = 0
    started = time.perf_counter()
    while written < total_seconds:
        batch = []
        for _ in range(WRITE_BATCH_SIZE):
            if written + len(batch) >= total_seconds:
                break
            batch.append(next(generator))

        client.write_points(
            batch,
            time_precision="s",
            retention_policy=RETENTION_POLICY,
            batch_size=WRITE_BATCH_SIZE,
        )

        written += len(batch)
        if written % 100000 == 0 or written == total_seconds:
            elapsed = time.perf_counter() - started
            rate = int(written / elapsed) if elapsed > 0 else 0
            print(f"Written {written}/{total_seconds} points ({rate} points/s)")

    ended = time.perf_counter()

    end_time = datetime.now(timezone.utc)
    if RUN_DOWNSAMPLE_BACKFILL:
        if DELETE_EXISTING_DOWNSAMPLED_FIRST:
            delete_ds_query = (
                f'DELETE FROM "{DOWNSAMPLED_MEASUREMENT}" '
                f"WHERE \"loop_id\" = '{LOOP_ID}' AND \"machine_id\" = '{MACHINE_ID}'"
            )
            client.query(delete_ds_query)
            print("Deleted previous downsampled synthetic series for selected loop_id/machine_id.")

        backfill_started = time.perf_counter()
        run_downsample_backfill_in_chunks(client=client, start_time=start_time, end_time=end_time)
        print(f"Downsample backfill completed in {round(time.perf_counter() - backfill_started, 2)}s.")

    print(
        "Completed synthetic write:",
        {
            "database": INFLUX_DB,
            "retention_policy": RETENTION_POLICY,
            "measurement": MEASUREMENT,
            "loop_id": LOOP_ID,
            "machine_id": MACHINE_ID,
            "points": total_seconds,
            "from_utc": start_time.isoformat(),
            "to_utc": end_time.isoformat(),
            "elapsed_seconds": round(ended - started, 2),
        },
    )


if __name__ == "__main__":
    main()
