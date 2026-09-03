# audited on 20260903
"""The evidence pack: one self-contained HTML file per run.

This is the artifact someone hands to someone else: a lead engineer
reviewing an agent's pull request, an agency delivering to a client, an
auditor asking what was actually checked. It has to say four things without
being asked: what was checked, how, what was found, and what was not
covered.

It has no external assets, so it opens anywhere and prints to PDF from any
browser.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .claims import PASS
from .engine import FAIL_VERDICT, TEST_EXCLUSION_NOTE, GateResult
from .receipt import commit_label
from .stubscan import RULES

NOT_COVERED_ALWAYS: tuple[str, ...] = (
    "This pack reports deterministic checks run in this repository, on this machine, at the "
    "time stated. It is not a security audit, not a code review, and not a statement of "
    "fitness for any purpose.",
    "Only the claims declared in the claims file were verified. Anything nobody declared was "
    "not checked.",
    "Claim commands are the declaring team's own commands, run exactly as declared. Whether "
    "they are deterministic, and whether they test what their statement says, is the team's "
    "responsibility; Countersign records what they did.",
    "Line exemptions are honored in the source itself and counted here. Each one is a human "
    "decision that a finding was a false positive; audit them like any other review decision.",
    "The register this pack cites lives on the machine that ran the checks. It proves that no "
    "entry was altered after it was written by anyone who did not also rewrite every entry after "
    "it. On its own that is evidence against accident and against third parties, not against "
    "the machine's owner; evidence against the owner requires the register head to be anchored "
    "outside the machine.",
)

NOT_COVERED_TESTS_EXCLUDED = (
    "Test files were excluded from the marker scan by policy (exclude_tests = true): test "
    "code legitimately fabricates data. Unfinished work inside test files was not looked for."
)


def _esc(value: object) -> str:
    return html.escape(str(value))


STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 48px; background: #E9E8E2; color: #121410;
       font: 15px/1.55 "Helvetica Neue", Arial, sans-serif; }
.sheet { max-width: 920px; margin: 0 auto; background: #F2F1EC; border: 1px solid #D3D2C8;
         border-radius: 10px; padding: 40px; }
h1 { font-size: 26px; letter-spacing: -0.02em; margin: 0 0 4px; }
h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.18em; color: #6E7168;
     margin: 36px 0 12px; font-weight: 600; }
.mono { font-family: "SFMono-Regular", Menlo, Consolas, monospace; font-size: 12px; }
.faint { color: #6E7168; }
.meta { display: grid; grid-template-columns: 210px 1fr; gap: 6px 16px; margin-top: 8px; }
.meta span:nth-child(2n) { word-break: break-all; }
table { width: 100%; border-collapse: collapse; margin-top: 8px; }
th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid #D3D2C8; vertical-align: top; }
th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.14em; color: #6E7168; font-weight: 600; }
tr:last-child td { border-bottom: none; }
.verdict-pass { color: #1E5B41; font-weight: 700; }
.verdict-fail { color: #C2402A; font-weight: 700; }
.counts { display: flex; gap: 28px; margin-top: 10px; }
.count b { display: block; font-size: 30px; letter-spacing: -0.02em; }
.note { border-left: 3px solid #1E5B41; padding: 4px 0 4px 14px; margin-top: 10px; color: #3C3F38; }
.chain { background: #E9E8E2; border: 1px solid #D3D2C8; border-radius: 6px; padding: 12px;
         word-break: break-all; }
footer { max-width: 920px; margin: 18px auto 0; color: #6E7168; font-size: 12px; }
@media print { body { padding: 0; background: #fff; } .sheet { border: none; background: #fff; } }
"""


def _esc_lines(items: tuple[str, ...]) -> str:
    return "\n".join(f"  <li>{_esc(item)}</li>" for item in items)


