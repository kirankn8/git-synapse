"""Does this actually help? Answered against history itself."""

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

from git_synapse.db.orm import models, session_scope
from git_synapse.ingest.gitops import _base_env, mirror_path_for
from git_synapse.stats.contingency import Contingency
from git_synapse.stats.registry import DEFAULT_MEASURE, resolve

log = logging.getLogger(__name__)

WARMUP_COMMITS = 20

MAX_FILES_PER_PROMPT = 25

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
    hard_hit_rate: float = 0.0
    hard_prompts: int = 0
    unaided_hits: int = 0
    unaided_prompts: int = 0
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
        """True only when the interval clears the baseline, not merely the point."""
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
        """The hardest baseline lift is measured against."""
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
    """commit id -> (sha, repository full name, host), for the grep baseline."""
    Commit, Repo = models().Commit, models().Repo
    with session_scope() as session:
        query = session.query(Commit, Repo).join(Repo, Repo.id == Commit.repo_id)
        if repo_id is not None:
            query = query.filter(Commit.repo_id == repo_id)
        rows = query.all()
    return {commit.id: (commit.sha, repo.full_name, repo.host) for commit, repo in rows}


def _history(repo_id: int | None) -> list[tuple[int, list[int], int]]:
    """Pair-eligible commits in time order, as ``(repo_id, file ids)``."""
    Commit, Change = models().Commit, models().CommitFile
    with session_scope() as session:
        query = session.query(Commit.id, Commit.repo_id, Change.file_id).join(
            Change, Change.commit_id == Commit.id
        ).filter(Commit.pair_eligible.is_(True))
        if repo_id is not None:
            query = query.filter(Commit.repo_id == repo_id)
        rows = query.order_by(Commit.committed_at, Commit.id, Change.file_id).all()
    grouped: dict[int, list[int]] = defaultdict(list)
    owner: dict[int, int] = {}
    for row in rows:
        commit_id, repo_id_value, file_id = row
        grouped[commit_id].append(file_id)
        owner[commit_id] = repo_id_value
    return [(owner[cid], files, cid) for cid, files in grouped.items()]


_STOPWORDS = frozenset((
    "test", "tests", "spec", "specs", "main", "index", "util", "utils",
    "common", "base", "core", "impl", "internal", "public", "private",
    "config", "types", "type", "data", "file", "files", "code", "java",
    "class", "interface", "string", "value", "result", "error", "context",
))

_DECL = re.compile(
    r"\b(?:class|interface|struct|enum|trait|type|func|def|fn|module|"
    r"package|function)\s+([A-Za-z_][A-Za-z0-9_]{3,})")

_EXCLUDE = (":(exclude)*vendor/*", ":(exclude)*third_party/*",
            ":(exclude)*node_modules/*", ":(exclude)*.min.js")


@lru_cache(maxsize=200_000)
def concept_tokens(name: str) -> frozenset[str]:
    """Split a path or symbol into the words an agent would actually search for."""
    stem = name.rsplit("/", 1)[-1].split(".", 1)[0]
    out = set()
    for word in re.split(r"[^A-Za-z0-9]+", stem):
        for part in re.findall(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+", word):
            part = part.lower()
            if len(part) >= 4 and part not in _STOPWORDS:
                out.add(part)
    return frozenset(out)


def _git(mirror: str, args: list[str], timeout: int) -> str:
    """Stdout of a git command, or "" if it failed, timed out or git is absent."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=mirror, env=_base_env(), capture_output=True,
            text=True, errors="replace", timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        log.debug("baseline git %s failed in %s: %s", args[0], mirror, exc)
        return ""
    return proc.stdout if proc.returncode in (0, 1) else ""


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


_W_NAME, _W_BODY, _W_WIDENED = 3.0, 2.0, 1.0


def agent_search(mirror: Path, sha: str, seed_path: str, k: int) -> list[str]:
    """What an agent's own search would have surfaced, at that commit."""
    parent = f"{sha}^"
    tree = _tree(str(mirror), parent)
    if not tree:
        return []          # root commit, or a sha the mirror no longer has

    path_tokens = concept_tokens(seed_path)
    symbols = {m for m in _DECL.findall(_blob(str(mirror), parent, seed_path))
               if len(m) >= 4}
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
    File = models().File
    with session_scope() as session:
        query = session.query(File)
        if repo_id is not None:
            query = query.filter_by(repo_id=repo_id)
        rows = query.all()
    paths, by_stem, by_dir = {}, defaultdict(list), defaultdict(list)
    for r in rows:
        paths[r.id] = r.path
        by_stem[stem_of(r.path)].append(r.id)
        by_dir[r.dir_path or ""].append(r.id)
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
    """What an agent finds without any history: siblings, then the directory."""
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


SEEDINGS = ("all", "obscure")


def _seeds(files: list[int], marginal: dict[int, int], seeding: str) -> list[int]:
    """Which files of this commit become prompts."""
    if seeding == "all":
        return files
    return [min(files, key=lambda f: (marginal.get(f, 0), f))]


def run(repo_id: int | None = None, measures: tuple[str, ...] = (DEFAULT_MEASURE,), k: int = 5, min_support: int = 2, limit: int | None = None, grep_sample: int = 0, seeding: str = "all") -> BacktestResult:
    """Replay history and report how often each measure named the right files."""
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
    grep_hits, grep_found, grep_recall, grep_n, grep_wanted = 0, 0, 0.0, 0, 0
    reservoir: list[tuple] = []
    candidates = 0
    unaided_hard, unaided_hits = 0, dict(zero)
    shas = _commit_shas(repo_id) if grep_sample else {}
    rng = random.Random(20260830)  # noqa: S311 - sampling, not secrets
    prompts, wanted, scored_commits = 0, 0, 0

    for repo, files, commit_id in commits:
        joint, marginal, total = joints[repo], marginals[repo], totals[repo]
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

        for a, b in combinations(sorted(set(files)), 2):
            joint[a][b] = joint[a].get(b, 0) + 1
            joint[b][a] = joint[b].get(a, 0) + 1
        for f in set(files):
            marginal[f] += 1
        totals[repo] += 1

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
