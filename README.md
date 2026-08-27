# Ledger

Streaming LLM chat over a governed dataset. The model never sees a row — it calls
typed tools over a profiled catalogue, the service executes them against DuckDB,
and every call is published to a Kafka governance spine before its result is served.

Work in progress. See `docs/` and the milestone list in the repository history.
