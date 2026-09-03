# audited on 20260903
"""The command line.

Five verbs, no config ceremony to start:

  countersign init                    write countersign.toml, a starter claims.toml and, on GitHub, the workflow
  countersign verify                  run the gate, write receipt + pack
  countersign check                   verify the evidence register's chain
  countersign reproduce --run ID      re-derive a recorded run
  countersign claims diff --base REF  what changed in the claims file, weakenings named

Exit codes: 0 clean or reproduced, 1 the work did not pass (or the register
is damaged, or the run did not reproduce), 2 usage error (including a config
or claims file that cannot be honoured), 130 interrupted. A CI system can
trust the exit code; a human should read the receipt.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .claims import ClaimsError, load_claims
from .config import Config, ConfigError
from .engine import FAIL_VERDICT, run_gate
from .pack import build_pack
from .receipt import markdown_summary, receipt_json, terminal_summary, write_receipt
from .claimsdiff import diff_against_ref
from .register import Register, RegisterDamaged
from .reproduce import reproduce_run
from .starter import WORKFLOW_RELATIVE_PATH, detect_github_repository, detect_starter_claims, render_claims_toml, render_workflow

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130

DEFAULT_CONFIG_TEMPLATE = """\
# Countersign: deterministic verification of agent completion claims.
# Docs: the README in this repository. Everything below has a working default.

[scan]
# Which paths to scan for unfinished-work markers. Relative to this file.
# A path that does not exist is an error, not an empty scan.
paths = ["."]
# Directories never scanned. The defaults cover build output and vendored
# dependencies; add yours here (the defaults are replaced, so keep the ones
# you want).
ignore_dirs = [
  ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
  ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", "target",
  ".next", ".nuxt", ".wrangler", ".cache", ".countersign", "coverage", ".tox",
  ".idea", ".vscode", "vendor",
]
# File extensions scanned. Only add extensions where the rules behave.
extensions = [
  ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".rb",
  ".java", ".kt", ".swift", ".php", ".cs", ".scala",
]
# Append this marker to a line that is a genuine false positive.
exempt_marker = "countersign: exempt"
# Test files legitimately fabricate data; they are excluded by default.
exclude_tests = true

[claims]
# Declaration of what is true about this repository, each claim with the
# command that fails if the claim is false, in claims.toml next to this
# file. Set file = "" to run scan-only (reported as skipped, not passed).
file = "claims.toml"
# Claim ids that must be declared. A required claim nobody wrote is recorded
# as missing and fails the gate, so the standard cannot be lowered by
# deleting the claim.
required = {required}
# When verify is given a base revision (--claims-base, which the GitHub
# action does on pull requests), a removed claim or a changed expectation
# or needle is a weakening. true fails the gate on it; false only records it.
fail_on_weakened = true

[receipts]
# Where receipts, the register and evidence packs are written.
dir = ".countersign"

