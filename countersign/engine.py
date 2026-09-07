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
4. When a base revision is given (a pull request), enforces the base
   revision's policy rather than the checkout's, diffs both the policy and
   the claims file against the base, and records every change. A weakening
   of either fails the gate unless a maintainer approved it (``approved``,
   which the GitHub action sets from the ``countersign-approved`` label).
5. Records the verdict and the register head.

The verdict rule is deliberately simple enough to check by hand: any
finding, any failed, timed-out or missing claim, an absent claims file the
policy did not make optional, or any unapproved weakening means the run is
not countersigned. Approval softens weakenings only: a scan of nothing is
a usage error and a run with no claims file still fails, label or not.

A claims file or config that cannot be honoured raises before anything is
written; a run that dies after it started appends a ``run_aborted`` entry
on its way out so the register never shows a start without an ending.
"""

from __future__ import annotations

import hashlib
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .claims import NOT_PASSED, ClaimResult, ClaimsError, fingerprint_inputs, load_claims, missing_claim, run_claim
from .claimsdiff import ClaimChange, diff_against_ref, file_text_at, input_content_changes, merge_changes
from .config import Config, ConfigError, file_sha256
from .policydiff import PolicyChange, diff_policy
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
    claims_status: str = "skipped"  # ran, skipped (optional by policy) or absent (fails)
    claims_base: str | None = None
    claims_diff: list[ClaimChange] | None = None
    claims_base_problem: str | None = None
    base_config_sha256: str | None = None
    base_claims_sha256: str | None = None
    policy_sha256: str = ""
    base_policy_sha256: str | None = None
    policy_diff: list[PolicyChange] | None = None
    approved: bool = False
    approval_used: bool = False
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

    @property
    def weakened_policy(self) -> list[PolicyChange]:
        return [c for c in (self.policy_diff or []) if c.weakened]


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


def base_policy(config: Config, ref: str) -> tuple[Config | None, bytes | None]:
    """The config as it is at ``ref``, parsed against this checkout's root.

    Returns (config, raw text). (None, None) when the base has no config
    file: then there is no policy to enforce and the checkout's own is used,
    which the receipt says."""
    relative = Path(config.config_path).resolve().relative_to(Path(config.root).resolve()).as_posix()
    try:
        text = file_text_at(config.root, ref, relative, "config")
    except ClaimsError as exc:
        raise ConfigError(str(exc)) from None
    if text is None:
        return None, None
    return Config.from_bytes(text, root=config.root, config_path=config.config_path), text


def run_gate(config: Config, *, register: Register | None = None, claims_base: str | None = None, approved: bool = False) -> GateResult:
    """Run every check. Appends evidence to the register; writes nothing else.

    ``claims_base`` names the revision whose policy and claims the run is
    judged against (the branch a pull request targets). ``approved`` says a
    maintainer accepted this pull request's weakenings; it never turns an
    empty scan or an absent claims file into a pass."""
    head_config = config
    config_path = Path(config.config_path)
    if not config_path.is_file():
        raise ConfigError(f"no config file at {config_path}; the run would have nothing to fingerprint")

    started = datetime.now(timezone.utc)
    run_id = f"{started.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
    register = register or Register(config.register_path())

    notes: list[str] = []

    # On a pull request the policy that counts is the base branch's. The
    # checkout's config is still fingerprinted and diffed, so every change
    # the pull request makes to the policy is on the receipt.
    base_cfg: Config | None = None
    base_config_sha256: str | None = None
    base_claims_sha256: str | None = None
    policy_changes: list[PolicyChange] | None = None
    if claims_base:
        base_cfg, base_text = base_policy(head_config, claims_base)
        if base_cfg is None:
            notes.append(f"{claims_base} has no {config_path.name}; there is no base policy to enforce, so this checkout's own policy was used")
        else:
            base_config_sha256 = hashlib.sha256(base_text or b"").hexdigest()
            policy_changes = diff_policy(base_cfg, head_config)
            config = head_config.with_policy_of(base_cfg)
            if base_cfg.claims_file:
                base_claims = file_text_at(config.root, claims_base, base_cfg.claims_file, "claims")
                base_claims_sha256 = hashlib.sha256(base_claims).hexdigest() if base_claims is not None else None
    if claims_base and not config.claims_file:
        raise ConfigError("a base revision was given but the enforced policy configures no claims file")

    # Everything that can refuse the run refuses here, before any evidence
    # is written: a bad config, claims file or base revision is a usage
    # error, not a run. A scan that matches no file is one of them: nothing
    # was checked, so nothing can pass, and no approval changes that.
    files = config.collect_files()
    if not files and not config.allow_empty:
        raise ConfigError(
            "the scan scope matched no files; a scan of nothing cannot pass. Widen [scan] paths or extensions, "
            "or set allow_empty = true to say on purpose that this repository has nothing to scan"
        )
    claims_path = config.claims_path()
    claims_sha256 = file_sha256(claims_path) if claims_path else None
    config_sha256 = file_sha256(config_path)

    if config.exclude_tests:
        notes.append(TEST_EXCLUSION_NOTE)

    claims = None
    claims_absent = False
    if not config.claims_file:
        if config.claims_optional:
            notes.append("the claims check was skipped by policy (no claims file configured, optional = true); a skipped check is not a passed check")
        else:
            claims_absent = True
            notes.append("no claims file is configured and the policy does not make claims optional; the run fails")
    elif claims_path is None:
        if config.claims_optional:
            notes.append(f"no claims file found at {config.claims_file}; skipped by policy (optional = true), not passed")
        else:
            claims_absent = True
            notes.append(f"no claims file found at {config.claims_file} and the policy does not make claims optional; the run fails")
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
        changes, base_problem = diff_against_ref(config.root, claims_base, config.claims_file or "", claims, command_change_weakens=config.command_change == "fail")
        if claims:
            changes = merge_changes(changes, input_content_changes(config.root, claims_base, claims))
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
            "policy_sha256": config.policy_sha256(),
            "base_config_sha256": base_config_sha256,
            "base_claims_sha256": base_claims_sha256,
            "approved": approved,
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
        claims_status = "absent" if claims_absent else "skipped"
        if claims is not None or missing_ids:
            claim_results = []
            claims_status = "ran"
            for claim in claims or []:
                result = run_claim(claim, config.root, config.timeout_s, config.max_output_bytes, keep_output=config.output == "excerpt")
                result.inputs = fingerprint_inputs(config.root, claim)
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
        if policy_changes is not None:
            register.append(
                "policy_diff",
                {"run_id": run_id, "base": claims_base, "changes": [change.__dict__ for change in policy_changes]},
            )
    except BaseException as exc:
        register.append("run_aborted", {"run_id": run_id, "reason": f"{type(exc).__name__}: {exc}"[:500]})
        raise

    verdict = FAIL_VERDICT if findings else PASS_VERDICT
    if any(result.status in NOT_PASSED for result in (claim_results or [])):
        verdict = FAIL_VERDICT
    if claims_absent:
        verdict = FAIL_VERDICT
    redacted = sum(result.redactions for result in (claim_results or []))
    if redacted:
        notes.append(f"{redacted} credential-shaped value(s) in command output were replaced with [redacted] before the receipt kept it")
    weakened = [c for c in (changes or []) if c.weakened]
    weakened_policy = [c for c in (policy_changes or []) if c.weakened]
    approval_used = False
    if weakened or weakened_policy:
        what = []
        if weakened:
            what.append(f"{len(weakened)} claim(s) weakened")
        if weakened_policy:
            what.append(f"{len(weakened_policy)} policy field(s) weakened")
        described = " and ".join(what) + f" against {claims_base}"
        if approved:
            approval_used = True
            notes.append(f"{described}; a maintainer approved this pull request (countersign-approved), so the weakening is recorded, not failed")
        elif config.fail_on_weakened:
            verdict = FAIL_VERDICT
            notes.append(f"{described}; the gate fails on weakened claims and policy (fail_on_weakened = true) unless a maintainer adds the countersign-approved label")
        else:
            notes.append(f"{described}; recorded, not failed (fail_on_weakened = false)")
    elif approved and claims_base:
        notes.append("the countersign-approved label was present but nothing needed approving")

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
            "policy_weakened": len(weakened_policy),
            "approval_used": approval_used,
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
        base_config_sha256=base_config_sha256,
        base_claims_sha256=base_claims_sha256,
        policy_sha256=config.policy_sha256(),
        base_policy_sha256=base_cfg.policy_sha256() if base_cfg else None,
        policy_diff=policy_changes,
        approved=approved,
        approval_used=approval_used,
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
        "inputs": dict(result.inputs),
    }
