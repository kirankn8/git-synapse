"""Does this actually help? Answered against history itself.

Every other number in this system describes the corpus. This one describes the
*product*: replay history commit by commit and ask, before each commit is
revealed, "given one file this commit touched, would we have named the others?"

Method
------
Strictly prequential -- test, then train. Walking commits in time order, each
commit is first scored using only the counts accumulated from earlier commits,
and only afterwards do its own pairs update those counts. A file pair therefore
never contributes evidence to its own prediction, which is the whole difficulty
in evaluating a co-change model, and the reason the materialised
``file_pair_metric`` table cannot be used here: it is computed over all history,
so every query against it has already seen the future.

Reading the result honestly
---------------------------
Three things are reported next to every measure, because recall alone is a
vanity metric:

* **Lift over the strongest baseline**, not a convenient one. "Guess the busiest
  files" is a straw man: it is not how anyone finds files. An agent greps for a
  symbol, opens what it finds, and looks at the obvious neighbours -- the test
  beside the source, the header beside the implementation, the rest of the
  directory. Those cost nothing and are usually right, so the only question
  worth asking is whether co-change history adds anything **on top of them**.
  Three baselines are therefore scored, and lift is measured against whichever
  does best.
* **A 95% confidence interval** on the hit rate, so a difference between two
  measures is not read as real when the sample cannot support it.
* **Whether the measure is known to over-reward rare items**, which is exactly
  how a measure tops this table while being useless in practice.

The interval assumes independent trials. Prompts drawn from the same commit are
not independent, so the true interval is somewhat wider than the one reported.
"""

from __future__ import annotations

import logging
import math
import random
import re
import subprocess
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import combinations
from pathlib import Path

import numpy as np

from git_synapse.db.engine import query
from git_synapse.ingest.gitops import _base_env, mirror_path_for
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.registry import DEFAULT_MEASURE, resolve

log = logging.getLogger(__name__)

#: Commits observed before scoring begins. With no history there is nothing to
#: predict from, and those degenerate commits would otherwise drag every
#: average toward zero and make two runs incomparable.
WARMUP_COMMITS = 20

#: Commits touching more files than this are skipped as prompts. A sweeping
#: change has no single "the file you are editing", so asking the question of it
#: measures nothing about the product.
MAX_FILES_PER_PROMPT = 25

#: Below this many prompts the intervals are too wide to separate anything, and
#: the result is reported as indicative rather than as a finding.
MIN_PROMPTS_FOR_A_VERDICT = 300

#: The one baseline that is sampled rather than run over every prompt.
SAMPLED_BASELINE = "newhire"


