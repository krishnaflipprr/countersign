# audited on 20260903
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

A receipt that cannot be read is a verdict (not reproduced), never a crash:
the file may have been hand-edited, and that is exactly the case this
command exists to catch.
"""

from __future__ import annotations

import json
from pathlib import Path

from .claims import ClaimsError, load_claims, run_claim
from .config import Config, ConfigError, file_sha256
from .receipt import find_receipt, load_receipt
from .register import Register
from .stubscan import scan_tree


def _finding_key(finding: dict) -> str:
    return json.dumps(
        {k: finding.get(k) for k in ("path", "line", "rule_id", "evidence")},
        sort_keys=True,
    )


def _load_receipt_or_explain(receipt_path: Path) -> tuple[dict | None, str | None]:
    try:
        receipt = load_receipt(receipt_path)
    except (OSError, ValueError) as exc:  # json.JSONDecodeError is a ValueError
        return None, f"receipt {receipt_path.name} cannot be read: {exc}"
    if not isinstance(receipt, dict):
        return None, f"receipt {receipt_path.name} cannot be read: not a receipt object"
    for key in ("verdict", "recorded_at", "config", "findings", "scan"):
        if key not in receipt:
            return None, f"receipt {receipt_path.name} cannot be read: missing '{key}'"
    if not isinstance(receipt["config"], dict) or "sha256" not in receipt["config"]:
        return None, f"receipt {receipt_path.name} cannot be read: config fingerprint missing"
    if not isinstance(receipt["findings"], list):
        return None, f"receipt {receipt_path.name} cannot be read: findings are not a list"
    return receipt, None


def reproduce_run(config: Config, run_id: str) -> tuple[bool, list[str]]:
    """Re-derive a recorded run. Returns (reproduced, human readable notes)."""
    notes: list[str] = []

    receipt_path = find_receipt(config.receipts_root(), run_id)
    if receipt_path is None:
        return False, [f"no receipt for run {run_id} under {config.receipts_root()}"]

    receipt, problem = _load_receipt_or_explain(receipt_path)
    if receipt is None:
        return False, [problem or "receipt cannot be read"]
    notes.append(f"receipt: {receipt_path.name}, verdict {receipt['verdict']}, recorded {receipt['recorded_at']}")

    register = Register(config.register_path())
    intact, chain_note = register.verify_chain()
    if not intact:
        return False, notes + [f"register: {chain_note}"]
    notes.append(f"register: {chain_note}")

    recorded_config_sha = str(receipt["config"]["sha256"])
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

    try:
        rerun_findings, _exemptions, _inert, files_scanned = scan_tree(config)
    except ConfigError as exc:
        return False, notes + [f"marker scan: cannot re-run, {exc}", "overall: NOT REPRODUCED"]
    recorded_findings = receipt["findings"]
    rerun_keys = sorted(_finding_key(f.__dict__) for f in rerun_findings)
    recorded_keys = sorted(_finding_key(f) for f in recorded_findings if isinstance(f, dict))

    recorded_files = receipt["scan"].get("files_scanned") if isinstance(receipt["scan"], dict) else None
    if isinstance(recorded_files, int) and recorded_files != files_scanned:
        notes.append(f"tree: {recorded_files} file(s) then, {files_scanned} now; files were added or removed since the run")

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
        notes.append("claims: the run skipped the claims check; nothing to re-run")
    elif not isinstance(recorded_claims, list):
        claims_reproduced = False
        notes.append("claims: the receipt's claims section cannot be read")
    elif config.claims_path() is None:
        claims_reproduced = False
        notes.append("claims: the claims file has since been removed; cannot re-run")
    else:
        try:
            claims = load_claims(config.claims_path()) or []
        except ClaimsError as exc:
            claims = []
            claims_reproduced = False
            notes.append(f"claims: the claims file can no longer be honoured as written: {exc}")
        for recorded in recorded_claims:
            if not isinstance(recorded, dict) or "claim_id" not in recorded or "status" not in recorded:
                claims_reproduced = False
                notes.append("claim: a recorded claim entry cannot be read")
                continue
            match = next((c for c in claims if c.claim_id == recorded["claim_id"]), None)
            if match is None:
                claims_reproduced = False
                notes.append(f"claim {recorded['claim_id']}: no longer declared in the claims file")
                continue
            rerun_result = run_claim(match, config.root, config.timeout_s, config.max_output_bytes)
            if rerun_result.status == recorded["status"]:
                notes.append(f"claim {recorded['claim_id']}: {str(recorded['status']).upper()}, reproduced ({rerun_result.duration_ms} ms)")
            else:
                claims_reproduced = False
                notes.append(
                    f"claim {recorded['claim_id']}: was {str(recorded['status']).upper()}, re-ran {rerun_result.status.upper()}"
                )

    # The verdict is a pure function of the findings and the claim statuses,
    # so if both of those reproduced, the verdict reproduced with them. The
    # gate itself is never re-run here: appending evidence while checking
    # evidence would put reproduce output into the register.
    reproduced = scan_reproduced and claims_reproduced
    notes.append(f"overall: {'REPRODUCED' if reproduced else 'NOT REPRODUCED'}")
    return reproduced, notes
