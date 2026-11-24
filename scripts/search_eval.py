"""Replay past memory_search queries and check where the entries the AI read now rank.

The audit log records each search and the memory_read calls that followed it
(see audit_log.search_reads). Treating "read after searching" as the relevant
answer gives a small, real evaluation set. This script reruns those queries on
the current library with each ranker and reports hit@1, hit@3, hit@5 and MRR.

The set is biased: the AI only read what the old ranker showed it, so a new
ranker is compared on the old ranker's home ground. Use it to catch
regressions, not to prove large gains.
"""

import argparse
import json
from pathlib import Path
import sys
import time

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE / "scripts"))
sys.path.insert(0, str(CODE / "mcp"))

import audit_log  # noqa: E402

RANKERS = ("legacy", "bm25f", "hybrid")


def build_cases(events):
    """Return [{"query", "library", "relevant": [refs]}] from search→read pairs."""
    cases = []
    seen = set()
    for search, read in audit_log.search_reads(events):
        query = (search.get("query") or "").strip()
        shown = set(search.get("refs") or [])
        # Reads of entries the search did not return (MEMORY.md, a known path)
        # say nothing about ranking, so only shown-and-read refs count.
        read = [ref for ref in read if ref in shown]
        if not query or not read:
            continue
        library = search.get("library") or "personal"
        key = (query, library, tuple(sorted(read)))
        if key in seen:
            continue
        seen.add(key)
        cases.append({"query": query, "library": library, "relevant": read})
    return cases


def evaluate(store, cases, rankers=RANKERS, limit=10):
    """Rerun each case with every ranker; return per-ranker metrics and per-case ranks."""
    report = {"cases": len(cases), "skipped": 0, "rankers": {}, "details": []}
    usable = []
    for case in cases:
        ranks = {}
        try:
            for ranker in rankers:
                hits = store.search(case["query"], limit=limit, library=case["library"], ranker=ranker)
                refs = [h.get("ref") for h in hits]
                found = [refs.index(r) + 1 for r in case["relevant"] if r in refs]
                ranks[ranker] = min(found) if found else None
        except ValueError:
            report["skipped"] += 1
            continue
        usable.append(ranks)
        report["details"].append({**case, "ranks": ranks})
    n = len(usable) or 1
    for ranker in rankers:
        best = [r[ranker] for r in usable]
        report["rankers"][ranker] = {
            "hit@1": round(sum(1 for r in best if r and r <= 1) / n, 3),
            "hit@3": round(sum(1 for r in best if r and r <= 3) / n, 3),
            "hit@5": round(sum(1 for r in best if r and r <= 5) / n, 3),
            "mrr": round(sum(1 / r for r in best if r) / n, 3),
            "missing": sum(1 for r in best if not r),
        }
    report["evaluated"] = len(usable)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memory-root", required=True)
    parser.add_argument("--days", type=float, default=0, help="Only use searches from the last N days (0 = all).")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--details", action="store_true", help="Include per-query ranks.")
    args = parser.parse_args(argv)

    from server import WorkspaceStore

    since = time.time() - args.days * 86400 if args.days else None
    store = WorkspaceStore(args.memory_root)
    try:
        report = evaluate(store, build_cases(audit_log.read_events(since=since)), limit=args.limit)
    finally:
        store.shutdown()
    if not args.details:
        report.pop("details")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
