"""Performance check: how much does korbase add over the raw driver?

- `measure [--json PATH]` times each case (raw driver vs korbase) and
  prints a Markdown table (µs per operation).
- `compare --base PYTHON --head PYTHON [--limit 0.10] [--report-only]`
  runs `measure --json` with two interpreters, each with its own korbase
  installed, and fails (exit 1) when the head ratio korbase/raw is more
  than `--limit` above the base on any case.

Postgres cases run when KORBASE_TEST_DSN is set.
"""

import argparse
import asyncio
import importlib.util
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WARMUP = 50
ROUNDS = 15
CALLS = 200


def _load(name: str):
    """Import a case module that sits next to this script."""
    path = Path(__file__).with_name(f"{name}.py")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


async def _time(fn, calls: int) -> float:
    """Return the µs per call of `fn` over `calls` calls."""
    start = time.perf_counter_ns()
    for _ in range(calls):
        await fn()
    return (time.perf_counter_ns() - start) / calls / 1000


async def _measure_case(raw, kor) -> dict:
    """Time one case: warm up, then alternate raw and korbase rounds."""
    await _time(raw, WARMUP)
    await _time(kor, WARMUP)
    raws, kors = [], []
    for _ in range(ROUNDS):
        raws.append(await _time(raw, CALLS))
        kors.append(await _time(kor, CALLS))
    ratios = [k / r for k, r in zip(kors, raws, strict=True)]
    return {
        "raw": statistics.median(raws),
        "korbase": statistics.median(kors),
        "ratio": statistics.median(ratios),
    }


async def _measure_backend(name: str, dsn: str) -> dict:
    """Measure every case of one backend, keyed `backend/case`."""
    out = {}
    async with _load(f"cases_{name}").cases(dsn) as table:
        for case, (raw, kor) in table.items():
            out[f"{name}/{case}"] = await _measure_case(raw, kor)
    return out


async def _measure_all() -> dict:
    """Measure SQLite, and Postgres when KORBASE_TEST_DSN is set."""
    with tempfile.TemporaryDirectory() as tmp:
        results = await _measure_backend("sqlite", f"sqlite:///{tmp}/bench.db")
    dsn = os.environ.get("KORBASE_TEST_DSN")
    if dsn:
        results.update(await _measure_backend("pg", dsn))
    else:
        print("KORBASE_TEST_DSN not set: skipping Postgres", file=sys.stderr)
    return results


def _table(results: dict) -> str:
    """Render measurements as a Markdown table."""
    lines = [
        "| case | raw µs | korbase µs | overhead µs | ratio |",
        "|---|---:|---:|---:|---:|",
    ]
    for case, r in results.items():
        over = r["korbase"] - r["raw"]
        lines.append(f"| {case} | {r['raw']:.1f} | {r['korbase']:.1f} | {over:.1f} | {r['ratio']:.2f} |")
    return "\n".join(lines)


def _measure(args) -> int:
    """The `measure` command."""
    results = asyncio.run(_measure_all())
    print(_table(results))
    if args.json:
        Path(args.json).write_text(json.dumps(results))
    return 0


def _run(python: str) -> dict:
    """Run `measure --json` with one interpreter and load its numbers."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.json"
        cmd = [python, __file__, "measure", "--json", str(out)]
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
        return json.loads(out.read_text())


def _compare_table(base: dict, head: dict, limit: float) -> tuple[str, bool]:
    """Return the base/head table and whether any case regressed."""
    lines = ["| case | base ratio | head ratio | change |", "|---|---:|---:|---:|"]
    failed = False
    for case in base.keys() & head.keys():
        b, h = base[case]["ratio"], head[case]["ratio"]
        change = h / b - 1
        bad = change > limit
        failed = failed or bad
        lines.append(f"| {case} | {b:.2f} | {h:.2f} | {change * 100:+.1f}%{' FAIL' if bad else ''} |")
    return "\n".join(lines), failed


def _compare(args) -> int:
    """The `compare` command."""
    table, failed = _compare_table(_run(args.base), _run(args.head), args.limit)
    print(table)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as f:
            f.write(table + "\n")
    return 1 if failed and not args.report_only else 0


def main() -> int:
    """Parse the command line and run the chosen command."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure")
    m.add_argument("--json")
    m.set_defaults(run=_measure)
    c = sub.add_parser("compare")
    c.add_argument("--base", required=True)
    c.add_argument("--head", required=True)
    c.add_argument("--limit", type=float, default=0.10)
    c.add_argument("--report-only", action="store_true")
    c.set_defaults(run=_compare)
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
