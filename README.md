<!-- audited on 20260903 -->
# Countersign

Countersign checks whether the work a coding agent says it finished was actually finished, and keeps a record of the answer that the agent cannot edit.

It is two things:

- **A GitHub App** for people who do not want to touch a terminal. Install it, merge one pull request, and every change to your repository gets a verdict in plain words. Hosted at [getcountersign.me](https://getcountersign.me).
- **A command line tool**, in this repository, for people who want to run the same checks themselves. Free, open source, no dependencies.

Both use the same engine. Nothing in the verdict involves an AI model.

## The problem

Coding agents (Cursor, Claude Code, Lovable, Bolt, Replit and the rest) finish a task and tell you "Done. All tests pass." Sometimes that is true. Sometimes the code has a `TODO` where the real work should be, returns made-up numbers, or has a function whose body is empty. The agent's message looks the same either way, and reading the code to find out takes longer than asking the agent did.

The agent cannot be the one that certifies its own work. Something outside the agent has to check.

## What Countersign does

On every push and every pull request, in your own GitHub Actions:

1. **Scans the code for unfinished work.** Thirteen kinds of marker agents leave behind (a note for later, made-up data standing in for a real result, "not implemented yet", "coming soon"), plus functions whose body does nothing and explains nothing. Each finding names the file and line.
2. **Runs your claims.** A claim is a sentence about the work paired with the command that would fail if the sentence were false. "The test suite passes" is checked by running the tests. "The pricing endpoint answers" is checked by calling it. The command's exit code decides.
3. **Writes a receipt.** The verdict in plain words, every finding with the line as evidence, every claim as declared and as judged, sealed into a hash-chained log. The GitHub App also keeps a copy outside the repository, where the agent that wrote the code cannot reach it.

The verdict is either **countersigned** or **not countersigned**, and the receipt always says why.

## Use it without a terminal: the GitHub App

1. Open [github.com/apps/countersignapp](https://github.com/apps/countersignapp) and click Install. Pick the repositories.
2. Within a few seconds Countersign opens a pull request in each repository titled "Add Countersign: verify every change, in plain words". It adds three small files and nothing else: the workflow, a configuration, and starter claims read from what is already in the repository (the test script in package.json, a pytest configuration, go.mod, Cargo.toml).
3. Merge that pull request.
4. From then on, every push and pull request gets:
   - a check named `countersign` on the commit,
   - a comment on the pull request, updated in place on later runs,
   - a receipt page, reachable by a link only you have.

A comment looks like this, unedited, from a pull request where the agent had left a note for later, a made-up price and an empty function:

> Not countersigned: 3 places in the code look unfinished.
> src/pricing.js line 1: a note left for later instead of finished work.
> src/pricing.js line 3: made-up data standing in for a real result.
> src/pricing.js line 6: a function that does nothing.
> 1 other claim held: 'The full test suite passes'.

Public repositories are free. Private repositories need a plan: USD 9 a month for a personal account, USD 20 a month for an organisation, covering every private repository of that account. Details at [getcountersign.me/pricing](https://getcountersign.me/pricing).

## Use it from the command line

Requires Python 3.11 or newer. No dependencies, standard library only.

```bash
pip install countersign-cli
cd your-repository
countersign init      # writes countersign.toml, claims.toml and, on GitHub, the workflow
countersign verify    # scans the code, runs the claims, writes the receipt
```

`countersign verify` prints the verdict and exits 0 when countersigned, 1 when not. Example output:

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

The other commands:

```bash
countersign check                        # the receipt log's hash chain, and its head hash
countersign check --expect-head <hash>   # fail unless the head matches a value you pinned elsewhere
countersign reproduce --run <id>         # re-run a recorded run from the same inputs and compare
countersign claims diff --base origin/main   # what changed in claims.toml against a branch
countersign claims from-report done.md   # propose claims from an agent's own "done" message
```

It also runs without installing anything: `PYTHONPATH=/path/to/countersign python3 -m countersign verify`.

The full guide, with a screenshot of every command, is in [docs/guide.md](docs/guide.md).

## Use it in GitHub Actions by hand

If the repository's origin is on github.com, `countersign init` writes `.github/workflows/countersign.yml` for you. To add it to an existing workflow instead:

```yaml
permissions:
  contents: read
  id-token: write        # these two let the action sign the receipt;
  attestations: write    # drop them and set attest: "false" to opt out

steps:
  - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1
  - uses: krishnaflipprr/countersign@v0.3
    with:
      config: countersign.toml          # default
      fail-on: fail                     # or warn: record the verdict without failing the job
      receipts-dir: .countersign        # must match [receipts] dir in the config
      claims-base: ""                   # revision to judge against; pull requests use their exact base commit by default
      approval-label: countersign-approved   # the label a maintainer adds to approve a weakening
      attest: auto                      # auto signs public repositories; true always; false never
```

The action runs the engine straight from its checkout; nothing is fetched from a package index during the run, and every third-party action it uses is pinned to a commit. The verdict lands in the job summary and the receipts upload as an artifact named `countersign-receipts`. Attestation signs the receipt with GitHub Artifact Attestations; it is free for public repositories on every plan, so `auto` signs those and skips private ones, which need GitHub Enterprise Cloud. What the attestation proves, exactly: that this workflow produced this receipt file. The claim commands ran repository code in the same job before the receipt was written, so it does not prove that code could not have influenced the receipt. Countersign kills each claim's process group when the claim ends and copies the receipts out of the workspace the moment verification ends, which closes the easy routes, not every route. The copy the GitHub App keeps is the independent record; a receipt signed inside the job is provenance, not custody.

On pull requests the action judges the change against the exact base commit: the base branch's `countersign.toml` is the policy that counts, and the base's `claims.toml` is what the pull request's claims are compared with. See the next section for what that means.

## Writing claims

Claims live in `claims.toml`. Each one is a statement plus the command that fails if the statement is false.

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

Rules:

- **Three expectations.** `exit 0`: the command must succeed. `nonzero exit`: the command must fail (for "the old endpoint is gone"). `output contains`: the output must contain `needle`.
- **Inputs.** `inputs = ["package.json"]` names the files (or directories) the command's meaning depends on. `npm test` can stay word for word the same while package.json's test script becomes `echo ok`; on a pull request every input is fingerprinted against the base commit and a change is a weakening, exactly like a changed command. The starter claims declare the obvious inputs (package.json, the pytest configuration, go.mod, Cargo.toml, Gemfile). Test directories are not declared by default, since tests change in most pull requests; add them if you want that guarded. Each input's fingerprint is on the receipt.
- **Only these keys.** `id`, `statement`, `command`, `expect`, `needle`, `timeout_s`, `inputs`. Any other key is refused with exit code 2 and a did-you-mean suggestion, because a misspelled `expct` would otherwise silently drop the claim back to `exit 0` and pass.
- **Timeouts.** A claim that runs past `timeout_s` (default 300 seconds) is killed together with everything it started and recorded as timed out, which fails the run.
- **Output on the receipt.** The end of each command's output is kept as evidence, after credential-shaped values (tokens, keys, `Authorization` headers, private keys, labelled passwords) are replaced with `[redacted]`; the receipt counts the replacements. `output = "none"` keeps only the exit code.
- **Commands run with your privileges**, in the repository root, through your shell. Review changes to `claims.toml` the way you review changes to CI configuration.

### Who guards the claims, and the policy

The agent that wrote the code can also edit the claims, the configuration and the workflow, and the quiet way past a gate is to soften the gate rather than fix the code. On a pull request, Countersign enforces the branch the pull request targets, not the pull request:

- **The base policy counts.** `countersign.toml` on the base branch decides what is scanned, which claims are required and whether weakenings fail. The pull request's copy is read, fingerprinted and diffed, but not obeyed. A change that narrows the gate (a scan path or extension removed, a directory newly ignored, a required claim dropped, the claims file removed, `fail_on_weakened` turned off) is a weakening and fails the run.
- **Required claims.** `required = ["tests-pass"]` names claim ids that must exist. A required claim nobody declared is recorded as `MISSING` and fails the run.
- **The claims diff.** The pull request's `claims.toml` is compared with the base's. A removed claim, a changed expectation, a changed needle, a changed command, a dropped input or a changed input file is a weakening and fails the run. A changed command counts because the engine does not try to read shell: `pytest || true` looks like a command, and only a person can say whether the new proof still proves anything.
- **Approval is a label.** A maintainer accepts a weakening by adding the `countersign-approved` label to the pull request. Only people with write access can add labels, so the label is the maintainer's word, and the receipt records that it was used. The label never turns a run that proved nothing into a pass: an empty scan is an error and a missing claims file fails, label or not.
- **Fail closed.** A scan that matches no file is an error, not a pass. A missing claims file fails unless the policy says `optional = true`. Unknown keys in `countersign.toml` are refused like unknown keys in `claims.toml`.
- **The workflow itself.** A pull request can also edit `.github/workflows/countersign.yml`. The engine cannot see that from inside the workflow; the GitHub App can, and its check fails on such a pull request until the label is added. Repositories without the App should protect that file with CODEOWNERS.
- **Claims from the agent's report.** `countersign claims from-report done.md` (or `-` for standard input) turns the checkable sentences of an agent's completion message into proposed claims: "all tests pass" becomes the repository's test command, "created src/pricing.ts" becomes a file check, a URL becomes a request that must succeed. Fixed English patterns, not a model; a sentence whose command cannot be derived is reported as unresolved, never guessed. `--write` appends the proposals to `claims.toml`.

## What the scan looks for

Thirteen rules, eleven of them ported from a gate that ran daily on a production codebase of more than five hundred source files with zero false positives, plus one structural check:

| The receipt says | What was found |
|---|---|
| a note left for later instead of finished work | TODO, FIXME, XXX or HACK |
| code that declares itself unfinished | "not yet implemented", "not implemented yet" |
| code that raises an error instead of doing the work | raise or throw NotImplementedError |
| code that says the work will be done another time | "implemented later", "implemented in a future" |
| code marked as a stand-in | the word stub or stubbed |
| made-up data standing in for a real result | fake, dummy, mock, sample, placeholder or hardcoded data, value, response, result or payload |
| a deliberately incomplete version | "simplified implementation" and the like |
| a description of what the real thing would do | "in a real implementation", "in the real world" |
| a description of work not done | "would be implemented", "would be fetched" |
| text announcing a feature that is not there | "coming soon" |
| an empty result returned as a stand-in | an empty return with a comment saying TODO, placeholder or "for now" |
| a placeholder that stops the program instead of doing the work | Rust's `todo!()` or `unimplemented!()` |
| code that stops with "not implemented" instead of doing the work | a panic, a thrown exception or a raised error whose message says not implemented, in any language |
| a function that does nothing | a body that is only `pass`, `...` or `{}` with no docstring or comment explaining why |

Python is checked through the parser; overloads, abstract methods and Protocol methods are exempt. TypeScript and JavaScript are checked by a scan that understands comments, strings, template literals and regular expressions; constructors, Angular lifecycle hooks, unexported callbacks, `.d.ts` and minified files are exempt. The word rules apply to every language in scope: Python, TypeScript, JavaScript, Go, Rust, Ruby, Java, Kotlin, Swift, PHP, C# and Scala by default.

Test files are excluded by default, because test code legitimately fabricates data, and the receipt says so. A genuine false positive is exempted on the line itself with `countersign: exempt`; every exemption that suppressed a finding is counted on the receipt, and a marker that suppresses nothing is reported as inert.

## Receipts, the register, reproduce

- Every run writes a JSON receipt, a single-file HTML evidence pack and a Markdown summary into `.countersign/`. All three open with the result in plain words. Receipts name the git commit, say whether the working tree had uncommitted changes, and on pull requests carry the hashes of the base policy and base claims the run was judged against and whether the approval label was used, so a signed receipt says which policy it enforced.
- Every run appends one line to `.countersign/register.jsonl`, each carrying the hash of the line before it. Edit any earlier line and `countersign check` says so. What this proves, exactly: that no entry was altered in place. It cannot show entries dropped from the end, so `check` prints the head hash; pin it somewhere the machine does not control and pass it back with `--expect-head` to catch that too. The GitHub App keeps a copy of every receipt outside the repository for the same reason.
- `countersign reproduce --run <id>` re-derives a recorded run from the same inputs and compares, result for result. If the configuration or claims file changed since, you are told.

Exit codes: 0 countersigned or reproduced; 1 not countersigned, register damaged, or not reproduced; 2 usage error, including a configuration or claims file that cannot be honoured as written; 130 interrupted.

## What Countersign is not

Not a security scanner, not a code review, and not a statement that software is fit for any purpose. A countersigned verdict means exactly that the declared checks passed and no marker matched, nothing more. You remain responsible for your code and your claims. The evidence pack states its own limits on every page.

## Try it on planted defects

The `demo/` directory is a small service with defects planted in it: a marker comment, fabricated return data, a function that raises instead of doing work, a body that does nothing, one true claim and one false one.

```bash
countersign verify --config demo/countersign.toml
```

Expected outcome: NOT COUNTERSIGNED, four findings listed, the false claim caught. This repository's own claims file requires the demo to fail; if the demo ever passed, the repository's own run would fail.

## Documentation and support

- User guide with screenshots: [docs/guide.md](docs/guide.md)
- Hosted service, pricing, terms and privacy: [getcountersign.me](https://getcountersign.me)
- Questions and problems: [open an issue](https://github.com/krishnaflipprr/countersign/issues) or write to help@getcountersign.me

## License and origins

Apache License 2.0 (see LICENSE and NOTICE). The command line tool, every scan rule, the claims protocol, the register, receipts, packs and reproduce are open source in full and work offline without the hosted service. The hosted App is a separate service and the engine never depends on it.

The scan rules and their tuning come from a gate that ran daily on a production codebase. The register, reproduce and evidence-pack patterns come from Gaigentic Verify, which applies the same engine to decisions in regulated finance. Countersign itself makes no regulatory claim.
