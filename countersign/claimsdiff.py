# audited on 20260903
"""What changed in the claims file, judged against a base revision.

The agent that wrote the code can also write the claims. The quiet way to
pass a gate is not to fix the code but to soften the claim: drop the test
claim, change ``exit 0`` to ``nonzero exit``, point the needle at a string
that is always there. This module makes that visible where a reviewer
looks: as a diff of claims between a base revision (the branch a pull
request targets) and the working tree, with every weakening named.

What counts as weakened, deterministically:

  removed              the claim is gone
  expect changed       the judgement rule changed
  needle changed       what the output must contain changed

A changed command is reported as changed and left to the reviewer: the
engine cannot know whether ``npm test`` became stricter or looser. A
changed statement or timeout is reported as wording.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .claims import Claim, ClaimsError, parse_claims

ADDED = "added"
REMOVED = "removed"
CHANGED = "changed"

WEAKENING_FIELDS = ("expect", "needle")


@dataclass(frozen=True)
class ClaimChange:
    claim_id: str
    kind: str
    fields: tuple[str, ...]
    weakened: bool
    detail: str


def claims_text_at(root: Path, ref: str, claims_file: str) -> bytes | None:
    """The claims file as it was at ``ref``; None when it did not exist there.

    Raises ClaimsError when git cannot answer at all (no repository, no such
    ref, git missing): a base that cannot be read must not pass as "no
    claims at base".
    """
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=str(root),
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaimsError(f"cannot read claims at {ref}: git is not available ({exc})") from None
    if top.returncode != 0:
        raise ClaimsError(f"cannot read claims at {ref}: {root} is not inside a git repository")
    toplevel = Path(top.stdout.strip()).resolve()
    target = (Path(root).resolve() / claims_file).resolve()
    if not target.is_relative_to(toplevel):
        raise ClaimsError(f"cannot read claims at {ref}: {target} is outside the repository {toplevel}")
    relative = target.relative_to(toplevel).as_posix()
    # ls-tree answers "does this path exist at that revision" with its exit
    # code and output alone, so no error message has to be parsed (git
    # localises its messages).
    listed = _git_bytes(toplevel, ref, "ls-tree", ref, "--", relative)
    if listed.returncode != 0:
        raise ClaimsError(f"cannot read claims at {ref}: {listed.stderr.decode('utf-8', errors='replace').strip() or 'not a valid revision'}")
    if not listed.stdout.strip():
        return None
    shown = _git_bytes(toplevel, ref, "show", f"{ref}:{relative}")
    if shown.returncode != 0:
        raise ClaimsError(f"cannot read claims at {ref}: {shown.stderr.decode('utf-8', errors='replace').strip() or 'git show failed'}")
    return shown.stdout


def _git_bytes(cwd: Path, ref: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaimsError(f"cannot read claims at {ref}: {exc}") from None


def diff_claims(base: list[Claim] | None, head: list[Claim] | None) -> list[ClaimChange]:
    """Changes from ``base`` to ``head``, ordered by claim id. None means the
    file did not exist on that side."""
    base_by_id = {c.claim_id: c for c in (base or [])}
    head_by_id = {c.claim_id: c for c in (head or [])}
    changes: list[ClaimChange] = []
    for claim_id in sorted(set(base_by_id) | set(head_by_id)):
        before = base_by_id.get(claim_id)
        after = head_by_id.get(claim_id)
        if before is None and after is not None:
            changes.append(ClaimChange(claim_id, ADDED, (), False, f"added: {after.statement}"))
            continue
        if before is not None and after is None:
            changes.append(ClaimChange(claim_id, REMOVED, (), True, f"removed: {before.statement}"))
            continue
        if before is None or after is None:
            continue  # unreachable: the id came from one of the two sides
        fields = tuple(
            name for name in ("statement", "command", "expect", "needle", "timeout_s")
            if getattr(before, name) != getattr(after, name)
        )
        if not fields:
            continue
        weakened = any(name in WEAKENING_FIELDS for name in fields)
        parts = []
        for name in fields:
            parts.append(f"{name}: {getattr(before, name)!r} to {getattr(after, name)!r}")
        changes.append(ClaimChange(claim_id, CHANGED, fields, weakened, "; ".join(parts)))
    return changes


def diff_against_ref(root: Path, ref: str, claims_file: str, head: list[Claim] | None) -> tuple[list[ClaimChange], str | None]:
    """Diff the working tree's claims against those at ``ref``.

    Returns (changes, base_problem). ``base_problem`` names a base claims
    file that exists but cannot be parsed; the diff then treats the base
    as empty so that every head claim shows as added, and the problem is
    reported next to it rather than hidden.
    """
    text = claims_text_at(root, ref, claims_file)
    if text is None:
        return diff_claims(None, head), None
    try:
        base = parse_claims(text, f"{claims_file} at {ref}")
    except ClaimsError as exc:
        return diff_claims(None, head), str(exc)
    return diff_claims(base, head), None
