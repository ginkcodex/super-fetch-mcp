"""Fetch a set of real sites and report what came back, from where, and how fast.

    uv run python bench/run.py                    # every site in sites.toml, fresh identity
    uv run python bench/run.py --passes 2         # again in a new pipeline: are passed challenges reused?
    uv run python bench/run.py --only cloudflare  # sites whose name or category matches
    uv run python bench/run.py --json out.json    # also save the results, to compare runs

Exits non-zero when a site not marked `flaky` fails. Results depend on the network: sites that judge
IP reputation (marked `flaky`) can block one day and pass the next.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import tempfile
import time
import tomllib
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from super_fetch.config import Settings
from super_fetch.http_tier import FetchError
from super_fetch.pipeline import Pipeline
from super_fetch.urlcheck import BlockedURLError

SITES = Path(__file__).with_name("sites.toml")


@dataclass
class Site:
    name: str
    url: str
    category: str
    expect: str
    """Text the content (or title) must contain for the fetch to count as a pass."""
    render: str = "auto"
    wait_for: str | None = None
    flaky: bool = False
    """Passing depends on IP reputation, so a failure here isn't a regression."""


@dataclass
class Outcome:
    site: str
    category: str
    run: int
    result: str
    """pass, missing (fetched, marker not found), blocked, denied (hard block) or error."""
    seconds: float
    tier: str = ""
    status: int = 0
    chars: int = 0
    detail: str = ""


def _words(text: str) -> str:
    """Lower-cased, whitespace collapsed: markers match however the content is broken into lines."""
    return " ".join(text.lower().split())


async def run_site(pipeline: Pipeline, site: Site, run: int) -> Outcome:
    started = time.monotonic()
    outcome = Outcome(site.name, site.category, run, "error", 0.0)
    try:
        result = await pipeline.fetch(site.url, render=site.render, wait_for=site.wait_for)
    except (BlockedURLError, FetchError) as exc:
        outcome.detail = str(exc)
    else:
        outcome.tier, outcome.status, outcome.chars = result.tier, result.status, len(result.content)
        denied = any(w.startswith("Access denied") for w in result.warnings)
        if result.blocked_by:
            outcome.result = "denied" if denied else "blocked"
            outcome.detail = result.blocked_by
        elif _words(site.expect) in _words(f"{result.title or ''} {result.content}"):
            outcome.result = "pass"
        else:
            outcome.result = "missing"
            outcome.detail = "; ".join(result.warnings)
    outcome.seconds = round(time.monotonic() - started, 2)
    return outcome


async def run_pass(settings: Settings, sites: list[Site], run: int) -> list[Outcome]:
    pipeline = Pipeline(settings)
    outcomes = []
    try:
        for site in sites:
            outcome = await run_site(pipeline, site, run)
            print(f"  {outcome.site:<24} {outcome.result:<8} {outcome.seconds:>6.1f}s  {outcome.tier}", file=sys.stderr)
            outcomes.append(outcome)
    finally:
        await pipeline.close()
    return outcomes


def report(outcomes: list[Outcome], sites: dict[str, Site]) -> str:
    lines = [
        "| Site | Category | Run | Result | Tier | Status | Time | Chars | Detail |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for o in outcomes:
        flaky = " (flaky)" if sites[o.site].flaky else ""
        lines.append(
            f"| {o.site}{flaky} | {o.category} | {o.run} | {o.result} | {o.tier} | {o.status or ''} "
            f"| {o.seconds:.1f}s | {o.chars} | {o.detail[:80]} |"
        )
    for run in sorted({o.run for o in outcomes}):
        done = [o for o in outcomes if o.run == run]
        passed = sum(o.result == "pass" for o in done)
        median = statistics.median(o.seconds for o in done)
        lines.append(
            f"\nRun {run}: {passed}/{len(done)} passed, median {median:.1f}s, total {sum(o.seconds for o in done):.0f}s"
        )
    return "\n".join(lines)


def load_sites(only: str | None) -> list[Site]:
    sites = [Site(**entry) for entry in tomllib.loads(SITES.read_text())["site"]]
    if only:
        wanted = {w.strip() for w in only.split(",")}
        sites = [s for s in sites if s.name in wanted or s.category in wanted]
    return sites


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="comma-separated site names or categories")
    parser.add_argument("--passes", type=int, default=1, help="runs over all sites, each in a new pipeline")
    parser.add_argument("--data-dir", type=Path, help="identity and cookies to use (default: a fresh one)")
    parser.add_argument("--json", type=Path, help="also write the results here")
    args = parser.parse_args()

    logging.getLogger("trafilatura").setLevel(logging.ERROR)
    sites = load_sites(args.only)
    if not sites:
        parser.error("no sites match --only")
    with tempfile.TemporaryDirectory(prefix="super-fetch-bench-") as fresh:
        settings = replace(Settings.from_env(), data_dir=args.data_dir or Path(fresh))
        outcomes = []
        for run in range(1, args.passes + 1):
            print(f"Run {run}/{args.passes}", file=sys.stderr)
            outcomes += await run_pass(settings, sites, run)

    by_name = {s.name: s for s in sites}
    print(report(outcomes, by_name))
    if args.json:
        args.json.write_text(json.dumps([asdict(o) for o in outcomes], indent=2))
    regressions = [o for o in outcomes if o.result != "pass" and not by_name[o.site].flaky]
    return 1 if regressions else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