[run]
# Per-claim command timeout, seconds. A claim that runs longer is killed,
# with everything it spawned, and recorded as timed out.
timeout_s = 300
# How much captured command output a receipt keeps, in characters. Longer
# output is kept from both ends with the middle cut out.
max_output_bytes = 20000
"""


def render_config(required: list[str]) -> str:
    return DEFAULT_CONFIG_TEMPLATE.replace("{required}", json.dumps(required))


def _cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.config).resolve()
    if target.exists() and not args.force:
        print(f"{target} already exists; use --force to overwrite", file=sys.stderr)
        return EXIT_USAGE
    root = target.parent
    root.mkdir(parents=True, exist_ok=True)
    starters = detect_starter_claims(root)
    required = [c.claim_id for c in starters if c.claim_id == "tests-pass"]
    target.write_text(render_config(required), encoding="utf-8")
    print(f"wrote {target}")

    claims_target = root / "claims.toml"
    if claims_target.exists():
        print(f"kept existing {claims_target}")
    else:
        claims_target.write_text(render_claims_toml(starters), encoding="utf-8")
        print(f"wrote {claims_target}")
    for claim in starters:
        print(f"  proposed claim {claim.claim_id}: {claim.command}  (from {claim.source})")
    if not starters:
        print("  no build files recognised; claims.toml holds a commented example to edit")
    if required:
        print(f"  required in countersign.toml: {', '.join(required)}")

    workflow_status = _write_workflow(root, target, args)
    if workflow_status is not None:
        return workflow_status
    print("next: review claims.toml, then run: countersign verify")
    return EXIT_OK


def _write_workflow(root: Path, config_target: Path, args: argparse.Namespace) -> int | None:
    """Write the GitHub Actions workflow when the repository lives on GitHub.

    Returns an exit code to stop with, or None to carry on. Written by
    default when origin is on github.com; ``--github`` insists (and is a
    usage error when there is no GitHub repository to write into);
    ``--no-github`` skips.
    """
    if args.no_github:
        return None
    repository = detect_github_repository(root)
    if repository is None:
        if args.github:
            print("cannot write a workflow: this directory is not inside a git repository whose origin is on github.com", file=sys.stderr)
            return EXIT_USAGE
        print("  no GitHub origin found; no workflow written (run again with --github once the repository is on GitHub)")
        return None
    workflow_path = repository.toplevel / WORKFLOW_RELATIVE_PATH
    if workflow_path.exists() and not args.force:
        print(f"kept existing {workflow_path}")
        return None
    config_in_repo = config_target.resolve().relative_to(repository.toplevel).as_posix()
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(render_workflow(config_in_repo, repository.default_branch), encoding="utf-8")
    print(f"wrote {workflow_path}")
    print(f"  runs countersign verify on every push to {repository.default_branch} and every pull request; commit it and push")
    return None


def _use_color(args: argparse.Namespace) -> bool:
    if args.no_color:
        return False
    if os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def _load_config(config_path: Path) -> Config | None:
    try:
        return Config.load(config_path)
    except ConfigError as exc:
        print(f"config cannot be used as written: {exc}", file=sys.stderr)
        return None


def _cmd_verify(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"no config at {config_path}; run: countersign init", file=sys.stderr)
        return EXIT_USAGE
    config = _load_config(config_path)
    if config is None:
        return EXIT_USAGE
    if args.no_claims:
        config.claims_file = None

    register = Register(config.register_path())
    try:
        intact, chain_note = register.verify_chain()
    except OSError as exc:
        print(f"the evidence register cannot be read: {exc}", file=sys.stderr)
        return EXIT_FAIL
    if not intact:
        print(f"the evidence register is damaged: {chain_note}", file=sys.stderr)
        print("nothing can be countersigned on top of a broken chain; investigate before running again", file=sys.stderr)
        return EXIT_FAIL

    try:
        result = run_gate(config, register=register, claims_base=args.claims_base or None)
    except (ConfigError, ClaimsError) as exc:
        print(f"verification could not run: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (RegisterDamaged, OSError) as exc:
        print(f"verification could not run: {exc}", file=sys.stderr)
        return EXIT_FAIL

    receipt_path = write_receipt(result, config.receipts_root() / f"{result.run_id}.json")
    pack_path = None
    if not args.no_pack:
        pack_path = build_pack(result, config.receipts_root() / f"{result.run_id}.html")
    summary_path = None
    if args.summary_file:
        summary_path = Path(args.summary_file)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(markdown_summary(result), encoding="utf-8")

    if args.json:
        print(json.dumps(receipt_json(result), indent=2, sort_keys=True))
    else:
        print(terminal_summary(result, use_color=_use_color(args)))
        print(f"\nreceipt: {receipt_path}")
        if pack_path:
            print(f"pack:    {pack_path}")
        if summary_path:
            print(f"summary: {summary_path}")

    return EXIT_FAIL if result.verdict == FAIL_VERDICT else EXIT_OK


def _cmd_check(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    config = _load_config(config_path)
    if config is None:
        return EXIT_USAGE
    register = Register(config.register_path())
    try:
        intact, note = register.verify_chain()
    except OSError as exc:
        print(f"the evidence register cannot be read: {exc}", file=sys.stderr)
        return EXIT_FAIL
    print(f"{config.register_path()}: {note}")
    return EXIT_OK if intact else EXIT_FAIL


def _cmd_reproduce(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"no config at {config_path}", file=sys.stderr)
        return EXIT_USAGE
    config = _load_config(config_path)
    if config is None:
        return EXIT_USAGE
    try:
        reproduced, notes = reproduce_run(config, args.run)
    except OSError as exc:
        print(f"reproduce could not run: {exc}", file=sys.stderr)
        return EXIT_FAIL
    for note in notes:
        print(note)
    return EXIT_OK if reproduced else EXIT_FAIL


def _cmd_claims_diff(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"no config at {config_path}", file=sys.stderr)
        return EXIT_USAGE
    config = _load_config(config_path)
    if config is None:
        return EXIT_USAGE
    if not config.claims_file:
        print("no claims file is configured; nothing to diff", file=sys.stderr)
        return EXIT_USAGE
    try:
        head = load_claims(config.claims_path())
        changes, problem = diff_against_ref(config.root, args.base, config.claims_file, head)
    except ClaimsError as exc:
        print(f"claims diff could not run: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if problem:
        print(f"note: {problem}; every current claim is shown as added")
    if not changes:
        print(f"claims unchanged against {args.base}")
        return EXIT_OK
    weakened = [c for c in changes if c.weakened]
    print(f"{len(changes)} change(s) against {args.base}, {len(weakened)} weakened")
    for change in changes:
        flag = "WEAKENED " if change.weakened else ""
        print(f"  {flag}{change.kind} {change.claim_id}: {change.detail}")
    if weakened and config.fail_on_weakened:
        return EXIT_FAIL
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="countersign",
        description="Your agent signs. Countersign proves it. Deterministic verification of agent completion claims.",
    )
    parser.add_argument("--version", action="version", version=f"countersign {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="write a countersign.toml for this repository")
    p_init.add_argument("--config", default="countersign.toml", help="config path (default: countersign.toml)")
    p_init.add_argument("--force", action="store_true", help="overwrite an existing config (and workflow)")
    p_init.add_argument("--github", action="store_true", help="insist on writing the GitHub Actions workflow; an error when no GitHub repository is found")
    p_init.add_argument("--no-github", action="store_true", help="do not write a GitHub Actions workflow even when origin is on github.com")
    p_init.set_defaults(func=_cmd_init)

    p_verify = sub.add_parser("verify", help="run the gate; write receipt, pack and register entries")
    p_verify.add_argument("--config", default="countersign.toml", help="config path (default: countersign.toml)")
    p_verify.add_argument("--json", action="store_true", help="print the receipt JSON instead of the terminal summary")
    p_verify.add_argument("--no-pack", action="store_true", help="skip writing the HTML evidence pack")
    p_verify.add_argument("--no-claims", action="store_true", help="run the marker scan only; skip the claims check (reported as skipped)")
    p_verify.add_argument("--no-color", action="store_true", help="disable colored output")
    p_verify.add_argument("--summary-file", default=None, help="also write a Markdown summary to this path")
    p_verify.add_argument("--claims-base", default=None, metavar="REF", help="git revision to diff the claims file against; weakened claims fail the gate unless the config says otherwise")
    p_verify.set_defaults(func=_cmd_verify)

    p_check = sub.add_parser("check", help="verify the evidence register's hash chain")
    p_check.add_argument("--config", default="countersign.toml", help="config path (default: countersign.toml)")
    p_check.set_defaults(func=_cmd_check)

    p_repro = sub.add_parser("reproduce", help="re-derive a recorded run and compare")
    p_repro.add_argument("--config", default="countersign.toml", help="config path (default: countersign.toml)")
    p_repro.add_argument("--run", required=True, help="run id from the receipt filename")
    p_repro.set_defaults(func=_cmd_reproduce)

    p_claims = sub.add_parser("claims", help="work with the claims file")
    claims_sub = p_claims.add_subparsers(dest="claims_command", required=True)
    p_diff = claims_sub.add_parser("diff", help="what changed in the claims file against a git revision, weakenings named")
    p_diff.add_argument("--config", default="countersign.toml", help="config path (default: countersign.toml)")
    p_diff.add_argument("--base", required=True, metavar="REF", help="git revision to compare with, for example origin/main")
    p_diff.set_defaults(func=_cmd_claims_diff)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(main())
