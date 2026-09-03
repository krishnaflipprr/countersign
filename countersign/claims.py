# audited on 20260903
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

Commands run through the shell in the repository root, in their own process
group. A claim that times out is killed together with everything it
spawned, so a hung test runner cannot outlive the verdict that recorded it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PASS = "pass"
FAIL = "fail"
TIMEOUT = "timeout"

VALID_EXPECTATIONS = frozenset({"exit 0", "nonzero exit", "output contains"})

# How long to wait for a killed command's pipes to drain before giving up
# on collecting its output. A grandchild that escaped its process group can
# hold the pipe open; the verdict must not hang on it.
_DRAIN_AFTER_KILL_S = 5


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
    try:
        with Path(path).open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ClaimsError(f"{Path(path).name} is not valid TOML: {exc}") from None
    declared: Any = raw.get("claim", [])
    if not isinstance(declared, list) or not all(isinstance(entry, dict) for entry in declared):
        raise ClaimsError("claims must be declared as an array of tables: one [[claim]] block per claim")
    claims: list[Claim] = []
    seen: set[str] = set()
    for index, entry in enumerate(declared, start=1):
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
        timeout_s: int | None = None
        if "timeout_s" in entry:
            raw_timeout = entry["timeout_s"]
            if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, int) or raw_timeout < 1:
                raise ClaimsError(f"claim '{claim_id}' has timeout_s = {raw_timeout!r}; it must be a whole number of seconds, at least 1")
            timeout_s = raw_timeout
        claims.append(
            Claim(
                claim_id=claim_id,
                statement=statement,
                command=command,
                expect=expect,
                needle=str(needle) if needle is not None else None,
                timeout_s=timeout_s,
            )
        )
    return claims


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + f"\n... [{len(text) - limit} characters truncated] ...\n" + text[-half:]


def _decode(data: bytes | None) -> str:
    """Command output as text, whatever bytes the command produced."""
    if not data:
        return ""
    return data.decode("utf-8", errors="replace")


def _kill_tree(process: subprocess.Popen) -> None:
    """Kill the command and everything it started."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True, check=False, timeout=15,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        process.kill()
    except OSError:
        pass


def run_claim(claim: Claim, cwd: Path, default_timeout_s: int, max_output_bytes: int) -> ClaimResult:
    """Run one claim's command and judge it exactly as declared."""
    timeout_s = claim.timeout_s if claim.timeout_s is not None else default_timeout_s
    started = time.monotonic()
    isolation: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    process = subprocess.Popen(
        claim.command,
        shell=True,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **isolation,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=_DRAIN_AFTER_KILL_S)
        except subprocess.TimeoutExpired:
            stdout, stderr = b"", b""
        return ClaimResult(
            claim_id=claim.claim_id,
            statement=claim.statement,
            command=claim.command,
            expect=claim.expect,
            status=TIMEOUT,
            duration_ms=int((time.monotonic() - started) * 1000),
            output_excerpt=_truncate(_decode(stdout) + _decode(stderr), max_output_bytes),
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    combined = _decode(stdout) + _decode(stderr)
    excerpt = _truncate(combined, max_output_bytes)

    if claim.expect == "exit 0":
        status = PASS if process.returncode == 0 else FAIL
    elif claim.expect == "nonzero exit":
        status = PASS if process.returncode != 0 else FAIL
    else:
        status = PASS if (claim.needle or "") in combined else FAIL

    return ClaimResult(
        claim_id=claim.claim_id,
        statement=claim.statement,
        command=claim.command,
        expect=claim.expect,
        status=status,
        exit_code=process.returncode,
        duration_ms=duration_ms,
        output_excerpt=excerpt,
    )
