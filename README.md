<!-- audited on 20260903 -->
# Countersign

**Your agent signs. Countersign proves it.**

Agents report work as done that was never done. A plausible final message becomes the record of what was verified, and nobody can tell the difference between "the tests passed" and "the agent said the tests passed". Countersign is the difference: a deterministic gate that makes every completion claim falsifiable, runs the disproof commands itself, and writes a tamper-evident receipt.

```
$ countersign verify

Countersign run 20260902T104512-a3f8c21e
  commit 4a1b09f0c3d2e1f4a5b6c7d8e9f0a1b2c3d4e5f6 · 214 files scanned · 8120 ms

✗ marker scan: 3 finding(s)
    src/app/page.tsx:41  [unfinished-marker]  // TODO: wire this to the real API
    src/lib/pricing.ts:12  [fabricated-data]  return 49.99  // fake data until billing lands
    src/lib/notify.py:8  [empty-body]  def send_invoice(order_id: str) -> None:
✗ claim tests-pass: The full test suite passes
    command: npm test
    output ends: 2 failing
– note: test files were excluded from the marker scan by policy (exclude_tests = true)

NOT COUNTERSIGNED · 3 finding(s), 1 failed claim(s)
The work did not pass its own declared checks. Fix the code or the claims.
```

## Why this exists

Three things became true at once in 2026:

1. Most new code is written by agents, and agents report success for work that was never finished, plausibly and at scale.
2. Teams ship that code anyway, because "looks done" and "is done" stopped being distinguishable by reading a pull request.
3. Vendors bolted self-verification onto their own agents, which is the fox counting the chickens. An audit trail written by the system being audited proves what the system says it did. Independence is structural, not a feature.

Countersign is independent by construction: it runs outside the agent, checks deterministically, records everything to a hash-chained register, and refuses to countersign work that did not pass its own declared checks. No model judgement participates in any verdict.

## The claims protocol

An agent (or a human) declares what is true about the work in `claims.toml`. Each claim carries the command that fails if the claim is false:

```toml
[[claim]]
id = "tests-pass"
statement = "The full test suite passes"
command = "npm test"
expect = "exit 0"

[[claim]]
id = "pricing-is-live"
statement = "The pricing endpoint returns real numbers, not seed data"
command = "curl -sf http://localhost:8000/api/pricing"
expect = "output contains"
needle = "unit_price"
```

If nobody can say what command would disprove the claim, the claim was not a claim. Three expectations are supported: `exit 0`, `nonzero exit` (negative tests), and `output contains`. A claim that runs past its timeout is killed together with everything it spawned and recorded as timed out, which fails the gate.

Claim commands run through your shell, in the repository root, with your privileges. Treat `claims.toml` like any other executable file in the repository: review changes to it the way you review changes to CI configuration.

## Who guards the claims

The agent that wrote the code can also write the claims, and the quiet way past a gate is not to fix the code but to soften the claim. Three things make that visible:

- **Required claims.** `required = ["tests-pass"]` in `countersign.toml` names claim ids that must be declared. A required claim nobody wrote is recorded as `MISSING` and fails the gate, so deleting the claim is not a way out.
- **Claims diff.** `countersign verify --claims-base origin/main` (the GitHub action does this on every pull request) compares `claims.toml` with the base branch and names every change. A removed claim, a changed expectation or a changed needle is a weakening and fails the gate (`fail_on_weakened = true`); a changed command is listed for the reviewer. `countersign claims diff --base origin/main` prints the same diff on its own.
- **Starter claims.** `countersign init` reads the build files that are actually there (package.json scripts, pytest or ruff configuration, go.mod, Cargo.toml) and writes a `claims.toml` with the stack's own test, lint and type-check commands, marking `tests-pass` as required. Nothing is guessed; a repository with no recognised build files gets a commented example.

## The marker scan

