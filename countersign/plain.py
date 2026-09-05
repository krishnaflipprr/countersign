# audited on 20260905
"""The receipt in plain words.

The terminal summary and the pack tables are written for engineers. The
person who most needs a receipt is often the one who cannot read a stack
trace: they asked an agent for a feature and were told it is done. This
module renders the same facts as short sentences, one idea each, with no
jargon the reader has to look up. Every sentence is derived from the run's
recorded facts; nothing is softened, nothing is inferred.
"""

from __future__ import annotations

from .claims import FAIL, MISSING, PASS, TIMEOUT
from .engine import FAIL_VERDICT, GateResult
from .stubscan import RULES

# What each finding kind means to someone who did not write the rule. The
# marker rules carry their own words; the structural kinds are named here.
KIND_IN_WORDS = {rule.rule_id: rule.plain for rule in RULES}
KIND_IN_WORDS["empty-body"] = "a function that does nothing"
KIND_IN_WORDS["unparseable"] = "a Python file that does not even parse"

FINDINGS_SHOWN = 5
CLAIMS_SHOWN = 3


def _count(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n} {singular if n == 1 else (plural or singular + 's')}"


def _last_line(text: str) -> str:
    lines = [line.strip() for line in (text or "").strip().splitlines() if line.strip()]
    return lines[-1][:120] if lines else ""


def plain_sentences(result: GateResult) -> list[str]:
    """Short sentences, each ending with a full stop, telling a non-engineer
    what was checked, what held, and what did not."""
    sentences: list[str] = []
    results = result.claim_results or []
    failed = [c for c in results if c.status == FAIL]
    timed_out = [c for c in results if c.status == TIMEOUT]
    missing = [c for c in results if c.status == MISSING]
    held = [c for c in results if c.status == PASS]
    weakened = result.weakened_claims

    if result.verdict == FAIL_VERDICT:
        reasons: list[str] = []
        if result.findings:
            reasons.append(f"{_count(len(result.findings), 'place')} in the code look unfinished")
        if failed or timed_out:
            reasons.append(f"{_count(len(failed) + len(timed_out), 'claim')} out of {len(results)} did not hold")
        if missing:
            reasons.append(f"{_count(len(missing), 'required claim')} {'was' if len(missing) == 1 else 'were'} never declared")
        if weakened:
            reasons.append(f"the claims file was weakened compared with {result.claims_base}")
        sentences.append("Not countersigned: " + "; ".join(reasons) + ".")
    else:
        scanned = f"{_count(result.files_scanned, 'file')} {'was' if result.files_scanned == 1 else 'were'} scanned and none carries unfinished work"
        if result.claim_results is None:
            sentences.append(f"Countersigned on the scan alone: {scanned}.")
        elif held:
            shown = "; ".join(f"'{c.statement}'" for c in held[:CLAIMS_SHOWN])
            more = f" and {len(held) - CLAIMS_SHOWN} more" if len(held) > CLAIMS_SHOWN else ""
            sentences.append(f"Countersigned: {scanned}, and {_count(len(held), 'claim')} held: {shown}{more}.")
        else:
            sentences.append(f"Countersigned: {scanned}, and no claim was declared.")

    for finding in result.findings[:FINDINGS_SHOWN]:
        kind = KIND_IN_WORDS.get(finding.rule_id, finding.why)
        sentences.append(f"{finding.path} line {finding.line}: {kind}.")
    if len(result.findings) > FINDINGS_SHOWN:
        sentences.append(f"{len(result.findings) - FINDINGS_SHOWN} more such places are listed in the findings table.")

    for claim in failed:
        tail = _last_line(claim.output_excerpt)
        ending = f"; its last line of output was '{tail}'" if tail else "; it printed nothing"
        code = f"exit code {claim.exit_code}" if claim.exit_code is not None else "no exit code"
        sentences.append(f"The claim '{claim.statement}' did not hold: the command '{claim.command}' ended with {code}{ending}.")
    for claim in timed_out:
        sentences.append(f"The claim '{claim.statement}' was stopped after {max(1, round(claim.duration_ms / 1000))} seconds without finishing.")
    for claim in missing:
        sentences.append(f"The claim '{claim.claim_id}' is required by your configuration but was never declared.")
    if result.verdict == FAIL_VERDICT and held:
        sentences.append(f"{_count(len(held), 'other claim')} held.")

    if result.claim_results is None:
        sentences.append("No claims were checked: no claims file was found, and a skipped check is not a pass.")

    if weakened:
        parts: list[str] = []
        for change in weakened:
            if change.kind == "removed":
                parts.append(f"'{change.claim_id}' was removed")
            elif "expect" in change.fields:
                parts.append(f"'{change.claim_id}' now expects the opposite outcome")
            elif "needle" in change.fields:
                parts.append(f"'{change.claim_id}' now looks for a different phrase in the output")
            else:
                parts.append(f"'{change.claim_id}' was changed")
        sentences.append(f"The claims file was weakened compared with {result.claims_base}: " + "; ".join(parts) + ".")

    if result.git_dirty:
        sentences.append(
            f"The files checked include uncommitted changes, so this receipt describes the working tree, "
            f"not commit {result.git_commit[:8]} exactly."
        )

    sentences.append("No AI judged anything here; every sentence comes from a command's exit code or a text match you can re-run yourself.")
    return sentences
