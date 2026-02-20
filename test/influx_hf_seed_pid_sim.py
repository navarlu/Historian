import math
import random
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from influxdb import InfluxDBClient

INFLUX_HOST = "localhost"
INFLUX_PORT = 8086
INFLUX_DB = "opcuadata"
RETENTION_POLICY = "hf_raw_400d"
MEASUREMENT = "pid_loop_hf_raw"

LOOP_ID = "hf_test_loop_002"
MACHINE_ID = "DeviceSyntheticPID"

YEARS_TO_GENERATE = 1
SAMPLE_INTERVAL_SECONDS = 1
WRITE_BATCH_SIZE = 5000
DELETE_EXISTING_SERIES_FIRST = True
RANDOM_SEED = 7

# Process model (first-order plus dead time): PV_dot = (Kp*u_delayed - PV) / tau
PROCESS_GAIN = 1.1
PROCESS_TAU_SECONDS = 180.0
PROCESS_DEAD_TIME_SECONDS = 20
MEASUREMENT_NOISE_STD = 0.15
DISTURBANCE_AMPLITUDE = 2.0
DISTURBANCE_PERIOD_SECONDS = 8 * 60 * 60

# PID settings (simple PID with anti-windup-by-clamp)
PID_KC = 2.2
PID_TI_SECONDS = 220.0
PID_TD_SECONDS = 12.0
CO_BIAS = 50.0
CO_MIN = 0.0
CO_MAX = 100.0

# SP behavior: random target between 80 and 120, change about every hour
SP_MIN = 80.0
SP_MAX = 120.0
SP_STEP_MIN_SECONDS = 45 * 60
SP_STEP_MAX_SECONDS = 75 * 60


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def format_eta(seconds: float) -> str:
    if seconds < 0:
        return "unknown"
    seconds_int = int(seconds)
    hh = seconds_int // 3600
    mm = (seconds_int % 3600) // 60
    ss = seconds_int % 60
    return f"{hh:02d}:{mm:02d}:{ss:02d}"


def generate_pid_series(total_points: int, start_epoch_seconds: int, rng: random.Random):
    dt = float(SAMPLE_INTERVAL_SECONDS)
    dead_steps = max(1, int(PROCESS_DEAD_TIME_SECONDS / SAMPLE_INTERVAL_SECONDS))
    co_buffer = deque([CO_BIAS] * dead_steps, maxlen=dead_steps)

    sp = rng.uniform(SP_MIN, SP_MAX)
    next_sp_change_at = rng.randint(SP_STEP_MIN_SECONDS, SP_STEP_MAX_SECONDS)
    pv = sp - 10.0
    co = CO_BIAS

    integral = 0.0
    prev_error = 0.0

    for i in range(total_points):
        if i >= next_sp_change_at:
            sp = rng.uniform(SP_MIN, SP_MAX)
            next_sp_change_at += rng.randint(SP_STEP_MIN_SECONDS, SP_STEP_MAX_SECONDS)

        error = sp - pv
        integral += (error * dt) / max(PID_TI_SECONDS, 1e-9)
        derivative = (error - prev_error) / dt
        co_unclamped = CO_BIAS + PID_KC * (error + integral + PID_TD_SECONDS * derivative)
        co = clamp(co_unclamped, CO_MIN, CO_MAX)

        co_buffer.append(co)
        delayed_co = co_buffer[0]

        disturbance = DISTURBANCE_AMPLITUDE * math.sin(
            2.0 * math.pi * (i / DISTURBANCE_PERIOD_SECONDS)
        )
        pv_dot = ((PROCESS_GAIN * delayed_co + disturbance) - pv) / PROCESS_TAU_SECONDS
        pv = pv + pv_dot * dt + rng.gauss(0.0, MEASUREMENT_NOISE_STD)

        prev_error = error

        yield {
            "measurement": MEASUREMENT,
            "tags": {"loop_id": LOOP_ID, "machine_id": MACHINE_ID},
            "time": start_epoch_seconds + i,
            "fields": {
                "PV": round(pv, 3),
                "SP": round(sp, 3),
                "CO": round(co, 3),
            },
        }


def main() -> None:
    if SAMPLE_INTERVAL_SECONDS != 1:
        raise SystemExit("Keep SAMPLE_INTERVAL_SECONDS = 1 for this benchmark script.")

    total_points = int(365 * YEARS_TO_GENERATE * 24 * 60 * 60 / SAMPLE_INTERVAL_SECONDS)
    start_time = datetime.now(timezone.utc) - timedelta(seconds=total_points)
    start_epoch_seconds = int(start_time.timestamp())

    client = InfluxDBClient(host=INFLUX_HOST, port=INFLUX_PORT, database=INFLUX_DB)
    try:
        client.query("SHOW MEASUREMENTS LIMIT 1")
    except Exception as exc:
        raise SystemExit(
            f"Cannot connect to InfluxDB at {INFLUX_HOST}:{INFLUX_PORT}. Details: {exc}"
        )

    if DELETE_EXISTING_SERIES_FIRST:
        delete_query = (
            f'DELETE FROM "{MEASUREMENT}" '
            f"WHERE \"loop_id\" = '{LOOP_ID}' AND \"machine_id\" = '{MACHINE_ID}'"
        )
        client.query(delete_query)
        print("Deleted existing synthetic PID series for target loop/machine.")

    rng = random.Random(RANDOM_SEED)
    series = generate_pid_series(
        total_points=total_points,
        start_epoch_seconds=start_epoch_seconds,
        rng=rng,
    )

    written = 0
    started = time.perf_counter()
    next_report_at = 100000

    while written < total_points:
        batch = []
        for _ in range(WRITE_BATCH_SIZE):
            if written + len(batch) >= total_points:
                break
            batch.append(next(series))

        write_started = time.perf_counter()
        client.write_points(
            batch,
            time_precision="s",
            retention_policy=RETENTION_POLICY,
            batch_size=WRITE_BATCH_SIZE,
        )
        write_elapsed = time.perf_counter() - write_started
        written += len(batch)

        if written >= next_report_at or written == total_points:
            elapsed = time.perf_counter() - started
            avg_rate = written / elapsed if elapsed > 0 else 0.0
            remaining = max(0, total_points - written)
            eta_seconds = remaining / avg_rate if avg_rate > 0 else -1.0
            percent = (written / total_points) * 100.0
            print(
                (
                    f"Progress {written}/{total_points} ({percent:.2f}%) | "
                    f"avg {int(avg_rate)} pts/s | last batch {write_elapsed:.2f}s | "
                    f"ETA {format_eta(eta_seconds)}"
                )
            )
            next_report_at += 100000

    ended = time.perf_counter()
    print(
        "Completed synthetic PID write:",
        {
            "database": INFLUX_DB,
            "retention_policy": RETENTION_POLICY,
            "measurement": MEASUREMENT,
            "loop_id": LOOP_ID,
            "machine_id": MACHINE_ID,
            "points": total_points,
            "from_utc": start_time.isoformat(),
            "to_utc": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(ended - started, 2),
            "avg_points_per_second": int(total_points / max(ended - started, 1e-9)),
        },
    )


if __name__ == "__main__":
    main()
