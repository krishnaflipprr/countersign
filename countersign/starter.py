# audited on 20260903
"""Starter claims for ``countersign init``: what this repository can already
prove about itself, read from the build files that are actually there.

Nothing here guesses. A claim is proposed only when the file that makes its
command meaningful exists (a ``test`` script in package.json, a pytest
configuration, a go.mod). The proposed commands are the stack's own
conventional ones; the team edits them like any other claim.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

TESTS_PASS = "tests-pass"

# Where customers' workflows point. Moves only with a release; the tag it
# names must exist on that repository before this constant changes.
ACTION_REF = "krishnaflipprr/countersign@v0.1"
WORKFLOW_RELATIVE_PATH = Path(".github") / "workflows" / "countersign.yml"


@dataclass(frozen=True)
class GitHubRepository:
    toplevel: Path
    default_branch: str


def _git(root: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def detect_github_repository(root: Path) -> GitHubRepository | None:
    """The repository around ``root`` when its origin is on github.com.

    Nothing is guessed: no git, no origin, or an origin elsewhere means None.
    The default branch comes from origin's HEAD when the clone knows it,
    else from the current branch, else ``main``.
    """
    toplevel = _git(root, "rev-parse", "--show-toplevel")
    if not toplevel:
        return None
    origin = _git(root, "remote", "get-url", "origin")
    if not origin or "github.com" not in origin:
        return None
    head = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head and head.startswith("origin/"):
        branch = head[len("origin/"):]
    else:
        branch = _git(root, "branch", "--show-current") or "main"
    return GitHubRepository(Path(toplevel).resolve(), branch or "main")


def render_workflow(config_path_in_repo: str, default_branch: str) -> str:
    return f"""# Countersign: verifies every push to {default_branch} and every pull request.
# Written by `countersign init`. Safe to edit; the action's inputs are
# documented at https://github.com/{ACTION_REF.split('@')[0]}.
name: countersign
on:
  push:
    branches: [{json.dumps(default_branch)}]
  pull_request:

permissions:
  contents: read

jobs:
  countersign:
    name: countersign verify
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: {ACTION_REF}
        with:
          config: {json.dumps(config_path_in_repo)}
