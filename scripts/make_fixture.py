"""Generate the deterministic mini dataset the test suite runs against.

Synthetic rather than sampled from the real download, for three reasons:

* **Determinism.** A fixed seed means an adversarial test can assert an exact
  row count or an exact set of present values.
* **No network in CI.** The real months are ~200MB; nothing in the test suite
  should pull them.
* **Shaped edge cases.** Real data happens to contain outliers and nulls; the
  fixture contains them *on purpose*, at known coordinates, so the tests that
  exist to catch plausible-but-wrong answers have something to catch.

The output reproduces the real schema drift: the 2024-12 file has no
``cbd_congestion_fee`` column at all, and uses ``Airport_fee`` casing, while the
2025 files add the fee column. That is what ``union_by_name`` in bootstrap.sql
exists to absorb, and it is only genuinely tested if the fixture reproduces it.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from ledger.logging import configure_logging, get_logger

log = get_logger(__name__)

SEED = 20250105  # the date congestion pricing began; arbitrary but memorable
ROWS_PER_MONTH = 15_000

# A small, fixed slice of the real TLC zone lookup. Enough boroughs to make
# group-by meaningful and enough zones to make a cardinality guard meaningful.
ZONES: list[tuple[int, str, str, str]] = [
    (1, "EWR", "Newark Airport", "EWR"),
    (4, "Manhattan", "Alphabet City", "Yellow Zone"),
    (13, "Manhattan", "Battery Park City", "Yellow Zone"),
    (24, "Manhattan", "Bloomingdale", "Yellow Zone"),
    (41, "Manhattan", "Central Harlem", "Boro Zone"),
    (43, "Manhattan", "Central Park", "Yellow Zone"),
    (48, "Manhattan", "Clinton East", "Yellow Zone"),
    (50, "Manhattan", "Clinton West", "Yellow Zone"),
    (68, "Manhattan", "East Chelsea", "Yellow Zone"),
    (79, "Manhattan", "East Village", "Yellow Zone"),
    (87, "Manhattan", "Financial District North", "Yellow Zone"),
    (100, "Manhattan", "Garment District", "Yellow Zone"),
    (107, "Manhattan", "Gramercy", "Yellow Zone"),
    (132, "Queens", "JFK Airport", "Airports"),
    (138, "Queens", "LaGuardia Airport", "Airports"),
    (140, "Manhattan", "Lenox Hill East", "Yellow Zone"),
    (161, "Manhattan", "Midtown Center", "Yellow Zone"),
    (162, "Manhattan", "Midtown East", "Yellow Zone"),
    (163, "Manhattan", "Midtown North", "Yellow Zone"),
    (170, "Manhattan", "Murray Hill", "Yellow Zone"),
    (181, "Brooklyn", "Park Slope", "Boro Zone"),
    (186, "Manhattan", "Penn Station/Madison Sq West", "Yellow Zone"),
    (211, "Manhattan", "SoHo", "Yellow Zone"),
    (236, "Manhattan", "Upper East Side North", "Yellow Zone"),
    (237, "Manhattan", "Upper East Side South", "Yellow Zone"),
    (238, "Manhattan", "Upper West Side North", "Yellow Zone"),
    (249, "Manhattan", "West Village", "Yellow Zone"),
    (255, "Brooklyn", "Williamsburg (North Side)", "Boro Zone"),
    (7, "Queens", "Astoria", "Boro Zone"),
    (82, "Queens", "Elmhurst", "Boro Zone"),
]

MONTHS: tuple[tuple[str, bool], ...] = (
    # (month, has_cbd_congestion_fee) -- False reproduces the pre-2025 schema
    ("2024-12", False),
    ("2025-01", True),
    ("2025-02", True),
)


def _month_bounds(month: str) -> tuple[datetime, datetime]:
    year, mon = (int(p) for p in month.split("-"))
    start = datetime(year, mon, 1, tzinfo=UTC)
    end = datetime(year + (mon == 12), (mon % 12) + 1, 1, tzinfo=UTC)
    return start, end


def _build_month(month: str, has_cbd: bool, rng: random.Random) -> dict[str, list[Any]]:
    start, end = _month_bounds(month)
    span = int((end - start).total_seconds())
    location_ids = [z[0] for z in ZONES]

    cols: dict[str, list[Any]] = {
        k: []
        for k in (
            "VendorID",
            "tpep_pickup_datetime",
            "tpep_dropoff_datetime",
            "passenger_count",
            "trip_distance",
            "RatecodeID",
            "store_and_fwd_flag",
            "PULocationID",
            "DOLocationID",
            "payment_type",
            "fare_amount",
            "extra",
            "mta_tax",
            "tip_amount",
            "tolls_amount",
            "improvement_surcharge",
            "total_amount",
            "congestion_surcharge",
            "Airport_fee",
        )
    }
    if has_cbd:
        cols["cbd_congestion_fee"] = []

    for i in range(ROWS_PER_MONTH):
        pickup = start + timedelta(seconds=rng.randrange(span))
        duration_s = max(60, int(rng.lognormvariate(6.4, 0.6)))
        dropoff = pickup + timedelta(seconds=duration_s)

        distance = round(max(0.1, rng.lognormvariate(0.9, 0.8)), 2)
        fare = round(3.0 + distance * 3.2 + duration_s * 0.012, 2)

        # Deliberate outliers, at known positions, so tests can target them:
        # one absurd long-haul fare and one negative (refunded) fare per month.
        if i == 100:
            distance, fare = 210.4, 998.25
        elif i == 200:
            fare = -12.50

        payment = rng.choices([1, 2, 3, 4], weights=[74, 20, 4, 2])[0]
        tip = round(fare * rng.uniform(0.15, 0.30), 2) if payment == 1 and fare > 0 else 0.0

        pu = rng.choice(location_ids)
        do = rng.choice(location_ids)
        airport_fee = 1.75 if pu in (132, 138) else 0.0
        congestion = 2.50 if rng.random() < 0.82 else 0.0
        extra = round(rng.choice([0.0, 0.5, 1.0, 2.5]), 2)
        mta = 0.5
        improvement = 1.0
        tolls = round(rng.choice([0.0, 0.0, 0.0, 6.94]), 2)

        cbd = 0.0
        if has_cbd:
            # Congestion pricing began 5 Jan 2025. Before that date the charge
            # is genuinely zero even in the months that carry the column.
            cbd = 0.75 if pickup >= datetime(2025, 1, 5, tzinfo=UTC) else 0.0
            cols["cbd_congestion_fee"].append(cbd)

        total = round(
            fare + extra + mta + tip + tolls + improvement + congestion + airport_fee + cbd, 2
        )

        cols["VendorID"].append(rng.choice([1, 2, 2, 2]))
        cols["tpep_pickup_datetime"].append(pickup.replace(tzinfo=None))
        cols["tpep_dropoff_datetime"].append(dropoff.replace(tzinfo=None))
        # ~2% null passenger_count, matching the real feed's habit.
        cols["passenger_count"].append(
            None
            if rng.random() < 0.02
            else rng.choices([1, 2, 3, 4, 5, 6], weights=[70, 14, 5, 4, 4, 3])[0]
        )
        cols["trip_distance"].append(distance)
        cols["RatecodeID"].append(2 if pu == 132 else rng.choices([1, 5], weights=[97, 3])[0])
        cols["store_and_fwd_flag"].append("Y" if rng.random() < 0.005 else "N")
        cols["PULocationID"].append(pu)
        cols["DOLocationID"].append(do)
        cols["payment_type"].append(payment)
        cols["fare_amount"].append(fare)
        cols["extra"].append(extra)
        cols["mta_tax"].append(mta)
        cols["tip_amount"].append(tip)
        cols["tolls_amount"].append(tolls)
        cols["improvement_surcharge"].append(improvement)
        cols["total_amount"].append(total)
        cols["congestion_surcharge"].append(congestion)
        cols["Airport_fee"].append(airport_fee)

    return cols


def _schema(has_cbd: bool) -> pa.Schema:
    fields = [
        pa.field("VendorID", pa.int32()),
        pa.field("tpep_pickup_datetime", pa.timestamp("us")),
        pa.field("tpep_dropoff_datetime", pa.timestamp("us")),
        pa.field("passenger_count", pa.int64()),
        pa.field("trip_distance", pa.float64()),
        pa.field("RatecodeID", pa.int64()),
        pa.field("store_and_fwd_flag", pa.string()),
        pa.field("PULocationID", pa.int32()),
        pa.field("DOLocationID", pa.int32()),
        pa.field("payment_type", pa.int64()),
        pa.field("fare_amount", pa.float64()),
        pa.field("extra", pa.float64()),
        pa.field("mta_tax", pa.float64()),
        pa.field("tip_amount", pa.float64()),
        pa.field("tolls_amount", pa.float64()),
        pa.field("improvement_surcharge", pa.float64()),
        pa.field("total_amount", pa.float64()),
        pa.field("congestion_surcharge", pa.float64()),
        pa.field("Airport_fee", pa.float64()),
    ]
    if has_cbd:
        fields.append(pa.field("cbd_congestion_fee", pa.float64()))
    return pa.schema(fields)


def build(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)  # noqa: S311 - fixture data, not cryptography

    for month, has_cbd in MONTHS:
        cols = _build_month(month, has_cbd, rng)
        table = pa.Table.from_pydict(cols, schema=_schema(has_cbd))
        path = out_dir / f"yellow_tripdata_{month}.parquet"
        pq.write_table(table, path, compression="zstd")
        log.info("%-34s %6d rows, cbd_congestion_fee=%s", path.name, table.num_rows, has_cbd)

    zones_path = out_dir / "taxi_zone_lookup.csv"
    lines = ['"LocationID","Borough","Zone","service_zone"']
    lines += [f'{lid},"{borough}","{zone}","{sz}"' for lid, borough, zone, sz in ZONES]
    zones_path.write_text("\n".join(lines) + "\n")
    log.info("%-34s %6d zones", zones_path.name, len(ZONES))


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "data" / "raw",
        help="Output directory (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    build(args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
