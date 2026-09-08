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
ACTION_REF = "krishnaflipprr/countersign@v0.3"
# Third-party actions are pinned to the commit behind the tag. The tag is
# kept as a comment so Dependabot can move both together.
CHECKOUT_REF = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1  # v7.0.1"
SETUP_PYTHON_REF = "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97  # v7.0.0"
SETUP_NODE_REF = "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020  # v7.0.0"
SETUP_BUN_REF = "oven-sh/setup-bun@0c5077e51419868618aeaa5fe8019c62421857d6  # v2.2.0"
SETUP_GO_REF = "actions/setup-go@b7ad1dad31e06c5925ef5d2fc7ad053ef454303e  # v7.0.0"
SETUP_RUBY_REF = "ruby/setup-ruby@95ef2b042f9d7a56d8268cba8559e2842e2ad01b  # v1.321.0"
# The Python the action itself runs on. The workflow sets up the same one
# before installing anything, so what pip installs is what the claims see.
ACTION_PYTHON = "3.12"
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


def detect_setup(root: Path) -> list[str]:
    """The workflow steps that install what the starter claims need, one
    YAML list item per entry, unindented. A claim like ``python3 -m pytest``
    cannot hold on a runner where pytest was never installed; the setup
    steps are derived from the same files the claims are, so the workflow
    works on the first push."""
    root = Path(root)
    steps: list[str] = []

    package_json = root / "package.json"
    if package_json.is_file():
        manager = _package_manager(root)
        if manager == "bun":
            steps.append(f"- uses: {SETUP_BUN_REF}")
            steps.append("- run: bun install")
        else:
            steps.append(f"- uses: {SETUP_NODE_REF}\n  with:\n    node-version: \"22\"")
            if manager == "pnpm":
                steps.append("- run: corepack enable && pnpm install --frozen-lockfile")
            elif manager == "yarn":
                steps.append("- run: corepack enable && yarn install --frozen-lockfile")
            elif (root / "package-lock.json").is_file():
                steps.append("- run: npm ci")
            else:
                steps.append("- run: npm install")

    pyproject_text = ""
    if (root / "pyproject.toml").is_file():
        try:
            pyproject_text = (root / "pyproject.toml").read_text(encoding="utf-8-sig")
        except OSError:
            pyproject_text = ""
    python_claims = [c for c in _python_claims(root)]
    has_python = bool(pyproject_text) or any((root / name).is_file() for name in ("setup.py", "setup.cfg", "requirements.txt"))
    if has_python or python_claims:
        steps.append(f"- uses: {SETUP_PYTHON_REF}\n  with:\n    python-version: \"{ACTION_PYTHON}\"")
        installs: list[str] = []
        if (root / "requirements.txt").is_file():
            installs.append("python -m pip install -r requirements.txt")
        if "[project]" in pyproject_text or "[build-system]" in pyproject_text or (root / "setup.py").is_file():
            installs.append("python -m pip install -e .")
        tools = []
        for claim in python_claims:
            if claim.command.startswith("python3 -m pytest"):
                tools.append("pytest")
            elif claim.command.startswith("ruff "):
                tools.append("ruff")
            elif claim.command.startswith("mypy "):
                tools.append("mypy")
        if tools:
            installs.append("python -m pip install " + " ".join(tools))
        if installs:
            steps.append("- run: |\n    " + "\n    ".join(installs))

    if (root / "go.mod").is_file():
        steps.append(f"- uses: {SETUP_GO_REF}\n  with:\n    go-version-file: go.mod")
    if (root / "Gemfile").is_file():
        steps.append(f"- uses: {SETUP_RUBY_REF}\n  with:\n    bundler-cache: true")
    return steps


def render_workflow(config_path_in_repo: str, default_branch: str, setup: list[str] | tuple[str, ...] = ()) -> str:
    setup_block = ""
    if setup:
        indented = "\n".join("      " + line if line else line for step in setup for line in step.split("\n"))
        setup_block = "      # Installs what the starter claims need. Edit freely; the claims in\n      # claims.toml are what the gate enforces, these steps only make them runnable.\n" + indented + "\n"
    return f"""# Countersign: verifies every push to {default_branch} and every pull request.
# Written by `countersign init`. Safe to edit; the action's inputs are
# documented at https://github.com/{ACTION_REF.split('@')[0]}.
name: countersign
on:
  push:
    branches: [{json.dumps(default_branch)}]
  pull_request:
    # labeled and unlabeled are here so that adding the countersign-approved
    # label re-runs the gate and the approval takes effect.
    types: [opened, synchronize, reopened, labeled, unlabeled]

# contents: read is all the checks need. The two write permissions let the
# action sign the receipt with GitHub Artifact Attestations, which it does
# by default for public repositories; remove them and set attest: "false"
# to opt out.
permissions:
  contents: read
  id-token: write
  attestations: write

jobs:
  countersign:
    name: countersign verify
    runs-on: ubuntu-latest
    steps:
      - uses: {CHECKOUT_REF}
{setup_block}      - uses: {ACTION_REF}
        with:
          config: {json.dumps(config_path_in_repo)}
"""


