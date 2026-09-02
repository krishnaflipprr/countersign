"""The receipt: what a countersignature actually is.

Three renderings of one run:

  JSON      the machine-readable receipt, digest-bound to the register
  Markdown  for CI step summaries, pull request comments and status pages
  Terminal  for the human who just ran the command

The wording rule inherited from the evidence-pack tradition: the receipt
never says "verified" without saying what was checked, and a skipped check
is printed as a skip. A receipt that overstates is worse than no receipt.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import __version__
from .claims import FAIL, PASS, TIMEOUT
from .engine import FAIL_VERDICT, GateResult

STATUS_MARK = {PASS: "PASS", FAIL: "FAIL", TIMEOUT: "TIMEOUT"}


def receipt_json(result: GateResult) -> dict:
    return {
        "run_id": result.run_id,
        "recorded_at": result.recorded_at,
        "countersign_version": __version__,
        "verdict": result.verdict,
        "config": {"path": result.config_path, "sha256": result.config_sha256},
        "claims_file": (
            {"sha256": result.claims_sha256} if result.claims_sha256 else None
        ),
        "git_commit": result.git_commit,
        "scan": {
            "files_scanned": result.files_scanned,
            "findings": len(result.findings),
            "exemptions": result.exemptions,
        },
        "findings": [
            {
                "path": f.path,
                "line": f.line,
                "rule_id": f.rule_id,
                "why": f.why,
                "evidence": f.evidence,
            }
            for f in result.findings
        ],
        "claims": (
            None
            if result.claim_results is None
            else [
                {
                    "claim_id": c.claim_id,
                    "statement": c.statement,
                    "command": c.command,
                    "expect": c.expect,
                    "status": c.status,
                    "exit_code": c.exit_code,
                    "duration_ms": c.duration_ms,
                    "output_excerpt": c.output_excerpt,
                }
                for c in result.claim_results
            ]
        ),
        "claims_status": result.claims_status,
        "register": {"index": result.register_index, "hash": result.register_hash},
        "duration_ms": result.duration_ms,
        "notes": result.notes,
    }


def write_receipt(result: GateResult, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt_json(result), indent=2, sort_keys=True), encoding="utf-8")
    return path


def load_receipt(path: Path) -> dict:
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def find_receipt(receipts_root: Path, run_id: str) -> Path | None:
    candidate = Path(receipts_root) / f"{run_id}.json"
    return candidate if candidate.exists() else None


def markdown_summary(result: GateResult) -> str:
    lines: list[str] = []
    verdict_word = "COUNTERSIGNED" if result.verdict != FAIL_VERDICT else "NOT COUNTERSIGNED"
    lines.append(f"## Countersign: {verdict_word}")
    lines.append("")
    lines.append(f"| | |")
    lines.append(f"|---|---|")
    lines.append(f"| Run | `{result.run_id}` |")
    lines.append(f"| Git commit | `{result.git_commit}` |")
    lines.append(f"| Files scanned | {result.files_scanned} |")
    lines.append(f"| Marker findings | {len(result.findings)} |")
    lines.append(f"| Line exemptions used | {result.exemptions} |")
    claims_line = "skipped (no claims file declared)" if result.claim_results is None else f"{len(result.claim_results)} declared"
    lines.append(f"| Claims | {claims_line} |")
    lines.append(f"| Register head | `{result.register_hash[:16]}...` at entry {result.register_index} |")
    lines.append("")

    if result.findings:
        lines.append("### Findings")
        lines.append("")
        lines.append("| Location | Rule | Evidence |")
        lines.append("|---|---|---|")
        for f in result.findings[:25]:
            evidence = f.evidence.replace("|", "\\|")[:120]
            lines.append(f"| `{f.path}:{f.line}` | {f.rule_id} | `{evidence}` |")
        if len(result.findings) > 25:
            lines.append(f"| ... | ... | {len(result.findings) - 25} more in the receipt JSON |")
        lines.append("")

    if result.claim_results is not None:
        lines.append("### Claims")
        lines.append("")
        lines.append("| Status | Claim | Command |")
        lines.append("|---|---|---|")
        for c in result.claim_results:
            command = c.command.replace("|", "\\|")[:100]
            lines.append(f"| {STATUS_MARK.get(c.status, c.status)} | {c.statement} | `{command}` |")
        lines.append("")

    for note in result.notes:
        lines.append(f"> {note}")
    if result.notes:
        lines.append("")
    return "\n".join(lines)


def terminal_summary(result: GateResult, use_color: bool = True) -> str:
    green, red, yellow, bold, reset = ("", "", "", "", "")
    if use_color:
        green, red, yellow, bold, reset = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[0m"

    lines: list[str] = []
    lines.append(f"{bold}Countersign{reset} run {result.run_id}")
    lines.append(f"  commit {result.git_commit} · {result.files_scanned} files scanned · {result.duration_ms} ms")
    lines.append("")

    if result.findings:
        mark = f"{red}✗{reset}"
        lines.append(f"{mark} marker scan: {len(result.findings)} finding(s)")
        for f in result.findings[:15]:
            lines.append(f"    {f.path}:{f.line}  [{f.rule_id}]  {f.evidence[:100]}")
        if len(result.findings) > 15:
            lines.append(f"    ... {len(result.findings) - 15} more")
    else:
        lines.append(f"{green}✓{reset} marker scan: clean ({result.files_scanned} files)")

    if result.claim_results is None:
        lines.append(f"{yellow}–{reset} claims: skipped, no claims file declared")
    else:
        for c in result.claim_results:
            if c.status == PASS:
                lines.append(f"{green}✓{reset} claim {c.claim_id}: {c.statement}")
            elif c.status == TIMEOUT:
                lines.append(f"{red}✗{reset} claim {c.claim_id}: TIMED OUT after {c.duration_ms} ms: {c.statement}")
            else:
                excerpt = (c.output_excerpt or "").strip().splitlines()
                tail = excerpt[-1][:120] if excerpt else "no output"
                lines.append(f"{red}✗{reset} claim {c.claim_id}: {c.statement}")
                lines.append(f"    command: {c.command}")
                lines.append(f"    output ends: {tail}")

    for note in result.notes:
        lines.append(f"{yellow}–{reset} note: {note}")

    lines.append("")
    if result.verdict == FAIL_VERDICT:
        lines.append(f"{red}{bold}NOT COUNTERSIGNED{reset} · {len(result.findings)} finding(s), {len(result.failed_claims)} failed claim(s)")
        lines.append(f"The work did not pass its own declared checks. Fix the code or the claims.")
    else:
        lines.append(f"{green}{bold}COUNTERSIGNED{reset} · register entry {result.register_index}, head {result.register_hash[:16]}...")
    return "\n".join(lines)
