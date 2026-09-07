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

  command changed      the proof changed (policy default: a person approves it)

The engine does not try to read shell well enough to decide whether
``pytest || true`` still tests anything; a changed command is a weakening
by default (``[claims] command_change = "fail"``) and a maintainer's
approval turns it into a note. With ``command_change = "note"`` only a
command that provably cannot fail (``true``, ``:``, a bare ``echo``) counts,
and the engine says why. A changed statement or timeout is wording.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .claims import Claim, ClaimsError, fingerprint_path, parse_claims

ADDED = "added"
REMOVED = "removed"
CHANGED = "changed"

WEAKENING_FIELDS = ("expect", "needle")

# Commands that cannot fail, and so cannot disprove anything. Swapping a
# real command for one of these is the cheapest way past the gate: cheaper
# than deleting the claim, which is already caught as a removal. The claim
# survives the diff looking untouched while checking nothing at all.
#
# Only exact, unambiguous no-ops are listed, case-sensitively: `TRUE` is
# not a command on Linux and fails, so it is not a no-op. A command the
# engine cannot read confidently stays a plain `changed` for the reviewer,
# because the engine genuinely cannot know whether `npm test` became stricter.
_NO_OP_COMMANDS = frozenset({
    "true", ":", "/bin/true", "/usr/bin/true",
    "exit 0", "return 0",
})
# A bare `echo ...` (no pipe, no redirect, no chaining) always exits 0.
_ECHO_ONLY = re.compile(r"^(?:/bin/)?echo\b[^|&;<>()`$]*$")


def is_no_op_command(command: str) -> bool:
    """True when the command always succeeds and so tests nothing."""
    stripped = command.strip().rstrip(";").strip()
    if stripped in _NO_OP_COMMANDS:
        return True
    return bool(_ECHO_ONLY.match(stripped))


@dataclass(frozen=True)
class ClaimChange:
    claim_id: str
    kind: str
    fields: tuple[str, ...]
    weakened: bool
    detail: str


def claims_text_at(root: Path, ref: str, claims_file: str) -> bytes | None:
    return file_text_at(root, ref, claims_file, "claims")


def file_text_at(root: Path, ref: str, relative_file: str, what: str = "file") -> bytes | None:
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
        raise ClaimsError(f"cannot read {what} at {ref}: git is not available ({exc})") from None
    if top.returncode != 0:
        raise ClaimsError(f"cannot read {what} at {ref}: {root} is not inside a git repository")
    toplevel = Path(top.stdout.strip()).resolve()
    target = (Path(root).resolve() / relative_file).resolve()
    if not target.is_relative_to(toplevel):
        raise ClaimsError(f"cannot read {what} at {ref}: {target} is outside the repository {toplevel}")
    relative = target.relative_to(toplevel).as_posix()
    # ls-tree answers "does this path exist at that revision" with its exit
    # code and output alone, so no error message has to be parsed (git
    # localises its messages).
    listed = _git_bytes(toplevel, ref, "ls-tree", ref, "--", relative)
    if listed.returncode != 0:
        raise ClaimsError(f"cannot read {what} at {ref}: {listed.stderr.decode('utf-8', errors='replace').strip() or 'not a valid revision'}")
    if not listed.stdout.strip():
        return None
    shown = _git_bytes(toplevel, ref, "show", f"{ref}:{relative}")
    if shown.returncode != 0:
        raise ClaimsError(f"cannot read {what} at {ref}: {shown.stderr.decode('utf-8', errors='replace').strip() or 'git show failed'}")
    return shown.stdout


def _git_bytes(cwd: Path, ref: str, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClaimsError(f"cannot read {ref}: {exc}") from None


def diff_claims(base: list[Claim] | None, head: list[Claim] | None, *, command_change_weakens: bool = True) -> list[ClaimChange]:
    """Changes from ``base`` to ``head``, ordered by claim id. None means the
    file did not exist on that side.

    ``command_change_weakens`` is the policy default: a changed command is a
    weakening, because the engine cannot read shell well enough to know
    whether ``pytest || true`` still tests anything, and a changed proof
    needs a person to look at it. With it off, only a command that provably
    cannot fail counts."""
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
            name for name in ("statement", "command", "expect", "needle", "timeout_s", "inputs")
            if getattr(before, name) != getattr(after, name)
        )
        if not fields:
            continue
        weakened = any(name in WEAKENING_FIELDS for name in fields)
        # Dropping a declared input takes a file out of the proof: weakening.
        # Adding one widens it.
        if "inputs" in fields and set(before.inputs) - set(after.inputs):
            weakened = True
        # A changed command is a changed proof. By default that is a
        # weakening for a person to approve; the engine does not try to
        # read shell well enough to say otherwise. The no-op check below is
        # an explanation, never the decision.
        if "command" in fields and command_change_weakens:
            weakened = True
        neutered = (
            "command" in fields
            and not is_no_op_command(before.command)
            and is_no_op_command(after.command)
        )
        if neutered:
            weakened = True
        parts = []
        for name in fields:
            parts.append(f"{name}: {getattr(before, name)!r} to {getattr(after, name)!r}")
        if neutered:
            parts.append("the new command always succeeds, so the claim can no longer fail")
        changes.append(ClaimChange(claim_id, CHANGED, fields, weakened, "; ".join(parts)))
    return changes


