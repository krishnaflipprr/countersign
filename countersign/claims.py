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

import difflib
import os
import re
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
# A claim the repository's config requires but the claims file does not
# declare. It fails the gate: a required claim that nobody wrote is the
# quietest way to weaken a repository's own standard.
MISSING = "missing"
NOT_PASSED = frozenset({FAIL, TIMEOUT, MISSING})

VALID_EXPECTATIONS = frozenset({"exit 0", "nonzero exit", "output contains"})

# Every key a [[claim]] block may carry. A key outside this set is refused
# rather than ignored: a misspelled `expct` or `neddle` would silently drop
# the claim back to its default judgement (`exit 0`), and the gate would
# then report a pass for a claim nobody is actually checking. A claims file
# that cannot be honoured exactly as written is a usage error, not a pass.
KNOWN_CLAIM_KEYS = frozenset({"id", "statement", "command", "expect", "needle", "timeout_s"})

# Top-level keys the claims file may carry. Only the claim array today;
# named here so a stray `[[claims]]` or `[claim]` typo is caught at the door
# instead of parsing as zero claims and passing the gate in silence.
KNOWN_CLAIMS_FILE_KEYS = frozenset({"claim"})

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
    redactions: int = 0


# Values that must not end up on a receipt. Command output is kept as
# evidence, and evidence that contains a credential is a leak: the receipt
# is uploaded as an artifact and, with the App, kept outside the repository.
# Each pattern is a well-known credential shape or a labelled secret; the
# value is replaced, the label stays so the reader knows what was there.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-(?:[A-Za-z0-9_-]{2,}-)?[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic|token)\s+)\S+"),
    re.compile(r"(?i)\b((?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|password|passwd|secret|token)\s*[=:]\s*['\"]?)[^\s'\"\[]{8,}"),
)
REDACTED = "[redacted]"


def redact_secrets(text: str) -> tuple[str, int]:
    """Replace credential-shaped values in ``text``. Returns the text and how
    many values were replaced, so the receipt can say so."""
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        prefix = match.group(1) if match.lastindex else ""
        return f"{prefix}{REDACTED}"

    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(replace, text)
    return text, count


class ClaimsError(ValueError):
    """The claims file exists but cannot be honoured as written."""


def _did_you_mean(unknown: str, known: frozenset[str]) -> str:
    """A ' (did you mean X?)' suffix when one known key is close enough.

    Deterministic: difflib's ratio on a fixed cutoff over a sorted list, so
    the same typo always produces the same message on every machine.
    """
    close = difflib.get_close_matches(unknown, sorted(known), n=1, cutoff=0.6)
    return f" (did you mean '{close[0]}'?)" if close else ""


def _refuse_unknown_keys(entry: dict[str, Any], known: frozenset[str], where: str) -> None:
    """Raise on any key outside ``known``, naming all of them at once."""
    unknown = sorted(set(entry) - known)
    if not unknown:
        return
    listed = ", ".join(f"'{key}'{_did_you_mean(key, known)}" for key in unknown)
    raise ClaimsError(
        f"{where} declares {listed}, which Countersign does not understand. "
        f"Known keys are {sorted(known)}. An unrecognised key is refused rather "
        "than ignored, because ignoring it would quietly change what the claim checks."
    )


def load_claims(path: Path | None) -> list[Claim] | None:
    """Parse claims.toml. None means no file (a reported skip, not silence)."""
    if path is None:
        return None
    with Path(path).open("rb") as handle:
        data = handle.read()
    return parse_claims(data, Path(path).name)


def parse_claims(data: bytes | str, source_name: str = "claims.toml") -> list[Claim]:
    """Parse the text of a claims file. Raises ClaimsError for anything that
    cannot be honoured as written."""
    if isinstance(data, bytes):
        try:
            # TOML is UTF-8 by definition; a byte order mark is tolerated,
            # anything undecodable is refused rather than silently repaired,
            # because a repaired byte inside a command would change what runs.
            data = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ClaimsError(f"{source_name} is not valid UTF-8: {exc}") from None
    try:
        raw = tomllib.loads(data)
    except tomllib.TOMLDecodeError as exc:
        raise ClaimsError(f"{source_name} is not valid TOML: {exc}") from None
    _refuse_unknown_keys(raw, KNOWN_CLAIMS_FILE_KEYS, source_name)
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
        _refuse_unknown_keys(entry, KNOWN_CLAIM_KEYS, f"claim '{claim_id}'")
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


def missing_claim(claim_id: str) -> ClaimResult:
    """The verdict for a required claim that was never declared."""
    return ClaimResult(
        claim_id=claim_id,
        statement="required by countersign.toml but not declared in the claims file",
        command="",
        expect="exit 0",
        status=MISSING,
    )


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


def _excerpt(stdout: bytes, stderr: bytes, max_output_bytes: int, keep_output: bool) -> tuple[str, int]:
    """What the receipt keeps of the command's output: nothing when the
    policy says so, otherwise a redacted, truncated excerpt."""
    if not keep_output:
        return "", 0
    text, redactions = redact_secrets(_decode(stdout) + _decode(stderr))
    return _truncate(text, max_output_bytes), redactions


def run_claim(claim: Claim, cwd: Path, default_timeout_s: int, max_output_bytes: int, keep_output: bool = True) -> ClaimResult:
    """Run one claim's command and judge it exactly as declared.

    The judgement reads the raw output; only the excerpt kept on the
    receipt is redacted and truncated."""
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
        excerpt, redactions = _excerpt(stdout, stderr, max_output_bytes, keep_output)
        return ClaimResult(
            claim_id=claim.claim_id,
            statement=claim.statement,
            command=claim.command,
            expect=claim.expect,
            status=TIMEOUT,
            duration_ms=int((time.monotonic() - started) * 1000),
            output_excerpt=excerpt,
            redactions=redactions,
        )

    duration_ms = int((time.monotonic() - started) * 1000)
    combined = _decode(stdout) + _decode(stderr)
    excerpt, redactions = _excerpt(stdout, stderr, max_output_bytes, keep_output)

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
        redactions=redactions,
    )