def wilson(successes: int, trials: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval, which stays sane at proportions near 0 and 1."""
    if trials == 0:
        return (0.0, 0.0)
    p = successes / trials
    denom = 1.0 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    margin = z * math.sqrt(p * (1 - p) / trials + z * z / (4 * trials * trials)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


@dataclass(frozen=True)
class Score:
    """How one measure fared over the whole replay."""

    measure: str
    label: str
    prompts: int
    #: Prompts where at least one correct file appeared in the top k.
    hit_prompts: int
    #: Correct files found, counting every one, not just the first.
    found: int
    wanted: int
    hit_rate: float
    ci_low: float
    ci_high: float
    recall_at_k: float
    precision_at_k: float
    mrr: float
    lift: float = 0.0
    rare_item_bias: bool = False
    #: Hit rate over the prompts the free neighbour rules did *not* solve. The
    #: number that matters: on everything else this product is redundant.
    hard_hit_rate: float = 0.0
    hard_prompts: int = 0
    #: As above, but against everything an agent can do unaided -- the free
    #: rules *and* its own search. Measured on the sampled subset only.
    unaided_hits: int = 0
    unaided_prompts: int = 0
    #: Hit rate of the baseline lift was measured against, kept so that beating
    #: it can be tested against the interval rather than the point estimate.
    baseline_hit_rate: float = 0.0

    @property
    def unaided_hit_rate(self) -> float:
        """Share of the unaided-unsolved prompts this measure still answered."""
        return self.unaided_hits / self.unaided_prompts if self.unaided_prompts else 0.0

    @property
    def unaided_ci(self) -> tuple[float, float]:
        return wilson(self.unaided_hits, self.unaided_prompts)

    @property
    def beats_baseline(self) -> bool:
        """True only when the interval clears the baseline, not merely the point.

        The point estimate crossing 1.0 is what a lift column shows and is not
        evidence on its own; a measure one noisy percent above the baseline has
        not beaten it.
        """
        return self.ci_low > self.baseline_hit_rate


@dataclass(frozen=True)
class BacktestResult:
    """The outcome of one replay, across every measure evaluated."""

    repo_id: int | None
    k: int
    min_support: int
    commits_seen: int
    commits_scored: int
    prompts: int
    #: Every baseline scored, hardest first. Lift is measured against the first.
    baselines: list[Score] = field(default_factory=list)
    scores: list[Score] = field(default_factory=list)
    seeding: str = "all"

    @property
    def sampled(self) -> int:
        """How many prompts the New Hire was actually run against."""
        return next((b.prompts for b in self.baselines if b.measure == "newhire"), 0)

    @property
    def baseline(self) -> Score:
        """The hardest baseline lift is measured against.

        Sampled baselines are excluded. Lift is a ratio, and dividing a rate
        measured over every prompt by one measured over a few hundred mixes two
        estimators: the figure would then move with the draw rather than with
        the product. The New Hire is still shown in the table, and it is what
        the unaided count is computed against.
        """
        full = [b for b in self.baselines if b.measure != SAMPLED_BASELINE]
        return (full or self.baselines)[0]

    @property
    def conclusive(self) -> bool:
        """False when the sample is too small to support any claim."""
        return self.prompts >= MIN_PROMPTS_FOR_A_VERDICT

    @property
    def best(self) -> Score | None:
        """The measure with the highest hit rate, or None if nothing scored."""
        return max(self.scores, key=lambda s: s.hit_rate, default=None)

    @property
    def verdict(self) -> str:
        """One line stating what this run does and does not establish."""
        if not self.prompts:
            return "no prompts: not enough history to replay"
        best = self.best
        if best is None:
            return "no measures evaluated"
        if not self.conclusive:
            return (f"indicative only: {self.prompts} prompts is below the "
                    f"{MIN_PROMPTS_FOR_A_VERDICT} needed to separate measures")
        rung = self.baseline.label.split(" --")[0]
        tail = (f"; on the {best.hard_prompts:,} prompts the free rules missed, "
                f"it still answers {best.hard_hit_rate:.1%}")
        if best.ci_low <= self.baseline.hit_rate:
            return (f"{best.measure} ({best.hit_rate:.1%}) does not beat the "
                    f"{rung} ({self.baseline.hit_rate:.1%}) by more than noise"
                    + tail)
        return (f"{best.measure} hits {best.hit_rate:.1%} vs "
                f"{self.baseline.hit_rate:.1%} for the {rung} "
                f"({best.lift:.2f}x)" + tail)


def _commit_shas(repo_id: int | None) -> dict[int, tuple[str, str, str]]:
    """commit id -> (sha, repository full name, host), for the grep baseline.

    The host travels with the name because it is half the mirror's address:
    `owner/name` is unique on one host, so without it two repositories can
    resolve to the same directory on disk.
    """
    where = "" if repo_id is None else "WHERE c.repo_id = %(repo)s"
    rows = query(f"""SELECT c.id, c.sha, r.full_name, r.host FROM commit c
                       JOIN repo r ON r.id = c.repo_id {where}""", {"repo": repo_id})
    return {r["id"]: (r["sha"], r["full_name"], r["host"]) for r in rows}


def _history(repo_id: int | None) -> list[tuple[int, list[int], int]]:
    """Pair-eligible commits in time order, as ``(repo_id, file ids)``.

    The repository travels with the commit because counts must never be pooled
    across repositories. Two files in different repositories cannot co-occur, so
    a shared population would hand the popularity baseline a set of candidates it
    can never hit -- which does not weaken the baseline honestly, it breaks it,
    and every lift measured against it is inflated.
    """
    where = "WHERE c.pair_eligible" + ("" if repo_id is None else " AND c.repo_id = %(repo)s")
    rows = query(
        f"""
        SELECT c.id AS commit_id, c.repo_id, cf.file_id
          FROM commit c JOIN commit_file cf ON cf.commit_id = c.id
          {where}
      ORDER BY c.committed_at, c.id, cf.file_id
        """,
        {"repo": repo_id},
    )
    grouped: dict[int, list[int]] = defaultdict(list)
    owner: dict[int, int] = {}
    for row in rows:
        grouped[row["commit_id"]].append(row["file_id"])
        owner[row["commit_id"]] = row["repo_id"]
    return [(owner[cid], files, cid) for cid, files in grouped.items()]


#: Words that occur in half the files of any repository. An agent drops them by
#: instinct; leaving them in makes every query match everything.
_STOPWORDS = frozenset((
    "test", "tests", "spec", "specs", "main", "index", "util", "utils",
    "common", "base", "core", "impl", "internal", "public", "private",
    "config", "types", "type", "data", "file", "files", "code", "java",
    "class", "interface", "string", "value", "result", "error", "context",
))

#: Declaration forms across the languages in scope. What is captured is the
#: name another file has to write down in order to use this one, which is the
#: term an agent searches for once it has read the file.
_DECL = re.compile(
    r"\b(?:class|interface|struct|enum|trait|type|func|def|fn|module|"
    r"package|function)\s+([A-Za-z_][A-Za-z0-9_]{3,})")

#: Directories whose contents are vendored or generated. An agent excludes them
#: by reflex, and leaving them in rewards the baseline for noise.
_EXCLUDE = (":(exclude)*vendor/*", ":(exclude)*third_party/*",
            ":(exclude)*node_modules/*", ":(exclude)*.min.js")


@lru_cache(maxsize=200_000)
def concept_tokens(name: str) -> frozenset[str]:
    """Split a path or symbol into the words an agent would actually search for.

    ``ImmutableList.java`` becomes ``{immutable, list}``. This is what a ``.*``
    pattern buys: it reaches files that share a concept but no symbol and sit in
    another directory, or another language, where no reference edge exists.
    """
    stem = name.rsplit("/", 1)[-1].split(".", 1)[0]
    out = set()
    for word in re.split(r"[^A-Za-z0-9]+", stem):
        for part in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+", word):
            part = part.lower()
            if len(part) >= 4 and part not in _STOPWORDS:
                out.add(part)
    return frozenset(out)


def _git(mirror: str, args: list[str], timeout: int) -> str:
    """Stdout of a git command, or "" if it failed, timed out or git is absent.

    A baseline that cannot answer scores a miss. Letting one slow `git grep`
    raise would abandon a replay that has already scored hundreds of thousands
    of prompts, which is a far worse outcome than one unanswered prompt.
    """
    try:
        proc = subprocess.run(
            ["git", *args], cwd=mirror, env=_base_env(), capture_output=True,
            text=True, errors="replace", timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("baseline git %s failed in %s: %s", args[0], mirror, exc)
        return ""
    return proc.stdout if proc.returncode in (0, 1) else ""


#: Vendored, generated and minified paths. Excluded from *both* halves of the
#: search: an agent ignores them, and matching their names while refusing to
#: match their contents would score them on a rule the grep never applied.
_EXCLUDE_DIRS = ("vendor/", "third_party/", "node_modules/", "testdata/")


def _wanted(path: str) -> bool:
    return not (path.endswith(".min.js")
                or any(d in f"/{path}" for d in (f"/{x}" for x in _EXCLUDE_DIRS)))


@lru_cache(maxsize=32)
def _tree(mirror: str, sha: str) -> tuple[str, ...]:
    """Searchable paths in the tree at that commit; cached, as prompts share commits."""
    out = _git(mirror, ["ls-tree", "-r", "--name-only", sha], timeout=120)
    return tuple(p for p in out.splitlines() if _wanted(p))


def _grep_terms(mirror: str, sha: str, terms: list[str]) -> dict[str, set[str]]:
    """path -> which of `terms` its *contents* mention, in the tree at `sha`."""
    if not terms:
        return {}
    pattern = "|".join(re.escape(t) for t in terms)
    stdout = _git(mirror, ["grep", "-I", "-w", "-i", "-o", "-E", "-e", pattern,
                           sha, "--", *_EXCLUDE], timeout=180)
    prefix = sha + ":"
    out: dict[str, set[str]] = defaultdict(set)
    for line in stdout.splitlines():
        if not line.startswith(prefix):
            continue
        # "<sha>:<path>:<matched text>"
        path, _, matched = line[len(prefix):].rpartition(":")
        if path and _wanted(path):
            out[path].add(matched.lower())
    return out


def _blob(mirror: str, sha: str, path: str) -> str:
    """The file as it stood at that commit, or "" if it did not exist yet."""
    return _git(mirror, ["show", f"{sha}:{path}"], timeout=60)


#: How strongly each kind of evidence counts. A term in the file's *name* is the
#: strongest signal an agent has; a mention in the body is weaker; a mention
#: found only after widening the search is weaker still.
_W_NAME, _W_BODY, _W_WIDENED = 3.0, 2.0, 1.0


def agent_search(mirror: Path, sha: str, seed_path: str, k: int) -> list[str]:
    """What an agent's own search would have surfaced, at that commit.

    Models the loop rather than one query. Terms are derived from the seed's
    path *and* the symbols it declares, matched against both file names and
    file contents, and then widened with what the first round returned -- the
    read-a-result-and-search-again step that finds callers no naming or folder
    rule would suggest.

    Searched against the **parent** tree, so it sees the repository exactly as
    it stood before the change existed. Grepping the commit's own tree would
    let a file that was edited *by* this commit answer for it.

    Expensive -- several `git grep` passes per prompt -- so it is sampled.
    """
    parent = f"{sha}^"
    tree = _tree(str(mirror), parent)
    if not tree:
        return []          # root commit, or a sha the mirror no longer has

    path_tokens = concept_tokens(seed_path)
    symbols = {m for m in _DECL.findall(_blob(str(mirror), parent, seed_path))
               if len(m) >= 4}
    # Declared symbols first: they are the precise terms. Longer path tokens
    # next, because a long word is a more selective query than a short one.
    terms = (sorted(symbols, key=lambda t: (-len(t), t))[:8]
             + sorted(path_tokens, key=lambda t: (-len(t), t))[:4])
    if not terms:
        return []

    score: dict[str, float] = defaultdict(float)
    for path in tree:
        if path == seed_path:
            continue
        overlap = concept_tokens(path) & path_tokens
        if overlap:
            score[path] += _W_NAME * len(overlap)

    for path, matched in _grep_terms(str(mirror), parent, terms).items():
        if path != seed_path:
            score[path] += _W_BODY * len(matched)

    # Second round: widen using the names of what came back, the way an agent
    # follows a result it has just read.
    lead = sorted(score, key=lambda p: -score[p])[:3]
    widened = set().union(*(concept_tokens(p) for p in lead)) if lead else set()
    widened -= path_tokens
    if widened:
        for path, matched in _grep_terms(
                str(mirror), parent, sorted(widened, key=len, reverse=True)[:6]).items():
            if path != seed_path:
                score[path] += _W_WIDENED * len(matched)

    return sorted(score, key=lambda p: (-score[p], p))[:k]


def _path_index(repo_id: int | None) -> tuple[dict[int, str], dict[str, list[int]], dict[str, list[int]]]:
    """File paths, grouped by name stem and by directory, for the baselines."""
    where = "" if repo_id is None else "WHERE repo_id = %(repo)s"
    rows = query(f"SELECT id, path, dir_path FROM file {where}", {"repo": repo_id})
    paths, by_stem, by_dir = {}, defaultdict(list), defaultdict(list)
    for r in rows:
        paths[r["id"]] = r["path"]
        by_stem[stem_of(r["path"])].append(r["id"])
        by_dir[r["dir_path"] or ""].append(r["id"])
    return paths, by_stem, by_dir


def _rank(seed: int, joint: dict[int, dict[int, int]], marginal: dict[int, int], total: int, spec, k: int, min_support: int) -> list[int]:
    """The k partners this measure would have offered for `seed`."""
    partners = joint.get(seed)
    if not partners:
        return []
    ids = [p for p, n in partners.items() if n >= min_support]
    if not ids:
        return []

    n_ab = np.array([partners[p] for p in ids], dtype=np.float64)
    n_b = np.array([marginal[p] for p in ids], dtype=np.float64)
    n_a = np.full(len(ids), float(marginal[seed]))
    raw = spec.compute(Contingency.from_counts(n_ab, n_a, n_b, float(total)))
    scores = np.nan_to_num(np.asarray(raw, dtype=np.float64), nan=-np.inf, neginf=-np.inf)
    return [ids[i] for i in np.argsort(-scores)[:k]]


def _popular(marginal: dict[int, int], seed: int, k: int) -> list[int]:
    """Busiest files so far. The weakest baseline, kept only for comparison."""
    ranked = sorted(marginal.items(), key=lambda kv: -kv[1])
    return [f for f, _ in ranked if f != seed][:k]


#: Affixes that mark a file as the test, spec or header partner of another.
#: `auth.go`/`auth_test.go` and `Auth.java`/`AuthTest.java` share a stem.
_AFFIXES = ("_test", "test_", "_spec", "spec_", ".test", ".spec", "_impl", "-test", "-spec")


def stem_of(path: str) -> str:
    """The comparable stem of a path, with extension and test affixes removed."""
    name = path.rsplit("/", 1)[-1].lower()
    name = name.split(".", 1)[0]
    for affix in _AFFIXES:
        bare = affix.strip("._-")
        if name.startswith(bare) and len(name) > len(bare):
            name = name[len(bare):]
        if name.endswith(bare) and len(name) > len(bare):
            name = name[: -len(bare)]
    return name.strip("._-")


def _neighbours(seed: int, marginal: dict[int, int], k: int, paths: dict[int, str],
                by_stem: dict[str, list[int]], by_dir: dict[str, list[int]]) -> list[int]:
    """What an agent finds without any history: siblings, then the directory.

    A name sibling first -- the test beside the source, the header beside the
    implementation -- because that is the cheapest and most reliable guess
    anyone makes. Then the rest of the directory, busiest first.
    """
    seed_path = paths.get(seed)
    if seed_path is None:
        return []
    out: list[int] = []
    for fid in by_stem.get(stem_of(seed_path), ()):
        if fid != seed and fid in marginal:
            out.append(fid)
    directory = seed_path.rsplit("/", 1)[0] if "/" in seed_path else ""
    siblings = [f for f in by_dir.get(directory, ()) if f != seed and f in marginal]
    siblings.sort(key=lambda f: -marginal[f])
    for fid in siblings:
        if fid not in out:
            out.append(fid)
    return out[:k]


def _same_directory(seed: int, marginal: dict[int, int], k: int, paths: dict[int, str],
                    by_dir: dict[str, list[int]]) -> list[int]:
    """Everything in the same directory, busiest first."""
    seed_path = paths.get(seed)
    if seed_path is None:
        return []
    directory = seed_path.rsplit("/", 1)[0] if "/" in seed_path else ""
    out = [f for f in by_dir.get(directory, ()) if f != seed and f in marginal]
    out.sort(key=lambda f: -marginal[f])
    return out[:k]


#: How the file "you are editing" is chosen from a commit.
#:
#: ``all``     -- every changed file takes a turn as the seed. The product's own
#:               question, asked once per file, and the larger sample.
#: ``obscure`` -- one prompt per commit, seeded with the *least* changed file.
#:               Starting from a quiet corner rather than a hub that half the
#:               repository already moves with. Harder, and the case where a
#:               naming or folder rule has least to offer.
SEEDINGS = ("all", "obscure")


def _seeds(files: list[int], marginal: dict[int, int], seeding: str) -> list[int]:
    """Which files of this commit become prompts."""
    if seeding == "all":
        return files
    # Prior counts only -- this commit has not been trained on yet, so choosing
    # by them cannot leak. Tie-broken by id so a run is reproducible.
    return [min(files, key=lambda f: (marginal.get(f, 0), f))]


def run(repo_id: int | None = None, measures: tuple[str, ...] = (DEFAULT_MEASURE,), k: int = 5, min_support: int = 2, limit: int | None = None, grep_sample: int = 0, seeding: str = "all") -> BacktestResult:
    """Replay history and report how often each measure named the right files.

    Args:
        repo_id: restrict to one repository, or None for the whole corpus.
        measures: measure keys to evaluate side by side.
        k: how many suggestions the product is allowed to offer.
        min_support: ignore partners seen together fewer times than this.
        limit: stop after this many commits, for a quick look.

    Returns:
        A :class:`BacktestResult`, carrying each measure's lift over the
        popularity baseline and a confidence interval on its hit rate.
    """
    if seeding not in SEEDINGS:
        raise ValueError(f"unknown seeding {seeding!r}; expected one of {SEEDINGS}")
    specs = [resolve(m) for m in measures]
    paths, by_stem, by_dir = _path_index(repo_id)
    commits = _history(repo_id)
    if limit is not None:
        commits = commits[:limit]

    # Per repository, never pooled: see _history.
    joints: dict[int, dict[int, dict[int, int]]] = defaultdict(lambda: defaultdict(dict))
    marginals: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    totals: dict[int, int] = defaultdict(int)

    zero = {s.key: 0 for s in specs}
    hit_prompts, found = dict(zero), dict(zero)
    #: Scored only on prompts the neighbour baseline missed entirely.
    hard_hits = dict(zero)
    hard_prompts = 0
    recall = {s.key: 0.0 for s in specs}
    precision = {s.key: 0.0 for s in specs}
    rr = {s.key: 0.0 for s in specs}
    #: name -> (hit prompts, files found, summed recall)
    bases = {n: [0, 0, 0.0] for n in ("neighbours", "same directory", "popularity")}
    #: The grep baseline is sampled: one `git grep` per prompt is far too slow
    #: to run over every one, so it carries its own denominator.
    grep_hits, grep_found, grep_recall, grep_n, grep_wanted = 0, 0, 0.0, 0, 0
    #: A uniform sample of prompts, filled by reservoir sampling and searched
    #: once the replay is over. Sampling with a fixed probability and a cap
    #: stopped as soon as the cap was reached, which drew the whole sample from
    #: the oldest commits while every other measure was scored across all of
    #: history -- a comparison between different eras of the repository.
    reservoir: list[tuple] = []
    candidates = 0
    #: Prompts in that sample which neither the free rules nor the agent's own
    #: search solved, and how often each measure answered them anyway.
    unaided_hard, unaided_hits = 0, dict(zero)
    shas = _commit_shas(repo_id) if grep_sample else {}
    rng = random.Random(20260830)  # noqa: S311 - sampling, not secrets
    prompts, wanted, scored_commits = 0, 0, 0

    for repo, files, commit_id in commits:
        joint, marginal, total = joints[repo], marginals[repo], totals[repo]
        # ---- test, using only what earlier commits taught -------------------
        if total >= WARMUP_COMMITS and 2 <= len(files) <= MAX_FILES_PER_PROMPT:
            scored_here = False
            for seed in _seeds(files, marginal, seeding):
                targets = set(files) - {seed}
                if not targets:
                    continue
                prompts += 1
                wanted += len(targets)
                scored_here = True

                neighbour_guess = _neighbours(seed, marginal, k, paths, by_stem, by_dir)
                neighbour_solved = any(f in targets for f in neighbour_guess)
                if not neighbour_solved:
                    hard_prompts += 1

                hit_here: dict[str, bool] = {}
                for spec in specs:
                    got = _rank(seed, joint, marginal, total, spec, k, min_support)
                    correct = [f for f in got if f in targets]
                    hit_here[spec.key] = bool(correct)
                    if not neighbour_solved and correct:
                        hard_hits[spec.key] += 1
                    found[spec.key] += len(correct)
                    hit_prompts[spec.key] += 1 if correct else 0
                    recall[spec.key] += len(correct) / len(targets)
                    precision[spec.key] += len(correct) / k
                    rank = next((i for i, f in enumerate(got, 1) if f in targets), 0)
                    rr[spec.key] += 1.0 / rank if rank else 0.0

                if grep_sample:
                    sha, full_name, host = shas.get(commit_id, ("", "", ""))
                    seed_path = paths.get(seed)
                    if sha and seed_path:
                        entry = (full_name, host, sha, seed_path,
                                 frozenset(p for p in (paths.get(f) for f in targets) if p),
                                 len(targets), neighbour_solved, tuple(hit_here.items()))
                        candidates += 1
                        if len(reservoir) < grep_sample:
                            reservoir.append(entry)
                        else:                       # Algorithm R
                            j = rng.randrange(candidates)
                            if j < grep_sample:
                                reservoir[j] = entry

                for name, guess in (
                    ("neighbours", neighbour_guess),
                    ("same directory", _same_directory(seed, marginal, k, paths, by_dir)),
                    ("popularity", _popular(marginal, seed, k)),
                ):
                    correct = [f for f in guess if f in targets]
                    slot = bases[name]
                    slot[0] += 1 if correct else 0
                    slot[1] += len(correct)
                    slot[2] += len(correct) / len(targets)
            scored_commits += 1 if scored_here else 0

        # ---- then train -----------------------------------------------------
        for a, b in combinations(sorted(set(files)), 2):
            joint[a][b] = joint[a].get(b, 0) + 1
            joint[b][a] = joint[b].get(a, 0) + 1
        for f in set(files):
            marginal[f] += 1
        totals[repo] += 1

    # The sample is searched only now, so that every prompt in the replay had an
    # equal chance of being in it regardless of when it occurred.
    for full_name, host, sha, seed_path, want, n_targets, solved, hits in reservoir:
        got = agent_search(mirror_path_for(full_name, host=host), sha, seed_path, k)
        correct = [g for g in got if g in want]
        grep_n += 1
        grep_wanted += n_targets
        grep_hits += 1 if correct else 0
        grep_found += len(correct)
        grep_recall += len(correct) / n_targets
        if not solved and not correct:
            unaided_hard += 1
            for key, hit in hits:
                unaided_hits[key] += 1 if hit else 0

    n = prompts or 1
    # Each baseline is a person who could answer this question without any
    # history, nicknamed by how much of the codebase they have seen. Every rung
    # is free, so whatever Git Synapse adds on top of the highest one has to
    # have come from the commit log and nowhere else.
    labels = {"neighbours": "Apprentice -- the file's test, then its folder",
              "same directory": "Intern -- the file's folder-mates, busiest first",
              "popularity": "Tourist -- the repository's busiest files"}
    baselines = []
    for name, (hits, found_n, rec) in bases.items():
        low, high = wilson(hits, prompts)
        baselines.append(Score(
            measure=name, label=labels[name], prompts=prompts, hit_prompts=hits,
            found=found_n, wanted=wanted, hit_rate=hits / n, ci_low=low, ci_high=high,
            recall_at_k=rec / n, precision_at_k=found_n / (n * k), mrr=0.0))
    if grep_n:
        low, high = wilson(grep_hits, grep_n)
        baselines.append(Score(
            measure=SAMPLED_BASELINE,
            label=f"New Hire -- greps names and bodies, follows leads (n={grep_n:,})",
            prompts=grep_n, hit_prompts=grep_hits, found=grep_found, wanted=grep_wanted,
            hit_rate=grep_hits / grep_n, ci_low=low, ci_high=high,
            recall_at_k=grep_recall / grep_n, precision_at_k=grep_found / (grep_n * k), mrr=0.0))
    baselines.sort(key=lambda b: -b.hit_rate)
    # Same rule as BacktestResult.baseline: never divide by a sampled rate.
    baseline = next((b for b in baselines if b.measure != SAMPLED_BASELINE), baselines[0])

    scores = []
    for s in specs:
        low, high = wilson(hit_prompts[s.key], prompts)
        rate = hit_prompts[s.key] / n
        scores.append(Score(
            measure=s.key, label=s.label, prompts=prompts,
            hit_prompts=hit_prompts[s.key], found=found[s.key], wanted=wanted,
            hit_rate=rate, ci_low=low, ci_high=high,
            recall_at_k=recall[s.key] / n, precision_at_k=precision[s.key] / n,
            mrr=rr[s.key] / n,
            hard_hit_rate=hard_hits[s.key] / (hard_prompts or 1),
            hard_prompts=hard_prompts,
            unaided_hits=unaided_hits[s.key],
            unaided_prompts=unaided_hard,
            baseline_hit_rate=baseline.hit_rate,
            lift=rate / baseline.hit_rate if baseline.hit_rate else 0.0,
            rare_item_bias=s.rare_item_bias,
        ))
    scores.sort(key=lambda s: -s.hit_rate)

    result = BacktestResult(repo_id, k, min_support, len(commits), scored_commits,
                            prompts, baselines, scores, seeding)
    log.info("backtest: %d commits, %d prompts -- %s", len(commits), prompts, result.verdict)
    return result