def input_content_changes(root: Path, ref: str, claims: list[Claim]) -> list[ClaimChange]:
    """One change per claim whose declared inputs differ between ``ref`` and
    the working tree. The command may be word for word the same; if the test
    script it runs was rewritten, the proof changed, and that is a weakening."""
    changes: list[ClaimChange] = []
    for claim in claims:
        if not claim.inputs:
            continue
        differing: list[str] = []
        for relative in claim.inputs:
            now = fingerprint_path(root, relative)
            base = _fingerprint_at(root, ref, relative)
            if now != base:
                differing.append(f"{relative} ({'absent' if base == 'absent' else base[:12]} to {'absent' if now == 'absent' else now[:12]})")
        if differing:
            changes.append(ClaimChange(claim.claim_id, CHANGED, ("inputs",), True, "input changed: " + "; ".join(differing) + "; the command is unchanged but what it runs is not"))
    return changes


def _fingerprint_at(root: Path, ref: str, relative: str) -> str:
    """The fingerprint a path had at ``ref``: the same function as the working
    tree's, computed over the committed bytes, so equal content fingerprints equal."""
    toplevel = _toplevel(root, ref)
    target = (Path(root).resolve() / relative).resolve()
    if not target.is_relative_to(toplevel):
        raise ClaimsError(f"cannot read input at {ref}: {relative} is outside the repository {toplevel}")
    repo_relative = target.relative_to(toplevel).as_posix()
    kind = _git_bytes(toplevel, ref, "cat-file", "-t", f"{ref}:{repo_relative}")
    if kind.returncode != 0:
        return "absent"
    if kind.stdout.strip() == b"blob":
        shown = _git_bytes(toplevel, ref, "show", f"{ref}:{repo_relative}")
        if shown.returncode != 0:
            raise ClaimsError(f"cannot read input at {ref}: git show failed for {repo_relative}")
        return hashlib.sha256(shown.stdout).hexdigest()
    listed = _git_bytes(toplevel, ref, "ls-tree", "-r", "--name-only", ref, "--", repo_relative)
    names = [n for n in listed.stdout.decode("utf-8", errors="replace").splitlines() if n.strip()]
    if listed.returncode != 0:
        return "absent"
    digest = hashlib.sha256()
    prefix = repo_relative.rstrip("/") + "/"
    for name in sorted(names):
        shown = _git_bytes(toplevel, ref, "show", f"{ref}:{name}")
        digest.update(name[len(prefix):].encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(shown.stdout if shown.returncode == 0 else b"").digest())
    return digest.hexdigest()


def _toplevel(root: Path, ref: str) -> Path:
    top = _git_bytes(Path(root), ref, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise ClaimsError(f"cannot read {ref}: {root} is not inside a git repository")
    return Path(top.stdout.decode("utf-8", errors="replace").strip()).resolve()


def merge_changes(changes: list[ClaimChange], extra: list[ClaimChange]) -> list[ClaimChange]:
    """Fold input changes into the field diff, one entry per claim."""
    by_id = {c.claim_id: c for c in changes}
    for change in extra:
        existing = by_id.get(change.claim_id)
        if existing is None or existing.kind != CHANGED:
            if existing is None:
                by_id[change.claim_id] = change
            continue
        by_id[change.claim_id] = ClaimChange(
            change.claim_id, CHANGED, tuple(dict.fromkeys(existing.fields + change.fields)), existing.weakened or change.weakened, existing.detail + "; " + change.detail,
        )
    return [by_id[k] for k in sorted(by_id)]


def diff_against_ref(root: Path, ref: str, claims_file: str, head: list[Claim] | None, *, command_change_weakens: bool = True) -> tuple[list[ClaimChange], str | None]:
    """Diff the working tree's claims against those at ``ref``.

    Returns (changes, base_problem). ``base_problem`` names a base claims
    file that exists but cannot be parsed; the diff then treats the base
    as empty so that every head claim shows as added, and the problem is
    reported next to it rather than hidden.
    """
    text = claims_text_at(root, ref, claims_file)
    if text is None:
        return diff_claims(None, head, command_change_weakens=command_change_weakens), None
    try:
        base = parse_claims(text, f"{claims_file} at {ref}")
    except ClaimsError as exc:
        return diff_claims(None, head, command_change_weakens=command_change_weakens), str(exc)
    return diff_claims(base, head, command_change_weakens=command_change_weakens), None
