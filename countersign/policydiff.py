# audited on 20260903
"""What changed in the gate policy, judged against a base revision.

The policy is the part of countersign.toml a pull request must not be
able to rewrite on its own: what is scanned, which claims are required,
whether weakenings fail. On a pull request the engine enforces the base
branch's policy and reports every change the pull request makes to it. A
change that narrows what is checked is a weakening and fails the gate
unless a maintainer approved it (the approval label, passed as --approved).

What counts as weakened, deterministically:

  paths               a path the base scanned is gone
  extensions          an extension the base scanned is gone
  ignore_dirs         a directory is newly ignored
  exempt_marker       the marker changed (what suppresses findings changed)
  exclude_tests       tests were scanned and now are not
  allow_empty         an empty scan is newly allowed
  claims_file         the claims file was removed or moved
  required_claims     a required claim is no longer required
  fail_on_weakened    weakenings no longer fail
  claims_optional     a missing claims file no longer fails
  command_change      command changes no longer fail
  receipt_dir         receipts go somewhere else

A change that widens what is checked (a new path, a new required claim) is
reported and not a weakening. Timeouts and output limits are operational
and reported as changed only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import POLICY_FIELDS, Config

ADDED = "added"
REMOVED = "removed"
CHANGED = "changed"


@dataclass(frozen=True)
class PolicyChange:
    field: str
    kind: str
    weakened: bool
    detail: str


def _set(value: Any) -> set[str]:
    return set(value) if isinstance(value, (set, frozenset, list, tuple)) else set()


def diff_policy(base: Config, head: Config) -> list[PolicyChange]:
    """Changes from the base policy to the head policy, ordered by field."""
    changes: list[PolicyChange] = []
    base_policy, head_policy = base.policy(), head.policy()
    for name in POLICY_FIELDS:
        before, after = base_policy[name], head_policy[name]
        if before == after:
            continue
        weakened = False
        detail = f"{name}: {before!r} to {after!r}"
        if name in ("paths", "extensions", "required_claims"):
            gone = sorted(_set(before) - _set(after))
            new = sorted(_set(after) - _set(before))
            weakened = bool(gone)
            parts = []
            if gone:
                parts.append(f"no longer {'required' if name == 'required_claims' else 'scanned'}: {', '.join(gone)}")
            if new:
                parts.append(f"newly {'required' if name == 'required_claims' else 'scanned'}: {', '.join(new)}")
            detail = f"{name}: " + "; ".join(parts)
        elif name == "ignore_dirs":
            new = sorted(_set(after) - _set(before))
            gone = sorted(_set(before) - _set(after))
            weakened = bool(new)
            parts = []
            if new:
                parts.append(f"newly ignored: {', '.join(new)}")
            if gone:
                parts.append(f"no longer ignored: {', '.join(gone)}")
            detail = f"{name}: " + "; ".join(parts)
        elif name == "exempt_marker":
            weakened = True
        elif name == "exclude_tests":
            weakened = after is True
        elif name == "allow_empty":
            weakened = after is True
        elif name == "claims_file":
            weakened = before is not None
        elif name == "fail_on_weakened":
            weakened = after is False
        elif name == "claims_optional":
            weakened = after is True
        elif name == "command_change":
            weakened = after == "note"
        elif name == "receipt_dir":
            weakened = True
        changes.append(PolicyChange(name, CHANGED, weakened, detail))
    return changes