Eleven rules ported from a gate that ran daily on a production tree of more than 500 source files, reviewed file by file, with zero false positives, plus a structural check for functions whose body does nothing and explains nothing, which catches unfinished work that forgot to advertise itself. A bare `pass`, `...` or `{}` is a stub; the same body with a docstring or a comment is a documented decision (an irreversible migration's `downgrade`, a `close()` with nothing to close) and is not reported. Python is checked through the parser (overloads, abstract and Protocol methods are exempt). TypeScript and JavaScript are checked by a comment-and-string-aware scan of function declarations, class and object methods, and exported arrow functions (constructors, Angular lifecycle hooks, unexported callbacks, `.d.ts` and minified files are exempt). The eleven marker rules apply to every language in scope.

Point `paths` at production source (the original gate covered `src/`, the dashboard and the clients, not seed scripts or migrations); prose about stubs in a seed script is not a stub. Test files are excluded by policy: test code legitimately fabricates data, and the receipt says so. A genuine false positive is exempted in the source itself, on the line, where a reviewer sees it. Every exemption that suppressed a finding is counted on the receipt; a marker that suppresses nothing is reported as inert so a stale one cannot hide.

## Receipts, register, reproduce

- Every run appends to `.countersign/register.jsonl`: an append-only, hash-chained log. Edit any earlier line and `countersign check` says so. Appends are locked, so two runs on one checkout cannot break the chain by racing.
- What the register proves, exactly: that no entry was altered after it was written by anyone who did not also rewrite every entry after it. It lives on the machine that ran the checks, so on its own it is evidence against accident and against third parties, not against the machine's owner. Tamper evidence against the owner requires the register head to be anchored outside the machine, which is what a hosted anchoring service is for.
- Every run writes a JSON receipt and, unless asked not to, a single-file HTML evidence pack: what was checked, how, what was found, what was not covered. Receipts name the git commit and say whether the working tree had uncommitted changes when it was scanned.
- `countersign reproduce --run <id>` re-derives a recorded run from the same inputs and compares, result for result. The run recorded the SHA-256 of the config and claims files it read; if they changed, you are told.

Exit codes: 0 countersigned or reproduced, 1 not countersigned (or the register is damaged, or the run did not reproduce), 2 usage error including a config or claims file that cannot be honoured as written, 130 interrupted.

## Install and run

```bash
pip install countersign-cli
countersign init         # writes countersign.toml and a starter claims.toml
countersign verify       # scan + claims gate; writes receipt, pack, register
countersign check        # the register's hash chain
countersign reproduce --run <id>
countersign claims diff --base origin/main
```

The user guide, with screenshots of every command and the evidence pack, is in [docs/guide.md](docs/guide.md).

Requires Python 3.11+. Zero dependencies, standard library only, on purpose: it has to run inside any CI runner, any locked-down laptop, any air-gapped environment, with no supply-chain conversation. It also runs without installing: `PYTHONPATH=/path/to/countersign python3 -m countersign verify`.

## CI usage (GitHub Actions)

When the repository's origin is on github.com, `countersign init` also writes `.github/workflows/countersign.yml`. Commit it and push; from then on every push to the default branch and every pull request runs Countersign, and pull requests get the claims diff against their base branch. The file it writes is this:

```yaml
- uses: gaigenticai/countersign@v0.1
  with:
    config: countersign.toml
```

The action runs Countersign straight from its checkout (no pip install, nothing fetched from PyPI). The verdict lands in the job step summary; receipts upload as artifacts. Set `fail-on: warn` to record without failing. If your config moves the receipts directory, set `receipts-dir` to match. Workflows run on the account that owns the repository, on its Actions minutes.

## What Countersign is not

Not a security scanner, not a code review, not a statement of fitness for any purpose. It verifies declared claims deterministically and scans for unfinished-work markers. The evidence pack states its own limits on every page.

## Try the demo

The `demo/` directory is a small accounts service with defects planted in it: a marker comment, fabricated return data, a function that raises instead of doing work, a body that does nothing, plus one true claim and one false one.

```bash
countersign verify --config demo/countersign.toml
```

Expected outcome: NOT COUNTERSIGNED, four findings listed, the false claim caught.

## Status and roadmap

v0.1. Working: marker scan, structural Python check, claims protocol, register, receipts, evidence packs, reproduce, CI action. Next: wiring cross-checks (frontend fields against the backend endpoints that feed them), agent-report parsing (verify the claims in an agent's own completion message), public receipt badges, hosted receipt verification. The regulated-industry lineage (EU AI Act Article 12 record-keeping evidence) is the same engine pointed at a different buyer.

## License

Apache License 2.0 (see LICENSE and NOTICE). The command line tool, every scan rule, the claims protocol, the register, receipts, packs and reproduce are open source in full and stay fully functional offline. Hosted anchoring, badges and organisation features are a separate service.
