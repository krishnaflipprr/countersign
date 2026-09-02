"""The gate: run every check, write every result to the register.

One verify run does four things, in this order:

1. Records what it is about to read (config, claims, git state) with SHA-256
   fingerprints, so a later reproduce can prove it read the same files.
2. Runs the marker scan and appends every finding to the register.
3. Runs every declared claim's command and appends every verdict.
4. Records the verdict and the register head.

The verdict rule is deliberately simple enough to check by hand: any
finding, any failed claim, or any timed-out claim means the run is not
countersigned. A skipped check (no claims file declared) is reported as a
skip on the receipt, never silently folded into a pass.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .claims import FAIL, TIMEOUT, ClaimResult, load_claims, run_claims
from .config import Config, file_sha256
from .register import Register
from .stubscan import Finding, scan_tree

PASS_VERDICT = "pass"
FAIL_VERDICT = "fail"


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
    findings: list[Finding] = field(default_factory=list)
    exemptions: int = 0
    claim_results: list[ClaimResult] | None = None
    claims_status: str = "skipped"
    register_index: int = 0
    register_hash: str = ""
    duration_ms: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def failed_claims(self) -> list[ClaimResult]:
        if self.claim_results is None:
            return []
        return [c for c in self.claim_results if c.status in (FAIL, TIMEOUT)]


def _git_commit(root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root), capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "not a git repository"
    if completed.returncode != 0:
        return "not a git repository"
    return completed.stdout.strip()


def run_gate(config: Config, *, register: Register | None = None) -> GateResult:
    """Run every check. Appends evidence to the register; writes nothing else."""
    started = datetime.now(timezone.utc)
    run_id = f"{started.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    register = register or Register(config.register_path())

    claims_path = config.claims_path()
    claims_sha256 = file_sha256(claims_path) if claims_path else None

    notes: list[str] = []
    if config.exclude_tests:
        notes.append("test files were excluded from the marker scan by policy (exclude_tests = true)")

    findings, exemptions, files_scanned = scan_tree(config)

    claims = None
    claim_results = None
    claims_status = "skipped"
    if config.claims_file and claims_path is None:
        notes.append(f"no claims file found at {config.claims_file}; the claims check was skipped, not passed")
    else:
        claims = load_claims(claims_path)
        if claims is not None:
            claim_results = run_claims(claims, config.root, config.timeout_s, config.max_output_bytes)
            claims_status = "ran"

    verdict = FAIL_VERDICT if findings else PASS_VERDICT
    if any(result.status in (FAIL, TIMEOUT) for result in (claim_results or [])):
        verdict = FAIL_VERDICT

    started_entry = register.append(
        "run_started",
        {
            "run_id": run_id,
            "countersign_version": __version__,
            "git_commit": _git_commit(config.root),
            "inputs": [
                {"role": "config", "path": str(config.config_path), "sha256": file_sha256(config.config_path)},
                *(
                    [{"role": "claims", "path": str(claims_path), "sha256": claims_sha256}]
                    if claims_path
                    else []
                ),
            ],
            "files_scanned": files_scanned,
            "notes": notes,
        },
        at=started,
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
    for result in claim_results or []:
        register.append(
            "claim",
            {
                "run_id": run_id,
                "claim_id": result.claim_id,
                "statement": result.statement,
                "command": result.command,
                "expect": result.expect,
                "status": result.status,
                "exit_code": result.exit_code,
                "duration_ms": result.duration_ms,
                "output_excerpt": result.output_excerpt,
            },
        )
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
            "claims_failed": len([r for r in (claim_results or []) if r.status in (FAIL, TIMEOUT)]),
        },
        at=finished,
    )

    return GateResult(
        run_id=run_id,
        recorded_at=finished.isoformat(),
        verdict=verdict,
        config_path=str(config.config_path),
        config_sha256=file_sha256(config.config_path),
        claims_sha256=claims_sha256,
        git_commit=started_entry["body"]["git_commit"],
        files_scanned=files_scanned,
        findings=findings,
        exemptions=exemptions,
        claim_results=claim_results,
        claims_status=claims_status,
        register_index=head["index"],
        register_hash=head["hash"],
        duration_ms=int((finished - started).total_seconds() * 1000),
        notes=notes,
    )
