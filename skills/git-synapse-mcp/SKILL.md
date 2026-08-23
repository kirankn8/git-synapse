---
name: git-synapse-mcp
description: Use the Git Synapse MCP server to find what else has to change. Before editing a file, before fixing a bug, or when asked "what else does this affect" - Git Synapse computes change coupling from 298k commits across 272 acme repositories and answers both "which other files in this repo move with it" and "which other repository does this change really belong in". Also covers when NOT to act on a result, which is most of the value.
---

# Git Synapse MCP: what else has to change

**Coupling is computed from the default branch only.** Work on branches that
never merged is not part of the shipped history and is excluded.

**Scope: file level and no finer.** The atomic record is one row per (commit,
file); no code is parsed. Git Synapse cannot answer anything about functions, methods
or symbols, and never will -- the distinction is not in the data. For coupling
inside a file, read the code.

Git Synapse answers one question from real commit history:

> **If I change this, what else has historically had to change too?**

Two levels. The second prevents the most common incomplete change: patching a
symptom in the repository you were pointed at, when the defect lives upstream.

Server: `http://git-synapse:8081/mcp` · UI: `http://git-synapse` · REST: `http://git-synapse/api/docs`

If the tools are unavailable, **say so**. A fabricated coupling claim is worse
than no claim.

---

## Tools

**Same repository**

| tool | use |
|---|---|
| `coupled_files(repo, path)` | Which files move with this one. **Start here before editing.** |
| `explain_pair(repo, path_a, path_b)` | Full 2×2 table, all 29 measures, and the commits behind them |
| `file_history(repo, path)` | Recent commits and who actually owns the file |
| `module_context(repo, path)` | In a monorepo: which module owns this file, what it declares, and what declares it |
| `search_files(term)` | Resolve a path |
| `repo_hotspots(repo)` | Churn leaders — orient in an unfamiliar repository |

**Across repositories**

| tool | use |
|---|---|
| `upstream_repos(repo)` | **Call before fixing a bug.** Repositories whose changes *precede* this one |
| `impact_of_change(repo)` | The reverse: what a change here forces others to update |
| `coupling_chain(repo, direction)` | Multi-hop paths, upstream or downstream |
| `explain_repo_pair(a, b)` | Declared status, every observed bump, exact upstream commits consumed |
| `coupled_directories(repo, path)` | Which other directories move with this one. The package-level question |
| `crossrepo_files(repo, path)` | Which specific file in another repository pairs with this one |
| `list_repositories()`, `list_measures()` | What exists; the measure catalogue with caveats |
| `report_gap(kind, detail, ...)` | **When Git Synapse itself is wrong.** See below |

Optional arguments worth knowing: `measure`, `min_support`, `limit` on
`coupled_files`; `direction` and `max_depth` on `coupling_chain`;
`declared_only` on `impact_of_change`.

---

## Reading a same-repo result

Read three fields **together**. A score on its own means nothing.

| field | meaning |
|---|---|
| `probability_also_changes` | `P(partner changes \| this changed)` — the actionable number |
| `co_changes` | commits the pair actually shares. **Under 3 is noise, whatever the score** |
| `currency` | whether the coupling still holds. **Read this before acting** |
| `interpretation` | plain-language summary, already calibrated |
| `informative` | `false` means skip it: this file's own test, generated output, or too few shared commits to mean anything |
| `labels` | `sibling_variant`, `generated`, `own_test`, `thin_support` |

Read the top-level `summary` first when present. It leads with whichever of two
things matters: sibling variants found, or how much of the result is noise.

Partners sharing fewer than three commits with yours are withheld. A pair that
changed twice and never apart scores 1.0 and would sort above everything real;
that is arithmetic on two commits, not evidence.

`upstream_repos` returns only declared and bump-backed edges. Discovery-tier
edges are withheld and counted in `guidance`; pass `include_discovery=true` if
you want them, but they have never been worth acting on.