@dataclass(frozen=True)
class StarterClaim:
    claim_id: str
    statement: str
    command: str
    source: str
    # The files the command's meaning depends on; fingerprinted on pull requests.
    inputs: tuple[str, ...] = ()


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
        claims.append(StarterClaim(TESTS_PASS, "The full test suite passes", test_command, "package.json scripts.test", NODE_TEST_INPUTS))
    if isinstance(scripts.get("lint"), str) and scripts["lint"].strip():
        claims.append(StarterClaim("lint-clean", "The linter reports nothing", f"{run} lint", "package.json scripts.lint", NODE_LINT_INPUTS))
    for name in ("typecheck", "type-check", "tsc"):
        if isinstance(scripts.get(name), str) and scripts[name].strip():
            claims.append(StarterClaim("types-check", "The type checker reports nothing", f"{run} {name}", f"package.json scripts.{name}", NODE_TYPES_INPUTS))
            break
    else:
        deps = {}
        for key in ("devDependencies", "dependencies"):
            if isinstance(data.get(key), dict):
                deps.update(data[key])
        if "typescript" in deps and (root / "tsconfig.json").is_file():
            claims.append(StarterClaim("types-check", "The type checker reports nothing", "npx tsc --noEmit", "tsconfig.json with typescript installed", NODE_TYPES_INPUTS))
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
        claims.append(StarterClaim(TESTS_PASS, "The full test suite passes", "python3 -m pytest -q", "pytest configuration", PYTEST_INPUTS))
    elif has_python and ((root / "tests").is_dir() or (root / "test").is_dir()):
        start = "tests" if (root / "tests").is_dir() else "test"
        claims.append(StarterClaim(TESTS_PASS, "The full test suite passes", f"python3 -m unittest discover -s {start} -t .", f"{start}/ directory", UNITTEST_INPUTS))
    if "[tool.ruff" in pyproject_text or (root / "ruff.toml").is_file() or (root / ".ruff.toml").is_file():
        claims.append(StarterClaim("lint-clean", "The linter reports nothing", "ruff check .", "ruff configuration", RUFF_INPUTS))
    if "[tool.mypy" in pyproject_text or (root / "mypy.ini").is_file():
        claims.append(StarterClaim("types-check", "The type checker reports nothing", "mypy .", "mypy configuration", MYPY_INPUTS))
    return claims


# The files and patterns that control what each runner discovers and runs.
# Declared whether or not they exist: an absent input has a fingerprint
# too, so a pull request that adds a pytest.ini or a vitest.config.ts that
# narrows discovery changes the proof and is a weakening.
NODE_TEST_INPUTS = ("package.json", "vitest.config.*", "vitest.workspace.*", "vite.config.*", "jest.config.*", ".mocharc.*", "playwright.config.*")
NODE_LINT_INPUTS = ("package.json", "eslint.config.*", ".eslintrc.*", ".eslintrc", ".eslintignore", "biome.json", "biome.jsonc")
NODE_TYPES_INPUTS = ("package.json", "tsconfig.json", "tsconfig.*.json")
PYTEST_INPUTS = ("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "conftest.py")
UNITTEST_INPUTS = ("pyproject.toml",)
RUFF_INPUTS = ("pyproject.toml", "ruff.toml", ".ruff.toml")
MYPY_INPUTS = ("pyproject.toml", "mypy.ini", "setup.cfg")
GO_INPUTS = ("go.mod",)
RUST_INPUTS = ("Cargo.toml", ".cargo/config.toml")
RUBY_INPUTS = ("Gemfile", ".rspec")


def _go_claims(root: Path) -> list[StarterClaim]:
    if not (root / "go.mod").is_file():
        return []
    return [
        StarterClaim(TESTS_PASS, "The full test suite passes", "go test ./...", "go.mod", GO_INPUTS),
        StarterClaim("vet-clean", "go vet reports nothing", "go vet ./...", "go.mod", GO_INPUTS),
    ]


def _rust_claims(root: Path) -> list[StarterClaim]:
    if not (root / "Cargo.toml").is_file():
        return []
    return [StarterClaim(TESTS_PASS, "The full test suite passes", "cargo test", "Cargo.toml", RUST_INPUTS)]


def _ruby_claims(root: Path) -> list[StarterClaim]:
    if (root / "Gemfile").is_file() and (root / "spec").is_dir():
        return [StarterClaim(TESTS_PASS, "The full test suite passes", "bundle exec rspec", "Gemfile with spec/", RUBY_INPUTS)]
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
            '# inputs = ["Makefile"]        files the command depends on; a change is a weakening on pull requests',
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
            *([f"inputs = [{', '.join(_toml_string(i) for i in claim.inputs)}]"] if claim.inputs else []),
            "",
        ]
    return "\n".join(lines)
