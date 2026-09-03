# audited on 20260903
"""The gate: run every check, write every result to the register.

One verify run does five things, in this order:

1. Records what it is about to read (config, claims, git state) with SHA-256
   fingerprints, so a later reproduce can prove it read the same files.
   This entry is on disk before any check runs: a run that is killed
   halfway still left a trace of having started.
2. Runs the marker scan and appends every finding to the register.
3. Runs every declared claim's command and appends each verdict as soon as
   it is known. A claim the config requires but the file does not declare
   is recorded as missing.
4. When a base revision is given, diffs the claims file against it and
   records every change; a weakened claim fails the gate unless the config
   says otherwise.
5. Records the verdict and the register head.

The verdict rule is deliberately simple enough to check by hand: any
finding, any failed, timed-out or missing claim, or any weakened claim
means the run is not countersigned. A skipped check (no claims file
declared) is reported as a skip on the receipt, never silently folded into
a pass.

A claims file or config that cannot be honoured raises before anything is
written; a run that dies after it started appends a ``run_aborted`` entry
on its way out so the register never shows a start without an ending.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .claims import NOT_PASSED, ClaimResult, load_claims, missing_claim, run_claim
from .claimsdiff import ClaimChange, diff_against_ref
from .config import Config, ConfigError, file_sha256
from .register import Register
from .stubscan import Finding, scan_tree

PASS_VERDICT = "pass"
FAIL_VERDICT = "fail"

TEST_EXCLUSION_NOTE = "test files were excluded from the marker scan by policy (exclude_tests = true)"
GIT_NOT_AVAILABLE = "git not available"
GIT_NOT_A_REPOSITORY = "not a git repository"
GIT_NO_COMMITS = "no commits yet"


@dataclass
class GateResult:
    run_id: str
    recorded_at: str
    verdict: str
    config_path: str
    config_sha256: str
    claims_sha256: str | None
    git_commit: str
    files_scanned: int
    git_dirty: bool | None = None
    tests_excluded: bool = True
    findings: list[Finding] = field(default_factory=list)
    exemptions: int = 0
    claim_results: list[ClaimResult] | None = None
    claims_status: str = "skipped"
    claims_base: str | None = None
    claims_diff: list[ClaimChange] | None = None
    claims_base_problem: str | None = None
    register_index: int = 0
    register_hash: str = ""
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def failed_claims(self) -> list[ClaimResult]:
        if self.claim_results is None:
            return []
        return [c for c in self.claim_results if c.status in NOT_PASSED]

    @property
    def weakened_claims(self) -> list[ClaimChange]:
        return [c for c in (self.claims_diff or []) if c.weakened]


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=str(root), capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_state(root: Path) -> tuple[str, bool | None]:
    """(commit, dirty). ``dirty`` is None when it could not be determined.

    A receipt that names a commit while the scanned files differ from that
    commit would misstate what was checked, so the working tree state is
    recorded next to the hash. Untracked files count as dirty: they are
    scanned, and the commit does not contain them.
    """
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if inside is None:
        return GIT_NOT_AVAILABLE, None
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return GIT_NOT_A_REPOSITORY, None
    head = _git(root, "rev-parse", "--verify", "HEAD")
    if head is None:
        return GIT_NOT_AVAILABLE, None
    if head.returncode != 0:
        # An initialised repository with nothing committed: every scanned
        # file is uncommitted by definition.
        return GIT_NO_COMMITS, True
    commit = head.stdout.strip()
    status = _git(root, "status", "--porcelain", "--untracked-files=normal")
    if status is None or status.returncode != 0:
        return commit, None
    return commit, bool(status.stdout.strip())


def run_gate(config: Config, *, register: Register | None = None, claims_base: str | None = None) -> GateResult:
    """Run every check. Appends evidence to the register; writes nothing else."""
    config_path = Path(config.config_path)
    if not config_path.is_file():
        raise ConfigError(f"no config file at {config_path}; the run would have nothing to fingerprint")
    if claims_base and not config.claims_file:
        raise ConfigError("a claims base revision was given but no claims file is configured")

    started = datetime.now(timezone.utc)
    run_id = f"{started.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    register = register or Register(config.register_path())

    # Everything that can refuse the run refuses here, before any evidence
    # is written: a bad config, claims file or base revision is a usage
    # error, not a run.
    files = config.collect_files()
    claims_path = config.claims_path()
    claims_sha256 = file_sha256(claims_path) if claims_path else None
    config_sha256 = file_sha256(config_path)

    notes: list[str] = []
    if config.exclude_tests:
        notes.append(TEST_EXCLUSION_NOTE)

    claims = None
    if not config.claims_file:
        notes.append("the claims check was skipped by request (no claims file configured); a skipped check is not a passed check")
    elif claims_path is None:
        notes.append(f"no claims file found at {config.claims_file}; the claims check was skipped, not passed")
    else:
        claims = load_claims(claims_path)

    declared_ids = {c.claim_id for c in (claims or [])}
    missing_ids = [claim_id for claim_id in config.required_claims if claim_id not in declared_ids]
    if missing_ids:
        notes.append(
            f"{len(missing_ids)} claim(s) required by the config are not declared: {', '.join(missing_ids)}; "
            "each is recorded as missing and fails the gate"
        )

    changes: list[ClaimChange] | None = None
    base_problem: str | None = None
    if claims_base:
        changes, base_problem = diff_against_ref(config.root, claims_base, config.claims_file or "", claims)
        if base_problem:
            notes.append(f"claims at {claims_base} could not be parsed ({base_problem}); every current claim is shown as added")

    git_commit, git_dirty = _git_state(config.root)

    started_entry = register.append(
        "run_started",
        {
            "run_id": run_id,
            "countersign_version": __version__,
            "git_commit": git_commit,
            "git_dirty": git_dirty,
            "inputs": [
                {"role": "config", "path": str(config_path), "sha256": config_sha256},
                *(
                    [{"role": "claims", "path": str(claims_path), "sha256": claims_sha256}]
                    if claims_path
                    else []
                ),
            ],
            "files_scanned": len(files),
            "tests_excluded": config.exclude_tests,
            "required_claims": list(config.required_claims),
            "claims_base": claims_base,
            "notes": notes,
        },
        at=started,
    )

    try:
        findings, exemptions, inert_markers, files_scanned = scan_tree(config, files)
        if inert_markers:
            notes.append(
                f"{inert_markers} exemption marker(s) sit on lines no rule flags; they suppress nothing today "
                "and are not counted as used, but would suppress a finding if those lines changed"
            )
        for finding in findings:
            register.append(
                "finding",
                {
                    "run_id": run_id,
                    "path": finding.path,
                    "line": finding.line,
                    "rule_id": finding.rule_id,
                    "why": finding.why,
                    "evidence": finding.evidence,
                },
            )

        claim_results: list[ClaimResult] | None = None
        claims_status = "skipped"
        if claims is not None or missing_ids:
            claim_results = []
            claims_status = "ran"
            for claim in claims or []:
                result = run_claim(claim, config.root, config.timeout_s, config.max_output_bytes)
                claim_results.append(result)
                register.append("claim", {"run_id": run_id, **_claim_body(result)})
            for claim_id in missing_ids:
                result = missing_claim(claim_id)
                claim_results.append(result)
                register.append("claim", {"run_id": run_id, **_claim_body(result)})

        if changes is not None:
            register.append(
                "claims_diff",
                {
                    "run_id": run_id,
                    "base": claims_base,
                    "base_problem": base_problem,
                    "changes": [change.__dict__ for change in changes],
                },
            )
    except BaseException as exc:
        register.append("run_aborted", {"run_id": run_id, "reason": f"{type(exc).__name__}: {exc}"[:500]})
        raise

    verdict = FAIL_VERDICT if findings else PASS_VERDICT
    if any(result.status in NOT_PASSED for result in (claim_results or [])):
        verdict = FAIL_VERDICT
    weakened = [c for c in (changes or []) if c.weakened]
    if weakened:
        if config.fail_on_weakened:
            verdict = FAIL_VERDICT
            notes.append(f"{len(weakened)} claim(s) weakened against {claims_base}; the gate fails on weakened claims (fail_on_weakened = true)")
        else:
            notes.append(f"{len(weakened)} claim(s) weakened against {claims_base}; recorded, not failed (fail_on_weakened = false)")

    finished = datetime.now(timezone.utc)
    head = register.append(
        "run_finished",
        {
            "run_id": run_id,
            "verdict": verdict,
            "findings": len(findings),
            "exemptions": exemptions,
            "claims_status": claims_status,
            "claims_total": len(claim_results) if claim_results is not None else 0,
            "claims_failed": len([r for r in (claim_results or []) if r.status in NOT_PASSED]),
            "claims_weakened": len(weakened),
        },
        at=finished,
    )

    return GateResult(
        run_id=run_id,
        recorded_at=finished.isoformat(),
        verdict=verdict,
        config_path=str(config_path),
        config_sha256=config_sha256,
        claims_sha256=claims_sha256,
        git_commit=started_entry["body"]["git_commit"],
        git_dirty=git_dirty,
        files_scanned=files_scanned,
        tests_excluded=config.exclude_tests,
        findings=findings,
        exemptions=exemptions,
        claim_results=claim_results,
        claims_status=claims_status,
        claims_base=claims_base,
        claims_diff=changes,
        claims_base_problem=base_problem,
        register_index=head["index"],
        register_hash=head["hash"],
        duration_ms=int((finished - started).total_seconds() * 1000),
        notes=notes,
    )


def _claim_body(result: ClaimResult) -> dict:
    return {
        "claim_id": result.claim_id,
        "statement": result.statement,
        "command": result.command,
        "expect": result.expect,
        "status": result.status,
        "exit_code": result.exit_code,
        "duration_ms": result.duration_ms,
        "output_excerpt": result.output_excerpt,
    }
