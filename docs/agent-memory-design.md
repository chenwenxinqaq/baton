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
  MCP and desktop writes advance the timestamp under the same library lock.
  A conditional move checks the source entry, even if the destination is empty;
  a missing source or ambiguous same-name entries cause a conflict. This guard
  is opt-in and does not detect out-of-band edits that keep `updated_at` intact.
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
  queries on the real library all pass. An unspaced Chinese query is one term:
  it gets `full` only if the whole phrase was found.
- **Scoped procedure phrase recall** (`procedure_phrase_variants` and
  `matched_phrase`). For a standalone Chinese phrase ending in 步骤/方法/流程,
  search can also match the same intact topic with one of the other endings,
  but only in an entry's title. It keeps this fallback `fuzzy` and shows the
  exact title phrase it matched. On the local personal and shared libraries,
  the default ranker now finds the intended procedure first for four labelled
  variants (打包方法 / 打包流程 / 排查方法 / 排查流程), up from zero in the top 10.
  All 53 existing labelled cases have unchanged relevant ranks or abstention
  verdicts across the three rankers. This does not conflate different topics
  such as 打包 and 构建.
- **Usage metrics** (`scripts/audit_log.py` `usage_stats`, `memoryctl usage`).
  Nothing in the literature; it came from the user's question of whether the
  memory saves tokens or just injects noise. It measures returned tokens, how
  often a search result is actually read, and at which rank.
- **Knowledge-update checks** (`scripts/search_eval.py`, `axis: "update"`).
  A hand-labelled case can mark outdated refs with `stale`. `freshness` is
  the fraction of those cases where a current ref is returned ahead of every
  stale ref in the top-k. On four local queries about two changed facts,
  fixed-point and hybrid ranked the current ref first in all four; BM25F
  promoted older snapshots in two. That is a regression signal, not a
  full LongMemEval score. A separate end-to-end store test checks the
  in-place case: search and read show the new conclusion at the same ref,
  while history still exposes the replaced content.

## Does following a citation repay its context cost?

`python3 scripts/search_eval.py --memory-root <root> --cases <private.json> --limit 10`
now reports `one_hop` per ranker. Use `{"query":"...", "axis":"evidence",
"relevant":["methods/a.md", "projects/b.md"]}` when an answer needs **both**
refs (ordinary `relevant` refs may be alternatives). We compare the *same*
returned hits before and after including `see_also`; linked refs never get a
rank. The cost is the estimated token difference between MCP search-result
texts, not the cost of opening an entry, model billing, or a correctness score.

On six local two-source questions, labelled before replay (12 required refs):

| Direct limit, default ranker | Required refs: direct → with links | Questions with both refs | Extra search tokens, all six |
| --- | --- | --- | --- |
| 1 | 4 → 6 | 0 → 1 | 148 |
| 3 | 7 → 8 | 2 → 3 | 116 |
| 10 | 8 → 9 | 3 → 4 | 116 |

At limit 10 only two searches actually returned hints. The 61 existing
single-answer/alternative/abstention cases cost 795 extra estimated tokens
across 11 searches with hints; their labels do **not** establish whether those
hints are useful. This small, purpose-built set supports keeping optional
one-hop hints, not expanding roots or hop count by default. If a compound
question is still missing a source, search its missing subject explicitly.

### Cost after opening a source

A separate local check selected five factual questions about four structured
entries. For each, the test reader knew which section to choose and checked
two preselected evidence strings in both full and targeted responses. Five
full MCP result texts cost 7,123 estimated tokens. Five outlines followed by
five section reads cost 3,239 (55% less); with section names already known,
the five reads alone cost 1,824. Selected sections no longer repeat the entire
outline, saving 680 estimated tokens across those five reads. Full and outline
reads retain navigation; all reads of a versioned entry retain `updated_at`.
This measures retrieval payloads with known sections, not model accuracy or
latency; extra round trips and a mistaken section choice can erase the benefit.

## What we tried and rejected

- **BM25F / hybrid ranking as the default.** Implemented and measured against a
  40-query hand-labelled set over 141 entries: the fixed-point ranker scored
  hit@1 0.975 and MRR 0.981, BM25F 0.975 / 0.981, hybrid 0.975 / 0.983, and on
  vague queries hybrid demoted the right entry (IDF promotes an entry that only
  shares one rare term; length normalization favours very short entries). Kept
  as opt-in via `UNIFIED_MEMORY_SEARCH_RANKER`; see
  `mcp/server.py` `SEARCH_RANKERS` and `scripts/search_eval.py`. Broader semantic
  recall, beyond the scoped procedure variants above, remains open.
- **A raw/ ingest layer and a knowledge graph.** Both the LLM-wiki pattern and
  HippoRAG assume a corpus being ingested. Our libraries are written by agents a
  few entries at a time, so the raw layer would be empty and the graph would
  have fewer edges than the entries themselves.

## Open ideas from the reading list

- Broader knowledge-update evaluation: these cases check retrieval order and
  in-place replacement, not whether an agent recognizes and applies a new
  fact on its own or answers historical questions correctly.
- A curation pass for shared libraries: triage a candidate as new / update /
  disputed / no-material before writing, with `draft` / `stable` /
  `needs-update` status, from the LLM-wiki projects and Zep's invalidation.
