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
  "gracefully invalidate, keep the old state" half of Zep made usable. The
  desktop entry page lists the same versions and restores one in place
  (`Library.topic_versions`, `/restore_version`), so a person who overwrote an
  entry by hand gets back the recovery path the agents already had.
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
- **One-hop expansion** (`see_also` on the best search hit, `mcp/server.py`
  `_attach_see_also`). HippoRAG (2405.14831) and *From RAG to Memory*
  (2502.14802) argue that following associations recovers facts a single lookup
  misses. The associations already exist as `methods/xxx.md` citations, so the
  top hit now names up to 3 entries it cites that the query itself did not
  return. Skipped when the top hit is only a `fuzzy` match, and in a
  multi-library search only the overall best hit keeps it. On the real libraries
  84 of 143 entries carry at least one citation (175 edges), and a query like
  "docker inspect 卡死" gains the generic hang-diagnosis procedure that shares no
  query term.
- **Particle-aware Chinese queries** (`mcp/server.py` `cjk_chunks`). Chinese
  queries are matched by overlapping bigrams, so 桌面版的打包方法 used to emit
  版的 and 的打 — bigrams that carry no topic, match unrelated entries, and raise
  the number of bigrams a genuine match must reach (`score_fields` needs half of
  them). Query runs are now split at 的吗呢吧嘛呀啦啊哦哈 only; 地/得/在/对/过 stay
  because 地址, 得到, 在线, 对齐, 过期 are real terms in these libraries. On the
  real libraries "最新的共享库同步问题" went from ranking an unrelated archive entry
  first to ranking `projects/unified-memory-shared-architecture.md` first, and the
  48-case labelled set is unchanged (legacy hit@1 0.975, MRR 0.981).
- **Match strength** (`match` = `full` / `partial` / `fuzzy` / `recent` on each
  search hit, `mcp/server.py` `match_strength`). LongMemEval (2410.10813) scores
  abstention: an agent should say "not in memory" instead of answering from the
  closest entry. A ranked list always has a top hit, so the agent needs a cue
  for how much of the query that hit covers. `scripts/search_eval.py` scores
  abstention cases (empty `relevant`) as passing when no hit is `full`; 8 such
  queries on the real library all pass. Unspaced Chinese queries are a single
  word, so they only ever match `fuzzy`.
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

- Recall-oriented eval. `see_also` adds entries no hit@k metric rewards, so its
  value is still judged by hand on sample queries, not measured.
- LongMemEval (2410.10813) knowledge-update axis still has no labelled cases:
  does a search find the newest version of a changed fact. `search_eval.py`
  accepts an `axis` field and reports each axis separately; extraction,
  abstention and temporal are covered (5 temporal cases, legacy hit@1 1.0 and
  MRR 1.0, while bm25f drops to 0.4 and hybrid to 0.8 — a third reason the
  legacy ranker stays the default). An entry is overwritten in place, so this
  axis mostly tests whether a stale snapshot was ever updated, not retrieval.
- A curation pass for shared libraries: triage a candidate as new / update /
  disputed / no-material before writing, with `draft` / `stable` /
  `needs-update` status, from the LLM-wiki projects and Zep's invalidation.