`sibling_variant` is the case worth stopping for -- the same filename under a
different parent, such as an `amd-values-yaml/` and `nvidia-values-yaml/` copy,
or a per-cloud or per-arch duplicate. Parallel files must usually be edited in
lockstep, and this is the one pattern the tool finds that reading the file you
are editing does not. Everything else it reports tends to confirm rather than
discover.

`currency` is not optional reading. A score is computed over all of history, so a
partner that was deleted in a refactor keeps every co-change it ever had and can
still rank near the top. It reports, in order of severity:

- `DELETED` — the file no longer exists at HEAD. Historical coupling; nothing to
  edit. If the behaviour moved, find where it moved to.
- `DECAYING` — weaker lately than historically. Usually a finished refactor.
- `STALE` — no co-change in a long time.
- `current` / `co-changed N days ago` — live.

Act on partners above roughly 60% with double-digit support. Mention but do not
edit the 20–40% band. Ignore below that, and ignore anything with
`informative: false` regardless of score.

**`coupled_files` is file-level and cannot see inside a file.** If the coupling
you need is between functions in one file, it will not find it -- read the code.
Its value is highest as a guardrail ("nothing else across the repository has to
move") and on parallel-file layouts.

For the package or subsystem question, use `coupled_directories`. A Go package is
a directory, so "what else in this subsystem moves with mine" is answerable even
though "what else in this file" is not. Read its `informative` flag: a directory's
own parents and children score near 1.0 because every change to a child is a
change to the parent, which is arithmetic rather than a finding. The
`sibling-or-unrelated` rows are the ones carrying information.

Raise `min_support` when a repository is noisy.

---

## Reading a cross-repo result: the tier decides

Every cross-repo result carries an `evidence` field. The tiers sit on **different
scales** — never sort them into one list or compare their scores.

| tier | meaning | what to do |
|---|---|---|
| `declared` | the consumer declares it in a manifest | AUC 0.88 in sample, 0.69 held out — the best evidence there is; act on it |
| `bump-backed` | an actual version bump was observed | ground truth — act on it |
| `discovery` | statistical only | **unvalidated** — a lead to verify, not a fact |

`discovery` rows skew toward repositories that simply commit a lot. Treating one
as fact is the likeliest way to be confidently wrong with this tool.

Read `median_lag_days` too: a short lag means the change belongs in the same PR,
a long one means a follow-up that is not your problem now.

---

## When NOT to act

High coupling is a prompt to look, not a mandate to edit. **Naive use of this
data makes an agent worse, not better.** Do not change a file merely because it
appeared in a list.

- **Mechanical pairs.** Lockfiles beside their manifests, and changelogs beside
  release tooling, top almost every ranking. They are build artefacts, not design
  coupling. Regenerate if the change warrants it; otherwise skip.
- **Rare-item bias.** A perfect score on two shared commits is arithmetic, not
  evidence. Always read `co_changes`.
- **Generated code.** Generated specs, `*_gen.go` and vendored trees couple to
  everything upstream of them. Change the source, then regenerate.
- **Hub files.** A file with thousands of coupling partners tells you nothing
  specific.
- **Finished refactors.** A pair coupled years ago but not lately is a completed
  migration. `http://git-synapse/insights?tab=drift` separates `emerging` from
  `decaying`; do not act on `decaying`.
- **Absence of a result is not absence of coupling.** Coverage is the
  `acme` organisation only, and declared-dependency evidence exists for
  Go repositories.
- **A deleted or decaying partner.** `currency` says so; do not spend a step
  confirming a file exists that the tool already told you does not.

---

## Monorepos: check the module graph, not just the files

A repository with several modules has a dependency structure in its own
manifests, and cross-repo tools cannot see it — every internal reference points
back at the same repository, so `upstream_repos` correctly returns nothing.
`module_context` is the structural prior in that case, and it is a fact from the
manifest rather than a correlation.

The reverse direction is the one that matters: changing a shared module is a
change to everything that declares it. A file in a widely-declared module
warrants more care than any co-change score conveys, and a file whose module
declares others is a hint that the behaviour you want may belong in one of them.

## The upstream score is a rank, not a probability

`score` on an `upstream_repos` or `impact_of_change` row is a rank position
within the corpus, not a likelihood. A `discovery` edge at 0.999 means "ranked
first among unvalidated guesses", not "almost certainly related". Read
`guidance` first: it states how many entries are declared, bump-backed and
discovery, and says plainly when none of them is validated.

When `coupling_chain` returns no chains, read `explanation`. Traversal follows
declared and bump-backed edges only, so a repository that declares no internal
dependencies has nothing to walk -- that is "there was nothing to search", not
"searched and found nothing", and it is not evidence that no relationship exists.

---

## If Git Synapse is wrong, say so

`report_gap` records a defect in Git Synapse itself: data missing that should be
indexed, a value that contradicts the repository, something correct once and now
stale, a tool that failed, a repository or path not covered. Include what you
expected and what you got, so it is reproducible.

Every improvement made on the first day of use came from a session noticing
exactly that — monorepo manifests below the root were never scanned, deleted
files were still ranked as editable partners, a monorepo's own module graph was
discarded. All three were found this way and are now fixed.

Two limits, both deliberate:

- It is **not** a way to disagree with a score or suppress a suggestion. Use
  judgment for that and say so in your report to the human instead.
- Reports feed **no** measure, score or ranking. Letting sessions write into the
  coupling data would close a confirmation loop: Git Synapse suggests a pair, the
  agent edits both files, the commit strengthens the pair, Git Synapse suggests it
  more confidently. Agent-authored commits are already ~16% of the last week of
  history, so that risk is live.

## Direction is the useful part

Coupling is rarely symmetric. A specification and its implementation illustrate
the general shape: the upstream side rarely changes without the downstream side
following, while the downstream side changes constantly for its own reasons. So
the same pair warrants action in one direction and not the other.

Compare `probability_also_changes` against `probability_reverse` before calling a
relationship bidirectional, and let the weaker direction go.

---

## Reporting a finding

State the evidence, not just the conclusion. Cite the support count alongside any
percentage — a percentage without its denominator is how a weak signal gets
mistaken for a strong one. Name the evidence tier when reporting a cross-repo
result, and when the upstream tier is `declared` or `bump-backed`, offer to look
there before editing locally rather than doing both silently.

---

## Choosing a measure

NPMI is the default and right most of the time. Override when:

| Want | Ask for |
|---|---|
| "Will I have to touch it?" | `confidence_ab` — the directional conditional |
| Statistical confidence | `log_likelihood_ratio` — calibrated on rare events |
| Safety over the long tail | `fager` — Ochiai with a small-sample penalty |
| A figure to state plainly | `association_strength` — a multiple of chance |
| Signed coupling | `phi` — negative means the two systematically avoid each other |

**Do not rank by** `sokal_michener`, `rogers_tanimoto`, `hamann` or `faith`. They
count joint *absence*, and since almost no commit touches any given file they sit
near 1.0 for nearly every pair.

---

## What the numbers are worth

Validated against ground truth recovered from Go pseudo-versions, where a
manifest diff names the exact upstream commit consumed — provable edges, not
inference.

| Approach | AUC | Directional accuracy |
|---|---|---|
| Best single measure over all ordered repository pairs | 0.80 | **0.63** |
| Ensemble ranked *within declared dependencies*, in sample | **0.884** | — |
| The same, held out in time (features pre-2025, labels after) | **0.685** | — |

A high AUC is not the same as a useful answer. The measure topping the global
table is pure joint frequency, which scores well by ranking *both repositories
are busy* while being close to a coin flip on which way the arrow points. That is
activity confounding, and it is the reason the evidence tiers exist and why
`discovery` excludes every frequency-weighted measure.

---

## What this is not

Git Synapse reads **only commit metadata and changed file paths** — never file
contents, imports, or a call graph. Coupling is *behavioural*.

That cuts both ways. It surfaces coupling no manifest declares — deployment charts,
documentation and configuration that must move with the code.
It also lists declared dependencies that have never once co-changed. And it can
never tell you *why* two things are coupled, only that they are and how strongly.

Use it to decide where to look, then read the code.
