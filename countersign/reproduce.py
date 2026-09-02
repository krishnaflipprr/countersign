"""Prove that a recorded run still reproduces.

The promise this module keeps: any verification run can be re-run later,
from the same inputs, and produce the same findings. It takes a receipt,
confirms the config and claims files are byte for byte the ones the run
read, re-runs the marker scan and every claim, and compares what comes out
against what the receipt recorded.

Commands that touch the outside world may legitimately diverge (a test that
pings a live service can pass today and fail in a year); each divergence is
reported, not excused. The marker scan has no such excuse: it is pure
arithmetic over files, and any difference means the files changed.
"""

from __future__ import annotations

import json
from pathlib import Path

from .claims import load_claims, run_claim
from .config import Config, file_sha256
from .receipt import find_receipt, load_receipt
from .register import Register
from .stubscan import scan_tree


def _finding_key(finding: dict) -> str:
    return json.dumps(
        {k: finding.get(k) for k in ("path", "line", "rule_id", "evidence")},
        sort_keys=True,
    )


def _claim_key(claim: dict) -> str:
    return json.dumps(
        {k: claim.get(k) for k in ("claim_id", "status", "exit_code")},
        sort_keys=True,
    )


def reproduce_run(config: Config, run_id: str) -> tuple[bool, list[str]]:
    """Re-derive a recorded run. Returns (reproduced, human readable notes)."""
    notes: list[str] = []

    receipt_path = find_receipt(config.receipts_root(), run_id)
    if receipt_path is None:
        return False, [f"no receipt for run {run_id} under {config.receipts_root()}"]

    receipt = load_receipt(receipt_path)
    notes.append(f"receipt: {receipt_path.name}, verdict {receipt['verdict']}, recorded {receipt['recorded_at']}")

    register = Register(config.register_path())
    intact, chain_note = register.verify_chain()
    if not intact:
        return False, notes + [f"register: {chain_note}"]
    notes.append(f"register: {chain_note}")

    recorded_config_sha = receipt["config"]["sha256"]
    actual_config_sha = file_sha256(config.config_path)
    if recorded_config_sha != actual_config_sha:
        notes.append(
            f"config: CHANGED since the run ({recorded_config_sha[:12]}... then, {actual_config_sha[:12]}... now); "
            "findings may differ for that reason"
        )
    else:
        notes.append(f"config: sha256 matches ({actual_config_sha[:12]}...)")

    recorded_claims_sha = (receipt.get("claims_file") or {}).get("sha256")
    if recorded_claims_sha:
        claims_path = config.claims_path()
        if claims_path is None:
            notes.append("claims: the file the run read is no longer present")
        else:
            actual_claims_sha = file_sha256(claims_path)
            if actual_claims_sha != recorded_claims_sha:
                notes.append(
                    f"claims: CHANGED since the run ({recorded_claims_sha[:12]}... then, {actual_claims_sha[:12]}... now)"
                )
            else:
                notes.append(f"claims: sha256 matches ({actual_claims_sha[:12]}...)")

    rerun_findings, _exemptions, files_scanned = scan_tree(config)
    recorded_findings = receipt["findings"]
    rerun_keys = sorted(_finding_key(f.__dict__) for f in rerun_findings)
    recorded_keys = sorted(_finding_key(f) for f in recorded_findings)

    scan_reproduced = rerun_keys == recorded_keys
    if scan_reproduced:
        notes.append(f"marker scan: {len(rerun_keys)} findings re-derived, identical ({files_scanned} files)")
    else:
        only_recorded = [k for k in recorded_keys if k not in rerun_keys][:3]
        only_now = [k for k in rerun_keys if k not in recorded_keys][:3]
        notes.append(
            f"marker scan: DOES NOT reproduce, {len(recorded_keys)} recorded vs {len(rerun_keys)} re-derived"
        )
        for item in only_recorded:
            notes.append(f"  recorded but not re-derived: {json.loads(item).get('path')}:{json.loads(item).get('line')}")
        for item in only_now:
            notes.append(f"  re-derived but not recorded: {json.loads(item).get('path')}:{json.loads(item).get('line')}")

    claims_reproduced = True
    recorded_claims = receipt.get("claims")
    if recorded_claims is None:
        notes.append("claims: the run skipped the claims check (none declared); nothing to re-run")
    elif config.claims_path() is None:
        claims_reproduced = False
        notes.append("claims: the claims file has since been removed; cannot re-run")
    else:
        claims = load_claims(config.claims_path()) or []
        for recorded in recorded_claims:
            match = next((c for c in claims if c.claim_id == recorded["claim_id"]), None)
            if match is None:
                claims_reproduced = False
                notes.append(f"claim {recorded['claim_id']}: no longer declared in the claims file")
                continue
            rerun_result = run_claim(match, config.root, config.timeout_s, config.max_output_bytes)
            if rerun_result.status == recorded["status"]:
                notes.append(f"claim {recorded['claim_id']}: {recorded['status'].upper()}, reproduced ({rerun_result.duration_ms} ms)")
            else:
                claims_reproduced = False
                notes.append(
                    f"claim {recorded['claim_id']}: was {recorded['status'].upper()}, re-ran {rerun_result.status.upper()}"
                )

    # The verdict is a pure function of the findings and the claim statuses,
    # so if both of those reproduced, the verdict reproduced with them. The
    # gate itself is never re-run here: appending evidence while checking
    # evidence would put reproduce output into the register.
    reproduced = scan_reproduced and claims_reproduced
    notes.append(f"overall: {'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
    return reproduced, notes
