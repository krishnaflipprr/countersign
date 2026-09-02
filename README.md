# Countersign

**Your agent signs. Countersign proves it.**

Agents report work as done that was never done. A plausible final message becomes the record of what was verified, and nobody can tell the difference between "the tests passed" and "the agent said the tests passed". Countersign is the difference: a deterministic gate that makes every completion claim falsifiable, runs the disproof commands itself, and writes a tamper-evident receipt.

```
$ countersign verify

Countersign run 20260902T104512-a3f8c21e
  commit 4a1b09f... · 214 files scanned · 8 seconds

✗ marker scan: 3 finding(s)
    src/app/page.tsx:41  [unfinished-marker]  // TODO: wire this to the real API
    src/lib/pricing.ts:12  [fabricated-data]  return 49.99  // fake data until billing lands
    src/lib/notify.ts:8  [empty-body]  function sendInvoice() {}
✗ claim tests-pass: The full test suite passes
    command: npm test
    output ends: 2 failing

NOT COUNTERSIGNED · 3 finding(s), 1 failed claim(s)
```

## Why this exists

Three things became true at once in 2026:

1. Most new code is written by agents, and agents have a documented false-completion problem: they report success for work that was never finished, plausibly and at scale.
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
command = "curl -sf http://localhost:8000/api/pricing | grep -q unit_price"
expect = "output contains"
needle = "unit_price"
```

If nobody can say what command would disprove the claim, the claim was not a claim. Three expectations are supported: `exit 0`, `nonzero exit` (negative tests), and `output contains`.

## The marker scan

Eleven rules ported from a gate that ran daily on a 546-file production-certified tree with zero false positives, plus a structural Python check for functions whose body does nothing (bare `pass` or `...`), which catches unfinished work that forgot to advertise itself. Test files are excluded by policy: test code legitimately fabricates data, and the receipt says so. A genuine false positive is exempted in the source itself, on the line, where a reviewer sees it, and every exemption is counted on the receipt.

## Receipts, register, reproduce

- Every run appends to `.countersign/register.jsonl`: an append-only, hash-chained log. Edit any earlier line and `countersign check` says so.
- Every run writes a JSON receipt and a single-file HTML evidence pack: what was checked, how, what was found, what was not covered.
- `countersign reproduce --run <id>` re-derives a recorded run from the same inputs and compares, result for result. The run recorded the SHA-256 of the config and claims files it read; if they changed, you are told.

## Install and run

```bash
pip install .            # from this repository
countersign init         # writes countersign.toml
countersign verify       # scan + claims gate; writes receipt, pack, register
```

Requires Python 3.11+. Zero dependencies, standard library only, on purpose: it has to run inside any CI runner, any locked-down laptop, any air-gapped environment, with no supply-chain conversation.

## CI usage (GitHub Actions)

```yaml
- uses: gaigenticai/countersign@v0.1
  with:
    config: countersign.toml
```

The verdict lands in the job step summary; receipts upload as artifacts. Set `fail-on: warn` to record without failing.

## What Countersign is not

Not a security scanner, not a code review, not a certification of fitness for any purpose. It verifies declared claims deterministically and scans for unfinished-work markers. The evidence pack states its own limits on every page.

## Try the demo

The `demo/` directory is a small accounts service with defects planted in it: a marker comment, fabricated return data, a function that raises instead of doing work, a body that does nothing, plus one true claim and one false one.

```bash
countersign verify --config demo/countersign.toml
```

Expected outcome: NOT COUNTERSIGNED, findings listed, the false claim caught.

## Status and roadmap

v0.1. Working: marker scan, structural Python check, claims protocol, register, receipts, evidence packs, reproduce, CI action. Next: wiring cross-checks (frontend fields against the backend endpoints that feed them), agent-report parsing (verify the claims in an agent's own completion message), public receipt badges, hosted receipt verification. The bank-grade lineage (EU AI Act Article 12 logging evidence) is the same engine pointed at a different buyer.

## License

Proprietary, all rights reserved (see LICENSE). The license decision for the public launch (permissive open source vs source-available) is deliberately not made yet; nothing in this repository may be redistributed until it is.