def build_pack(result: GateResult, path: Path) -> Path:
    generated = datetime.now(timezone.utc)
    verdict_class = "verdict-pass" if result.verdict != FAIL_VERDICT else "verdict-fail"
    verdict_word = "COUNTERSIGNED" if result.verdict != FAIL_VERDICT else "NOT COUNTERSIGNED"

    finding_rows = "\n".join(
        f"""<tr>
              <td class="mono">{_esc(f.path)}:{_esc(f.line)}</td>
              <td class="mono">{_esc(f.rule_id)}</td>
              <td>{_esc(f.why)}</td>
              <td class="mono faint">{_esc(f.evidence)}</td>
            </tr>"""
        for f in result.findings
    ) or '<tr><td colspan="4" class="faint">No marker findings.</td></tr>'

    if result.claim_results is None:
        claim_rows = '<tr><td colspan="5" class="faint">The claims check was skipped, not passed. The notes below say why.</td></tr>'
    else:
        claim_rows = "\n".join(
            f"""<tr>
                  <td class="mono {_esc('verdict-pass' if c.status == PASS else 'verdict-fail')}">{_esc(c.status.upper())}</td>
                  <td>{_esc(c.statement)}</td>
                  <td class="mono">{_esc(c.command or 'none declared')}</td>
                  <td class="mono">{_esc(c.exit_code if c.exit_code is not None else 'n/a')}</td>
                  <td class="mono faint">{_esc(f"{c.duration_ms} ms")}</td>
                </tr>"""
            for c in result.claim_results
        )

    method_rows = "\n".join(
        f"""<tr>
              <td class="mono">{_esc(rule.rule_id)}</td>
              <td>{_esc(rule.why)}</td>
              <td class="mono">{_esc(len([f for f in result.findings if f.rule_id == rule.rule_id]))}</td>
            </tr>"""
        for rule in RULES
    ) + """
        <tr><td class="mono">empty-body</td><td>functions whose body does nothing and explains nothing: a bare pass, ellipsis or empty braces with no docstring or comment (structural: Python via ast, TypeScript and JavaScript via a comment-and-string-aware scan)</td>
            <td class="mono">{}</td></tr>
        <tr><td class="mono">unparseable</td><td>Python files the parser rejects (Python, structural)</td>
            <td class="mono">{}</td></tr>
        <tr><td class="mono">claims</td><td>each declared claim's disproof command was executed and judged as declared; a claim the config requires but the file omits counts as failed</td>
            <td class="mono">{}</td></tr>
        <tr><td class="mono">claims-diff</td><td>claims compared with the base revision, when one was given; weakened claims counted here</td>
            <td class="mono">{}</td></tr>
      """.format(
        len([f for f in result.findings if f.rule_id == "empty-body"]),
        len([f for f in result.findings if f.rule_id == "unparseable"]),
        len(result.failed_claims),
        len(result.weakened_claims) if result.claims_diff is not None else "n/a",
    )

    if result.claims_diff is None:
        diff_section = ""
    else:
        diff_rows = "\n".join(
            f"""<tr>
                  <td class="mono">{_esc(c.claim_id)}</td>
                  <td class="mono">{_esc(c.kind)}</td>
                  <td class="mono {_esc('verdict-fail' if c.weakened else '')}">{_esc('YES' if c.weakened else 'no')}</td>
                  <td class="mono faint">{_esc(c.detail)}</td>
                </tr>"""
            for c in result.claims_diff
        ) or '<tr><td colspan="4" class="faint">No claim changed.</td></tr>'
        diff_section = f"""
<h2>Claims changed against {_esc(result.claims_base)}</h2>
<p class="faint">The claims file was compared with the one at the base revision. A removed claim, a
changed expectation or a changed needle is a weakening; a changed command is listed for review.</p>
<table>
  <tr><th>Claim</th><th>Change</th><th>Weakened</th><th>Detail</th></tr>
  {diff_rows}
</table>
"""
    not_covered = NOT_COVERED_ALWAYS + ((NOT_COVERED_TESTS_EXCLUDED,) if result.tests_excluded else ())
    # The test exclusion is stated in the not-covered list above when it
    # applies, so the run note that says the same thing is not repeated.
    notes = "\n".join(f"  <li>{_esc(note)}</li>" for note in result.notes if note != TEST_EXCLUSION_NOTE)

    document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Countersign evidence pack, {_esc(result.run_id)}</title>
<style>{STYLE}</style></head>
<body><div class="sheet">

<p class="mono faint">COUNTERSIGN · EVIDENCE PACK</p>
<h1>Agent work verification</h1>
<p class="{verdict_class} mono" style="font-size:18px; letter-spacing:0.08em;">{verdict_word}</p>

<div class="meta mono">
  <span class="faint">Run reference</span><span>{_esc(result.run_id)}</span>
  <span class="faint">Git commit</span><span>{_esc(commit_label(result))}</span>
  <span class="faint">Generated</span><span>{generated:%d %b %Y %H:%M} UTC</span>
  <span class="faint">Countersign version</span><span>{_esc(__version__)}</span>
  <span class="faint">Files scanned</span><span>{_esc(result.files_scanned)}</span>
  <span class="faint">Config (SHA-256)</span><span>{_esc(result.config_sha256)}</span>
  <span class="faint">Claims file (SHA-256)</span><span>{_esc(result.claims_sha256 or 'none read; the claims check was skipped')}</span>
</div>

<h2>What was found</h2>
<div class="counts">
  <div class="count"><b class="verdict-fail">{len(result.findings)}</b><span class="mono faint">MARKER FINDINGS</span></div>
  <div class="count"><b>{result.exemptions}</b><span class="mono faint">EXEMPTIONS USED</span></div>
  <div class="count"><b class="verdict-fail">{len(result.failed_claims)}</b><span class="mono faint">FAILED CLAIMS</span></div>
</div>
<table>
  <tr><th>Location</th><th>Rule</th><th>Why it is a problem</th><th>Evidence</th></tr>
  {finding_rows}
</table>

<h2>Claims, as declared and as judged</h2>
<table>
  <tr><th>Status</th><th>Claim</th><th>Disproof command</th><th>Exit</th><th>Duration</th></tr>
  {claim_rows}
</table>

{diff_section}
<h2>How it was checked</h2>
<table>
  <tr><th>Check</th><th>What it detects</th><th>Findings</th></tr>
  {method_rows}
</table>
<p class="note">The marker scan is deterministic and re-runnable: the same files under the same
rule versions give the same findings. Claims were run exactly as declared and judged only by
exit code or output, as declared. No model judgement participates in any verdict on this pack.</p>

<h2>What this pack does not cover</h2>
<ul>
{_esc_lines(not_covered)}
</ul>
{f'<ul>{notes}</ul>' if notes else ''}

<h2>Integrity and reproducibility</h2>
<p class="faint">Every check, finding and claim verdict was written to an append-only register,
each entry sealed against the one before it. The run recorded the SHA-256 of the config and
claims files it read, so the same run can be re-derived later from the same files and compared
result for result with <span class="mono">countersign reproduce</span>.</p>
<div class="chain mono">
  <div>Register entry: {_esc(result.register_index)}</div>
  <div>Head: {_esc(result.register_hash)}</div>
</div>

</div>
<footer class="mono">Countersign {_esc(__version__)} · run inside this repository's own environment · Countersign itself made no network requests; claim commands are the declaring team's own</footer>
</body></html>"""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path
