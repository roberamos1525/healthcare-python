"""Summarizes a k6 summary export and the resource samples of a measured run (used for docs/testing.md)."""

import csv
import json
import sys
from pathlib import Path


def main() -> None:
    """Print RPS, error rate, per-endpoint latencies, resource peaks and averages, Redis and outbox figures."""
    results = Path(__file__).parent / "results"
    name = sys.argv[1]
    summary = json.loads((results / f"{name}-summary.json").read_text(encoding="utf-8"))
    metrics = summary["metrics"]
    requests = metrics["http_reqs"]
    print(f"requests {requests['count']}  rps {requests['rate']:.1f}")
    failed = metrics.get("http_req_failed", {})
    print(f"http_req_failed {failed.get('value', failed.get('rate', 0)) * 100:.2f}%")
    for key in ("checks", "unexpected_errors", "bookings_created", "booking_conflicts"):
        if key in metrics:
            print(key, {k: v for k, v in metrics[key].items() if k in ("value", "count", "passes", "fails", "rate")})
    for key, value in sorted(metrics.items()):
        if key.startswith("http_req_duration"):
            print(f"{key:55s} avg={value['avg']:.1f} p50={value['med']:.1f} p90={value['p(90)']:.1f} "
                  f"p95={value['p(95)']:.1f} max={value['max']:.1f}" + (f" p99={value['p(99)']:.1f}" if "p(99)" in value else ""))
    samples = results / f"{name}-resources.csv"
    if samples.exists():
        rows = [row for row in csv.reader(samples.read_text(encoding="utf-8-sig").splitlines()) if row and not row[0].startswith("#")]
        header, data = rows[0], rows[1:]
        columns = {name: [float(row[index]) for row in data] for index, name in enumerate(header) if name != "time"}
        for column, values in columns.items():
            print(f"{column:25s} avg={sum(values) / len(values):.1f} max={max(values):.1f}")
        for line in samples.read_text(encoding="utf-8-sig").splitlines():
            if line.startswith("#"):
                print(line)
    outbox = results / f"{name}-outbox.txt"
    if outbox.exists():
        print(outbox.read_text(encoding="utf-16" if outbox.read_bytes()[:2] == b"\xff\xfe" else "utf-8"))


if __name__ == "__main__":
    main()
