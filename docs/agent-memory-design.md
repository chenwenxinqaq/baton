# Design notes: what we took from the agent-memory literature

This file records where each memory feature comes from and which part of the
research it implements. It is a repo document on purpose: §0 in
`templates/library.md` is injected into every agent session, so it stays short,
and the reasoning lives here instead.

## How L0–L4 maps to CoALA

CoALA (arXiv 2309.02427) splits agent memory into working, episodic, semantic
and procedural memory. Our layers are the same split, written as files:

| Layer | Directory | CoALA kind | Holds |
| --- | --- | --- | --- |
| L0 | `constraints/` | semantic (hard rules) | red lines and authorization limits the user set |
| L1 | `methods/` | procedural | verified, reusable procedures |
| L2 | `preferences/` | semantic (about the user) | stated preferences and corrections |
| L3 | `projects/` | episodic | dated snapshots, decisions, blockers |
| L4 | `archive/` | episodic (closed) | incidents with a known root cause |

Working memory is the agent's own context window. Our job is to keep what we
inject into it small, which is why reads are progressive and §0 is tiny.

## Feature → source

- **Outline and section reads** (`memory_read` with `outline=true` or
  `section="..."`, `mcp/server.py` `split_sections` / `section_outline`).
  MemGPT (2310.08560) pages memory in and out instead of pasting everything;
  Anthropic's context-engineering guidance calls the same idea progressive
  disclosure. Measured on the real library: `MEMORY.md` is about 2918 tokens in
  full, 209 as an outline, 1224 for the §0 section alone.
- **Write guards** (`expected_updated_at`, `previous`, `content_warning` in
  `upsert_entry`). Zep (2501.13956) invalidates old facts instead of silently
  overwriting them; Mem0 (2504.19413) treats a write as extract-and-update with
  explicit conflict resolution. We cannot diff semantics in a Markdown store, so
  we do the cheap equivalent: compare-and-set on `updated_at`, a receipt for the
  version that was replaced, and a warning when a write shrinks an entry a lot.
- **Duplicate hints** (`related` on a new entry). Mem0's update step and A-MEM
  (2502.12110) both avoid growing a pile of near-identical records. Before
  creating a new file we search for neighbours and tell the agent to fold them
  together.
- **History and restore** (`memory_history`, `memory_restore`). Trash backups
  already existed under
  `~/.local/state/unified-memory/libraries/<hash>/trash/` but nothing could read
  them, so a bad overwrite was unrecoverable in practice. This is the
  "gracefully invalidate, keep the old state" half of Zep made usable.
- **Links and backlinks** (`links` / `backlinks` on a full read,
  `ENTRY_LINK_RE`). A-MEM builds Zettelkasten cards with bidirectional links;
  HippoRAG (2405.14831) and *From RAG to Memory* (2502.14802) argue that
  associativity, not better embeddings, is what retrieval over a memory store is
  missing. Our cheap version: cite other entries as `methods/xxx.md` and the
  store resolves both directions on read.
- **Library health** (`memory_lint`, `umem lint`, the 记忆库体检 block on the
  activity page). Letta's sleep-time compute and Reflexion (2303.11366) both
  separate "do the task" from "tidy what you learned". Lint is the offline pass:
  duplicate frontmatter, index drift, missing dates, stale snapshots, oversized
  entries, suspected duplicate topics.
- **Usage metrics** (`scripts/audit_log.py` `usage_stats`, `memoryctl usage`).
  Nothing in the literature; it came from the user's question of whether the
  memory saves tokens or just injects noise. It measures returned tokens, how
  often a search result is actually read, and at which rank.

## What we tried and rejected

- **BM25F / hybrid ranking as the default.** Implemented and measured against a
  40-query hand-labelled set over 141 entries: the fixed-point ranker scored
  hit@1 0.975 and MRR 0.981, BM25F 0.975 / 0.981, hybrid 0.975 / 0.983, and on
  vague queries hybrid demoted the right entry (IDF promotes an entry that only
  shares one rare term; length normalization favours very short entries). Kept
  as opt-in via `UNIFIED_MEMORY_SEARCH_RANKER`; see
  `mcp/server.py` `SEARCH_RANKERS` and `scripts/search_eval.py`. The remaining
  recall gap is synonyms, which needs embeddings plus fusion, not weight tuning.
- **A raw/ ingest layer and a knowledge graph.** Both the LLM-wiki pattern and
  HippoRAG assume a corpus being ingested. Our libraries are written by agents a
  few entries at a time, so the raw layer would be empty and the graph would
  have fewer edges than the entries themselves.

## Open ideas from the reading list

- One-hop expansion at search time (follow links of the top hit), from HippoRAG's
  associativity argument. Needs an eval set that rewards recall, not just rank.
- LongMemEval (2410.10813) axes we do not test yet: knowledge updates (does the
  agent find the newest version of a changed fact), temporal reasoning, and
  abstention (saying "not in memory" instead of returning the closest entry).
- A curation pass for shared libraries: triage a candidate as new / update /
  disputed / no-material before writing, with `draft` / `stable` /
  `needs-update` status, from the LLM-wiki projects and Zep's invalidation.