"""


@dataclass(frozen=True)
class StarterClaim:
    claim_id: str
    statement: str
    command: str
    source: str


def _package_manager(root: Path) -> str:
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "bun.lockb").exists() or (root / "bun.lock").exists():
        return "bun"
    return "npm"


def _node_claims(root: Path) -> list[StarterClaim]:
    package_json = root / "package.json"
    if not package_json.is_file():
        return []
    try:
        data = json.loads(package_json.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    scripts = data.get("scripts") if isinstance(data, dict) else None
    if not isinstance(scripts, dict):
        return []
    manager = _package_manager(root)
    run = f"{manager} run"
    # `bun test` is bun's own runner, not the package's test script; every
    # other manager treats `<manager> test` as the script.
    test_command = "bun run test" if manager == "bun" else f"{manager} test"
    claims: list[StarterClaim] = []
    if isinstance(scripts.get("test"), str) and scripts["test"].strip():
        claims.append(StarterClaim(TESTS_PASS, "The full test suite passes", test_command, "package.json scripts.test"))
    if isinstance(scripts.get("lint"), str) and scripts["lint"].strip():
        claims.append(StarterClaim("lint-clean", "The linter reports nothing", f"{run} lint", "package.json scripts.lint"))
    for name in ("typecheck", "type-check", "tsc"):
        if isinstance(scripts.get(name), str) and scripts[name].strip():
            claims.append(StarterClaim("types-check", "The type checker reports nothing", f"{run} {name}", f"package.json scripts.{name}"))
            break
    else:
        deps = {}
        for key in ("devDependencies", "dependencies"):
            if isinstance(data.get(key), dict):
                deps.update(data[key])
        if "typescript" in deps and (root / "tsconfig.json").is_file():
            claims.append(StarterClaim("types-check", "The type checker reports nothing", "npx tsc --noEmit", "tsconfig.json with typescript installed"))
    return claims


def _python_claims(root: Path) -> list[StarterClaim]:
    pyproject = root / "pyproject.toml"
    pyproject_text = ""
    if pyproject.is_file():
        try:
            pyproject_text = pyproject.read_text(encoding="utf-8-sig")
        except OSError:
            pyproject_text = ""
    has_python = bool(pyproject_text) or (root / "setup.py").is_file() or (root / "setup.cfg").is_file() or (root / "requirements.txt").is_file()
    claims: list[StarterClaim] = []
    pytest_configured = (
        "[tool.pytest" in pyproject_text
        or re.search(r"\bpytest\b", pyproject_text) is not None
        or (root / "pytest.ini").is_file()
        or (root / "conftest.py").is_file()
    )
    if pytest_configured:
        claims.append(StarterClaim(TESTS_PASS, "The full test suite passes", "python3 -m pytest -q", "pytest configuration"))
    elif has_python and ((root / "tests").is_dir() or (root / "test").is_dir()):
        start = "tests" if (root / "tests").is_dir() else "test"
        claims.append(StarterClaim(TESTS_PASS, "The full test suite passes", f"python3 -m unittest discover -s {start} -t .", f"{start}/ directory"))
    if "[tool.ruff" in pyproject_text or (root / "ruff.toml").is_file() or (root / ".ruff.toml").is_file():
        claims.append(StarterClaim("lint-clean", "The linter reports nothing", "ruff check .", "ruff configuration"))
    if "[tool.mypy" in pyproject_text or (root / "mypy.ini").is_file():
        claims.append(StarterClaim("types-check", "The type checker reports nothing", "mypy .", "mypy configuration"))
    return claims


def _go_claims(root: Path) -> list[StarterClaim]:
    if not (root / "go.mod").is_file():
        return []
    return [
        StarterClaim(TESTS_PASS, "The full test suite passes", "go test ./...", "go.mod"),
        StarterClaim("vet-clean", "go vet reports nothing", "go vet ./...", "go.mod"),
    ]


def _rust_claims(root: Path) -> list[StarterClaim]:
    if not (root / "Cargo.toml").is_file():
        return []
    return [StarterClaim(TESTS_PASS, "The full test suite passes", "cargo test", "Cargo.toml")]


def _ruby_claims(root: Path) -> list[StarterClaim]:
    if (root / "Gemfile").is_file() and (root / "spec").is_dir():
        return [StarterClaim(TESTS_PASS, "The full test suite passes", "bundle exec rspec", "Gemfile with spec/")]
    return []


def detect_starter_claims(root: Path) -> list[StarterClaim]:
    """Starter claims for ``root``, at most one per claim id, first stack wins."""
    root = Path(root)
    seen: set[str] = set()
    claims: list[StarterClaim] = []
    for detector in (_node_claims, _python_claims, _go_claims, _rust_claims, _ruby_claims):
        for claim in detector(root):
            if claim.claim_id in seen:
                continue
            seen.add(claim.claim_id)
            claims.append(claim)
    return claims


def _toml_string(value: str) -> str:
    return json.dumps(value)


def render_claims_toml(claims: list[StarterClaim]) -> str:
    lines = [
        "# What is true about this repository, each claim paired with the command",
        "# that fails if the claim is false. Countersign runs every command from",
        "# the repository root through your shell and judges it exactly as declared.",
        "#",
        '# expect = "exit 0"           the command must succeed (default)',
        '# expect = "nonzero exit"     the command must fail (negative tests)',
        '# expect = "output contains"  the needle must appear in the combined output',
        "",
    ]
    if not claims:
        lines += [
            "# No build files were recognised, so no claim was written for you.",
            "# Declare your first claim by editing the example below.",
            "#",
            "# [[claim]]",
            '# id = "tests-pass"',
            '# statement = "The full test suite passes"',
            '# command = "make test"',
            '# expect = "exit 0"',
            "",
        ]
    for claim in claims:
        lines += [
            f"# proposed from {claim.source}",
            "[[claim]]",
            f"id = {_toml_string(claim.claim_id)}",
            f"statement = {_toml_string(claim.statement)}",
            f"command = {_toml_string(claim.command)}",
            'expect = "exit 0"',
            "",
        ]
    return "\n".join(lines)
