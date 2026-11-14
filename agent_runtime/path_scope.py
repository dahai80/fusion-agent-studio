# Issue #685: shared filesystem path-scoping primitive.
#
# A tool declares allowed path patterns (absolute roots or fnmatch globs,
# `**` crosses directory separators); the scope resolves + normalizes each
# candidate and rejects anything outside scope with a typed error.
#
# Semantics (documented contract):
#   - deny-by-default: an empty allowlist rejects every path
#   - `..` traversal is rejected BEFORE resolution — a candidate that
#     escapes its declared root never gets symlink-resolved into luck
#   - symlink resolution: candidates are resolved with
#     Path.resolve(strict=False), so a symlink pointing outside the
#     allowlist is caught when the target exists; a symlink chain whose
#     target does not exist yet is judged on its normalized literal path
#     (documented limit — creation-then-swap races are out of scope for
#     a pure path filter; sandboxing is the fusion-guard layer's job)
#   - absolute roots match themselves and everything beneath them; globs
#     are matched against the normalized path string
#   - "~" in a pattern expands to the user home before matching

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)


class PathScopeError(ValueError):
    """Raised when a candidate path is outside the declared allowlist."""

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"path out of scope ({reason}): {path}")


def _normalize(candidate: str) -> Path:
    if not candidate or not str(candidate).strip():
        raise PathScopeError(str(candidate), "empty path")
    p = Path(os.path.expanduser(str(candidate)))
    if not p.is_absolute():
        raise PathScopeError(str(candidate), "relative path — absolute required")
    # Reject `..` traversal on the literal path before any resolution.
    if ".." in p.parts:
        raise PathScopeError(str(candidate), "parent traversal '..' rejected")
    return Path(os.path.normpath(str(p)))


def _glob_regex(pattern: str) -> re.Pattern:
    # Glob -> regex: `**` crosses directory separators, `*`/`?` do not,
    # everything else is literal. Patterns are absolute by contract.
    pat = os.path.expanduser(pattern)
    if not os.path.isabs(pat):
        raise PathScopeError(pattern, "scope pattern must be absolute")
    out = []
    i = 0
    while i < len(pat):
        ch = pat[i]
        if ch == "*":
            if pat[i : i + 2] == "**":
                i += 2
                if i < len(pat) and pat[i] == "/":
                    # `a/**/b` matches `a/b` (zero depth) and any depth
                    out.append("(?:.*/)?")
                    i += 1
                else:
                    out.append(".*")
                continue
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return re.compile("^" + "".join(out) + "$")


def _glob_match(normalized: str, pattern: str, _cache: dict = {}) -> bool:
    # Per-call _cache would leak across scopes; the compiled cache is keyed
    # by pattern string which is safe (same pattern -> same regex).
    rx = _cache.get(pattern)
    if rx is None:
        rx = _glob_regex(pattern)
        _cache[pattern] = rx
    return rx.match(normalized) is not None


class PathScope:
    def __init__(self, allowed: list[str], label: str = ""):
        self.allowed = [str(a) for a in (allowed or []) if str(a).strip()]
        self.label = label or "path-scope"

    def check(self, candidate: str) -> Path:
        # Resolve + validate; returns the normalized absolute path or raises
        # PathScopeError. Deny-by-default: no patterns -> always rejected.
        normalized = _normalize(candidate)
        if not self.allowed:
            raise PathScopeError(str(candidate), "deny-by-default: empty allowlist")
        resolved_str = str(normalized)
        try:
            resolved_str = str(normalized.resolve(strict=False))
        except OSError:
            resolved_str = str(normalized)
        for pattern in self.allowed:
            pat = os.path.expanduser(pattern)
            has_glob = any(c in pat for c in "*?[")
            # Matching uses ONLY the symlink-resolved path: a link pointing
            # outside the allowlist must not pass on its literal name.
            if has_glob:
                if _glob_match(resolved_str, pattern):
                    logger.debug(
                        "%s allowed %s via pattern %s", self.label, resolved_str, pattern
                    )
                    return normalized
            else:
                # bare absolute root: the root itself + everything beneath
                root = os.path.normpath(pat)
                if resolved_str == root or resolved_str.startswith(root + os.sep):
                    logger.debug(
                        "%s allowed %s via root %s", self.label, resolved_str, root
                    )
                    return normalized
        raise PathScopeError(
            str(candidate), f"outside allowlist ({self.label}: {self.allowed})"
        )

    def check_many(self, candidates: list[str]) -> list[Path]:
        return [self.check(c) for c in candidates]
