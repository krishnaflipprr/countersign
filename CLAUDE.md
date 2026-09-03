<!-- audited on 20260903 -->
# Countersign: working rules

Countersign is a product, not a project. It is the productized form of the
God Audit methodology and Gaigentic Verify's evidence engine. Read this
before working in the directory.

## What this is

A zero-dependency CLI (Python 3.11+, standard library only) that makes AI
agent completion claims falsifiable: a marker scan ported from Argus's
certified gate, a claims protocol where every claim carries its disproof
command, a hash-chained evidence register adapted from Verify, receipts,
HTML evidence packs, and reproduce. Plus a GitHub Action wrapper.

## Hard rules

1. **This repo passes its own gate.** `countersign verify` at the root must
   exit 0. The engine's own pattern table legitimately carries line
   exemptions on the five rule lines whose own pattern matches them (a rule
   whose regex uses alternation does not match its own source and needs no
   marker); those five are counted on every receipt, pinned by a claim in
   claims.toml, and must never be used anywhere else. A new exemption
   anywhere else in `countersign/` is a finding to justify in conversation,
   not a habit. Two further lines (the marker's definition in config.py and
   the init template in cli.py) contain the marker text without exempting
   anything; receipts report them as inert.
2. **No dependencies. Ever.** Standard library only. A dependency is a
   supply-chain conversation with a locked-down customer.
3. **No model judgement in any verdict.** Deterministic checks only. A
   verdict a team cannot argue with is worth more than a clever one.
4. **Tests before behavior changes to a check.** A wrong finding shipped to
   users is worse than no finding. `python3 -m unittest discover -s tests -t .`
5. **Honest receipts.** A skipped check prints as skipped. The pack states
   what it does not cover. Copy never says "certified" or "audited"; the
   product countersigns runs, it does not certify software.
6. **No em dashes in user-facing copy** (README, receipt, pack, CLI output),
   parent house rule.
7. **The claims file is the dogfood.** The repo's own claims.toml is
   executable proof the product works on itself; keep it current when
   behavior changes.

## Commands

```bash
python3 -m unittest discover -s tests -t .   # tests
python3 -m countersign verify                # the gate on this repo
python3 -m countersign verify --config demo/countersign.toml   # must FAIL (planted defects)
python3 -m countersign check                 # register chain
```

The demo failing its own gate is a claim in claims.toml; if the demo ever
passes, `countersign verify` at the root fails. That is the demo doing its
job.

## Origins (credit where due)

- Marker rules and zero-false-positive tuning: Argus `scripts/check-no-stubs.sh`
  and the gate discipline of `scripts/verify.sh`.
- Register, reproduce, evidence-pack patterns and honesty rules: Gaigentic
  Verify (`verify/register.py`, `reproduce.py`, `pack.py`).

Both were production-certified file by file; this repo inherits that
standard. New files are production-grade the day they land: real data flow
end to end, no markers, and it runs.

## Open decisions (Krishna's call)

- License for public launch (permissive OSS vs source-available). LICENSE is
  a placeholder until decided.
- Package name availability on PyPI and the GitHub org name, before launch.
- Pricing shape of the eventual hosted receipts service; nothing here
  depends on it.
