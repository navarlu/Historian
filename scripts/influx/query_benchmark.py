import math
import time

from influxdb import InfluxDBClient

INFLUX_HOST = "localhost"
INFLUX_PORT = 8086
INFLUX_DB = "opcuadata"

LOOP_ID = "hf_test_loop_001"
MACHINE_ID = "DeviceSynthetic1"
LOOKBACK_DAYS = 60
MAX_POINTS = 1500
RUNS_PER_QUERY = 3


def seconds_to_influx_interval(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m"
    return f"{max(1, seconds // 3600)}h"


def timed_query(client: InfluxDBClient, query: str) -> tuple[float, int]:
    started = time.perf_counter()
    result = client.query(query)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    points = list(result.get_points())
    return elapsed_ms, len(points)


def benchmark(client: InfluxDBClient, query_name: str, query: str) -> None:
    durations = []
    row_count = 0
    for _ in range(RUNS_PER_QUERY):
        duration_ms, rows = timed_query(client, query)
        durations.append(duration_ms)
        row_count = rows

    avg_ms = sum(durations) / len(durations)
    p95_ms = sorted(durations)[math.ceil(0.95 * len(durations)) - 1]
    print(
        {
            "query": query_name,
            "rows": row_count,
            "runs": RUNS_PER_QUERY,
            "avg_ms": round(avg_ms, 2),
            "p95_ms": round(p95_ms, 2),
            "all_ms": [round(v, 2) for v in durations],
        }
    )


def main() -> None:
    client = InfluxDBClient(host=INFLUX_HOST, port=INFLUX_PORT, database=INFLUX_DB)

    lookback_seconds = LOOKBACK_DAYS * 24 * 60 * 60
    bucket_seconds = max(1, lookback_seconds // MAX_POINTS)
    bucket = seconds_to_influx_interval(bucket_seconds)

    raw_query = (
        'SELECT mean("PV") AS "pv_mean" '
        'FROM "hf_raw_400d"."pid_loop_hf_raw" '
        f"WHERE \"loop_id\" = '{LOOP_ID}' "
        f"AND \"machine_id\" = '{MACHINE_ID}' "
        f"AND time > now() - {LOOKBACK_DAYS}d "
        f"GROUP BY time({bucket}), \"loop_id\", \"machine_id\" fill(null)"
    )

    downsampled_query = (
        'SELECT mean("PV_mean") AS "pv_mean" '
        'FROM "hf_ds_5y"."pid_loop_hf_1m" '
        f"WHERE \"loop_id\" = '{LOOP_ID}' "
        f"AND \"machine_id\" = '{MACHINE_ID}' "
        f"AND time > now() - {LOOKBACK_DAYS}d "
        f"GROUP BY time({bucket}), \"loop_id\", \"machine_id\" fill(null)"
    )

    print(
        {
            "lookback_days": LOOKBACK_DAYS,
            "target_max_points": MAX_POINTS,
            "computed_bucket": bucket,
        }
    )
    benchmark(client, "raw_hf_query", raw_query)
    benchmark(client, "downsampled_query", downsampled_query)


if __name__ == "__main__":
    main()
