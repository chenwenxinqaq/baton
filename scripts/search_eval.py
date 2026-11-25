"""Replay past memory_search queries and check where the entries the AI read now rank.

The audit log records each search and the memory_read calls that followed it
(see audit_log.search_reads). Treating "read after searching" as the relevant
answer gives a small, real evaluation set. This script reruns those queries on
the current library with each ranker and reports hit@1, hit@3, hit@5 and MRR.

The set is biased: the AI only read what the old ranker showed it, so a new
ranker is compared on the old ranker's home ground. Use it to catch
regressions, not to prove large gains.

With --cases FILE it instead reads a hand-labeled set, a JSON list of
``[query, [relevant refs]]`` pairs (or objects with "query"/"relevant" and an
optional "library", default "*", and "axis"). Keep such a file outside the
repository when the queries or refs describe private work.

The "axis" field follows LongMemEval (arXiv 2410.10813): "extraction" (default),
"update" (the newest version of a changed fact should rank first),
"temporal" (queries with time cues), and "abstention" (the answer is not in
memory, so "relevant" is empty). An abstention case passes when no hit has
match="full"; it reports how often search would tempt the agent with a
confident-looking wrong entry. Update cases may include a ``stale`` list of
refs; their freshness score passes only when a current relevant ref is
returned and precedes every stale ref in the top-k. This measures retrieval,
not whether an agent answers correctly or updates the content at a stable ref.
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
        cases.append({"query": query, "library": library, "relevant": read, "axis": "extraction"})
    return cases


def load_cases(path, library="*"):
    """Read hand-labeled cases: [query, [refs]] pairs or {"query", "relevant"} objects."""
    return load_cases_from_rows(json.loads(Path(path).read_text(encoding="utf-8")), library=library)


def load_cases_from_rows(raw, library="*"):
    """Normalize case rows; rows with no relevant refs are abstention cases."""
    cases = []
    for i, row in enumerate(raw, 1):
        if isinstance(row, dict):
            query, relevant = row.get("query", ""), row.get("relevant") or []
            lib = row.get("library") or library
            axis = row.get("axis") or ""
            stale = row.get("stale", [])
        else:
            query, relevant = row[0], list(row[1])
            lib = row[2] if len(row) > 2 else library
            axis = row[3] if len(row) > 3 else ""
            stale = []
        query = (query or "").strip()
        axis = axis or ("extraction" if relevant else "abstention")
        if not isinstance(stale, list) or not all(isinstance(ref, str) and ref.strip() for ref in stale):
            raise ValueError(f"Case {i}: stale must be a list of entry refs.")
        if stale and (axis != "update" or not relevant or set(stale).intersection(relevant)):
            raise ValueError(f"Case {i}: stale refs require an update case and must be distinct from relevant refs.")
        if not query or (not relevant and axis != "abstention"):
            continue
        case = {
            "query": query,
            "library": lib,
            "relevant": list(relevant),
            "axis": axis,
        }
        if stale:
            case["stale"] = stale
        cases.append(case)
    return cases


def evaluate(store, cases, rankers=RANKERS, limit=10):
    """Rerun each case with every ranker; return per-ranker metrics and per-case ranks."""
    report = {"cases": len(cases), "skipped": 0, "rankers": {}, "details": []}
    usable = []
    abstain = []
    for case in cases:
        ranks = {}
        stale_ranks = {}
        try:
            for ranker in rankers:
                hits = store.search(case["query"], limit=limit, library=case["library"], ranker=ranker)
                if not case["relevant"]:
                    # Abstention: pass when nothing looks like a confident answer.
                    ranks[ranker] = not any(h.get("match") == "full" for h in hits)
                    continue
                refs = [h.get("ref") for h in hits]
                found = [refs.index(r) + 1 for r in case["relevant"] if r in refs]
                current_rank = min(found) if found else None
                ranks[ranker] = current_rank
                if case.get("stale"):
                    stale_ranks[ranker] = next(
                        (i for i, ref in enumerate(refs, 1) if ref in case["stale"]), None
                    )
        except ValueError:
            report["skipped"] += 1
            continue
        detail = {**case, "ranks": ranks}
        if stale_ranks:
            detail["stale_ranks"] = stale_ranks
        (usable if case["relevant"] else abstain).append(detail)
        report["details"].append(detail)
    for ranker in rankers:
        report["rankers"][ranker] = _metrics([c["ranks"][ranker] for c in usable])
        if abstain:
            report["rankers"][ranker]["abstention"] = round(
                sum(1 for c in abstain if c["ranks"][ranker]) / len(abstain), 3)
    axes = sorted({c.get("axis", "extraction") for c in usable})
    update_cases = [c for c in usable if c.get("stale_ranks")]
    if len(axes) > 1 or abstain or "update" in axes:
        report["axes"] = {
            axis: {ranker: _metrics([c["ranks"][ranker] for c in usable if c.get("axis", "extraction") == axis])
                   for ranker in rankers}
            for axis in axes
        }
        if update_cases:
            for ranker in rankers:
                passes = sum(_ranks_before_stale(c["ranks"][ranker], c["stale_ranks"][ranker])
                             for c in update_cases)
                report["axes"]["update"][ranker].update(
                    freshness=round(passes / len(update_cases), 3),
                    freshness_cases=len(update_cases),
                )
        if abstain:
            report["axes"]["abstention"] = {
                ranker: {"pass": round(sum(1 for c in abstain if c["ranks"][ranker]) / len(abstain), 3),
                         "cases": len(abstain)}
                for ranker in rankers
            }
    report["evaluated"] = len(usable) + len(abstain)
    return report


def _ranks_before_stale(current, stale):
    return current is not None and (stale is None or current < stale)


def _metrics(best):
    n = len(best) or 1
    return {
        "hit@1": round(sum(1 for r in best if r and r <= 1) / n, 3),
        "hit@3": round(sum(1 for r in best if r and r <= 3) / n, 3),
        "hit@5": round(sum(1 for r in best if r and r <= 5) / n, 3),
        "mrr": round(sum(1 / r for r in best if r) / n, 3),
        "missing": sum(1 for r in best if not r),
        "cases": len(best),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--memory-root", required=True)
    parser.add_argument("--days", type=float, default=0, help="Only use searches from the last N days (0 = all).")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--details", action="store_true", help="Include per-query ranks.")
    parser.add_argument("--cases", help="Hand-labeled JSON case file; skips the audit log.")
    parser.add_argument("--library", default="*", help="Library for hand-labeled cases without one.")
    args = parser.parse_args(argv)

    from server import WorkspaceStore

    since = time.time() - args.days * 86400 if args.days else None
    if args.cases:
        cases = load_cases(args.cases, library=args.library)
    else:
        cases = build_cases(audit_log.read_events(since=since))
    store = WorkspaceStore(args.memory_root)
    try:
        report = evaluate(store, cases, limit=args.limit)
    finally:
        store.shutdown()
    if not args.details:
        report.pop("details")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
