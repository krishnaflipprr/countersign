"""The claims protocol: every completion claim must be falsifiable.

An agent (or a human) declares what is true about the work in claims.toml.
Each claim carries the command that would fail if the claim were false.
Countersign runs those commands and records the verdict. Nothing here
interprets prose or trusts a summary: a claim either survived its command
or it did not.

The three expectations a claim can declare:

  expect = "exit 0"          the command must succeed (default)
  expect = "nonzero exit"    the command must fail (negative tests)
  expect = "output contains" the needle must appear in combined output

This is the protocol that makes "done" a testable statement: if nobody can
say what command would disprove the claim, the claim was not a claim.
"""

from __future__ import annotations

import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PASS = "pass"
FAIL = "fail"
TIMEOUT = "timeout"

VALID_EXPECTATIONS = frozenset({"exit 0", "nonzero exit", "output contains"})


@dataclass(frozen=True)
class Claim:
    claim_id: str
    statement: str
    command: str
    expect: str = "exit 0"
    needle: str | None = None
    timeout_s: int | None = None


@dataclass
class ClaimResult:
    claim_id: str
    statement: str
    command: str
    expect: str
    status: str
    exit_code: int | None = None
    duration_ms: int = 0
    output_excerpt: str = ""


class ClaimsError(ValueError):
    """The claims file exists but cannot be honoured as written."""


def load_claims(path: Path | None) -> list[Claim] | None:
    """Parse claims.toml. None means no file (a reported skip, not silence)."""
    if path is None:
        return None
    with Path(path).open("rb") as handle:
        raw = tomllib.load(handle)
    claims: list[Claim] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw.get("claim", []), start=1):
        claim_id = str(entry.get("id", "")).strip()
        if not claim_id:
            raise ClaimsError(f"claim {index} has no id")
        if claim_id in seen:
            raise ClaimsError(f"claim id '{claim_id}' is declared twice")
        seen.add(claim_id)
        statement = str(entry.get("statement", "")).strip()
        if not statement:
            raise ClaimsError(f"claim '{claim_id}' has no statement")
        command = str(entry.get("command", "")).strip()
        if not command:
            raise ClaimsError(f"claim '{claim_id}' declares no command; a claim without a disproof command is not falsifiable")
        expect = str(entry.get("expect", "exit 0"))
        if expect not in VALID_EXPECTATIONS:
            raise ClaimsError(
                f"claim '{claim_id}' uses expect = '{expect}', which is not one of {sorted(VALID_EXPECTATIONS)}"
            )
        needle = entry.get("needle")
        if expect == "output contains" and not needle:
            raise ClaimsError(f"claim '{claim_id}' expects 'output contains' but declares no needle")
        claims.append(
            Claim(
                claim_id=claim_id,
                statement=statement,
                command=command,
                expect=expect,
                needle=str(needle) if needle is not None else None,
                timeout_s=int(entry["timeout_s"]) if "timeout_s" in entry else None,
            )
        )
    return claims


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... [{len(text) - limit} characters truncated] ...\n" + text[-half:]


def run_claim(claim: Claim, cwd: Path, default_timeout_s: int, max_output_bytes: int) -> ClaimResult:
    """Run one claim's command and judge it exactly as declared."""
    timeout_s = claim.timeout_s if claim.timeout_s is not None else default_timeout_s
    started = time.monotonic()
    try:
        completed = subprocess.run(
            claim.command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as expired:
        output = (expired.stdout or "") + (expired.stderr or "")
        return ClaimResult(
            claim_id=claim.claim_id,
            statement=claim.statement,
            command=claim.command,
            expect=claim.expect,
            status=TIMEOUT,
            duration_ms=int((time.monotonic() - started) * 1000),
            output_excerpt=_truncate(str(output), max_output_bytes),
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    combined = (completed.stdout or "") + (completed.stderr or "")
    excerpt = _truncate(combined, max_output_bytes)

    if claim.expect == "exit 0":
        status = PASS if completed.returncode == 0 else FAIL
    elif claim.expect == "nonzero exit":
        status = PASS if completed.returncode != 0 else FAIL
    else:
        status = PASS if (claim.needle or "") in combined else FAIL

    return ClaimResult(
        claim_id=claim.claim_id,
        statement=claim.statement,
        command=claim.command,
        expect=claim.expect,
        status=status,
        exit_code=completed.returncode,
        duration_ms=duration_ms,
        output_excerpt=excerpt,
    )


def run_claims(claims: list[Claim] | None, cwd: Path, default_timeout_s: int, max_output_bytes: int) -> list[ClaimResult] | None:
    if claims is None:
        return None
    return [run_claim(claim, cwd, default_timeout_s, max_output_bytes) for claim in claims]
