"""Paths that describe a build rather than a relationship.

Manifests and lockfiles state which version was wanted or installed; generated and
vendored trees change whenever their generator does. Both mislead a ranking that is
supposed to be about design coupling, so the same rule applies wherever a file list
is ranked: coupling, hotspots and cross-repository pairs.
"""

from __future__ import annotations

import re

from git_synapse.analysis.manifests import LOCK_FILES, MANIFEST_FILES

_GENERATED = re.compile(
    r"(^|/)(vendor|node_modules|third_party|dist|generated|gen|__pycache__)/"
    r"|(_gen|\.pb|_auto_test)\.go$|\.min\.(js|css)$"
)

#: Release tooling rewrites these on every release, so they track cadence, not design.
_RELEASE = re.compile(
    r"(^|/)(CHANGELOG|CHANGES|HISTORY|VERSION|version|version\.txt)(\.(md|txt))?$"
    r"|(^|/)\.release-please-manifest\.json$"
    r"|(^|/)(release-please-config|\.releaserc|\.versionrc|lerna)\.(json|js|ya?ml|cjs)$"
    r"|(^|/)\.changeset/"
)

_NOISE_NAMES = frozenset(MANIFEST_FILES) | frozenset(LOCK_FILES)


def is_noise(path: str) -> bool:
    """True when the path says nothing about how the code is put together."""
    base = (path or "").rsplit("/", 1)[-1]
    return (base in _NOISE_NAMES or bool(_GENERATED.search(path or ""))
            or bool(_RELEASE.search(path or "")))
