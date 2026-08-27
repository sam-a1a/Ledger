-- Normalised views over the raw TLC parquet.
--
-- Everything downstream -- catalogue, SQL compiler, model -- speaks the
-- lower_snake names defined here, and the compiler only ever emits identifiers
-- the catalogue itself produced. This file is the single place where the raw
-- dataset's inconsistencies are absorbed.
--
-- DuckDB rejects prepared parameters in CREATE VIEW ('this type of statement
-- can't be prepared'), so duck.py binds the paths with `SET VARIABLE x = ?`
-- and the views read them through getvariable(). The paths therefore never
-- reach SQL as interpolated text, and the glob re-resolves per query.

CREATE SCHEMA IF NOT EXISTS ledger;

-- union_by_name is load-bearing, not defensive polish:
--   * cbd_congestion_fee does not exist before 2025-01 (congestion pricing
--     began 5 Jan 2025); positional union would silently misalign columns.
--   * Airport_fee has drifted in casing across years.
-- Verified against the real 2024-12 and 2025-01 footers.
CREATE OR REPLACE VIEW ledger.trips_raw AS
SELECT * FROM read_parquet(getvariable('trips_glob'), union_by_name => true, hive_partitioning => false);

CREATE OR REPLACE VIEW ledger.zones AS
SELECT
    "LocationID"::INTEGER AS location_id,
    "Borough"             AS borough,
    "Zone"                AS zone,
    "service_zone"        AS service_zone
FROM read_csv(getvariable('zones_path'), header => true);

CREATE OR REPLACE VIEW ledger.trips AS
SELECT
    t."VendorID"::INTEGER                                       AS vendor_id,
    CASE t."VendorID"
        WHEN 1 THEN 'Creative Mobile'
        WHEN 2 THEN 'Curb/VeriFone'
        WHEN 6 THEN 'Myle'
        WHEN 7 THEN 'Helix'
        ELSE 'Unknown'
    END                                                         AS vendor_label,

    -- Synthetic tenant key. Taxi data has no natural tenant, but row-level
    -- multi-tenant isolation is a requirement, and deriving it from the vendor
    -- gives two non-trivial, well-populated partitions to demonstrate it.
    ((t."VendorID" % 2) + 1)::INTEGER                            AS tenant_id,

    t.tpep_pickup_datetime                                      AS pickup_at,
    t.tpep_dropoff_datetime                                     AS dropoff_at,
    CAST(t.tpep_pickup_datetime AS DATE)                        AS pickup_date,
    hour(t.tpep_pickup_datetime)::INTEGER                        AS pickup_hour,
    date_diff('second', t.tpep_pickup_datetime, t.tpep_dropoff_datetime) / 60.0
                                                                AS trip_duration_min,

    t.passenger_count::INTEGER                                  AS passenger_count,
    t.trip_distance::DOUBLE                                     AS trip_distance,

    t."RatecodeID"::INTEGER                                     AS ratecode_id,
    CASE t."RatecodeID"
        WHEN 1 THEN 'Standard rate'
        WHEN 2 THEN 'JFK'
        WHEN 3 THEN 'Newark'
        WHEN 4 THEN 'Nassau or Westchester'
        WHEN 5 THEN 'Negotiated fare'
        WHEN 6 THEN 'Group ride'
        ELSE 'Unknown'
    END                                                         AS ratecode_label,

    t.store_and_fwd_flag                                        AS store_and_fwd_flag,

    t."PULocationID"::INTEGER                                   AS pickup_location_id,
    pu.borough                                                  AS pickup_borough,
    pu.zone                                                     AS pickup_zone,
    t."DOLocationID"::INTEGER                                   AS dropoff_location_id,
    dz.borough                                                  AS dropoff_borough,
    dz.zone                                                     AS dropoff_zone,

    t.payment_type::INTEGER                                     AS payment_type,
    CASE t.payment_type
        WHEN 0 THEN 'Flex Fare'
        WHEN 1 THEN 'Credit card'
        WHEN 2 THEN 'Cash'
        WHEN 3 THEN 'No charge'
        WHEN 4 THEN 'Dispute'
        WHEN 5 THEN 'Unknown'
        WHEN 6 THEN 'Voided trip'
        ELSE 'Unknown'
    END                                                         AS payment_type_label,

    t.fare_amount::DOUBLE                                       AS fare_amount,
    t.extra::DOUBLE                                             AS extra,
    t.mta_tax::DOUBLE                                           AS mta_tax,
    t.tip_amount::DOUBLE                                        AS tip_amount,
    t.tolls_amount::DOUBLE                                      AS tolls_amount,
    t.improvement_surcharge::DOUBLE                             AS improvement_surcharge,
    t.total_amount::DOUBLE                                      AS total_amount,
    t.congestion_surcharge::DOUBLE                              AS congestion_surcharge,

    -- Casing drifted across years; TRY_CAST because union_by_name leaves the
    -- absent variant as NULL of an unrelated type.
    COALESCE(
        TRY_CAST(t."Airport_fee" AS DOUBLE),
        TRY_CAST(t."airport_fee" AS DOUBLE)
    )                                                           AS airport_fee,

    -- NULL for every trip before 2025-01: the column is simply not in those
    -- files. That NULL is meaningful -- it is the fare change.
    TRY_CAST(t.cbd_congestion_fee AS DOUBLE)                    AS cbd_congestion_fee

FROM ledger.trips_raw t
LEFT JOIN ledger.zones pu ON pu.location_id = t."PULocationID"
LEFT JOIN ledger.zones dz ON dz.location_id = t."DOLocationID";
