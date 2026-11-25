# Benchmarks

korbase sits between your code and the database driver, so it adds some
time to each query. `benches/perf_check.py` measures how much.

## What is measured

Five cases, each on two backends (SQLite and Postgres), each run twice:
once with the raw driver (`sqlite3`, `asyncpg`) and once through korbase.

- `select_pk`: read one row by primary key.
- `select_100`: read 100 rows.
- `insert_one`: insert one row.
- `bulk_insert_1000`: insert 1000 rows in one call.
- `aggregate`: a `count` and `sum` over a table.

Each case warms up first, then runs 15 rounds that alternate between the
raw driver and korbase, so both see the same machine. The result is the
median of the rounds, in microseconds per operation.

## Run it yourself

Start Postgres (skip it to measure SQLite only), then run the script:

```sh
bash tests/pg/start.sh                 # prints the export line to run
export KORBASE_TEST_DSN=postgresql://korbase:korbase@localhost:55433/korbase_bench
uv run python benches/perf_check.py measure
```

The Postgres cases run when `KORBASE_TEST_DSN` is set.

## Results

Python 3.14, Postgres 18 in Docker on the same machine, SQLite in a file:

| case | raw µs | korbase µs | overhead µs | ratio |
|---|---|---|---|---|
| sqlite/select_pk | 2.8 | 61.1 | 58.2 | 21.83 |
| sqlite/select_100 | 56.7 | 135.8 | 79.2 | 2.35 |
| sqlite/insert_one | 39.5 | 190.1 | 150.6 | 4.99 |
| sqlite/bulk_insert_1000 | 4242.4 | 13150.3 | 8907.9 | 3.10 |
| sqlite/aggregate | 125.6 | 242.4 | 116.8 | 1.92 |
| pg/select_pk | 135.6 | 159.3 | 23.7 | 1.12 |
| pg/select_100 | 260.6 | 453.9 | 193.3 | 1.70 |
| pg/insert_one | 246.3 | 284.8 | 38.4 | 1.14 |
| pg/bulk_insert_1000 | 5698.7 | 9440.0 | 3741.2 | 1.66 |
| pg/aggregate | 8289.0 | 7999.3 | -289.7 | 1.01 |

Numbers vary by machine. Read them as an order of magnitude, not a promise.

The SQLite ratios look large, and they should be read with their cause in
mind. The raw `sqlite3` module is called synchronously: a point read takes
under 3 µs, but it blocks the event loop while it runs. korbase runs
SQLite on a worker thread per connection, so the loop never blocks. Most of
the overhead is the hop to that thread and back, a cost of roughly 50 µs
that dominates tiny queries such as `select_pk`. On larger queries it
is a small part of the total.

On Postgres, the network round trip already costs more than the work korbase
adds, so the overhead is small: from none to about 1.7 times the raw driver.

## In CI

Each pull request is measured against its base branch by the `Performance`
workflow. It builds two virtual environments, one with the base korbase and
one with the pull request's, runs the same cases against each, and prints
the comparison in the job summary.

It compares the ratio korbase/raw, not absolute times. Absolute times change
from one CI machine to the next; the ratio, measured in the same job with
alternating rounds, mostly cancels that out.

For now the check only reports: it never fails a pull request, because the
same code measured twice can differ by up to about 15% on `pg/select_100`.
It will become a gate once that noise is lower.
