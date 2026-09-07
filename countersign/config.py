# audited on 20260903
"""Configuration: one TOML file per repository, everything overridable.

The defaults are the ones the underlying checks were tuned against (a
production tree of 546+ source files, reviewed file by file, producing zero
false positives). Repositories can narrow paths, extend ignores, or turn off
the test-file exclusion, but the exemption marker mechanism is fixed: it is
the honest way to say "this line is a false positive" in the file itself,
where a reviewer sees it.

A config that cannot be honoured as written raises ConfigError with the
reason; it never degrades into a scan of nothing that then passes.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_EXEMPT_MARKER = "countersign: exempt"

# Every section and key the config may carry. Anything else is refused, not
# ignored: a misspelled `requird` would silently drop the requirement, and a
# stray `[claim]` section would parse as nothing and pass. Same rule as the
# claims file.
KNOWN_SECTIONS: dict[str, frozenset[str]] = {
    "scan": frozenset({"paths", "ignore_dirs", "extensions", "exempt_marker", "exclude_tests", "allow_empty"}),
    "claims": frozenset({"file", "required", "fail_on_weakened", "optional", "command_change"}),
    "receipts": frozenset({"dir"}),
    "run": frozenset({"timeout_s", "max_output_bytes", "output"}),
}

COMMAND_CHANGE_POLICIES = ("fail", "note")
OUTPUT_POLICIES = ("excerpt", "none")

# The fields that make up the gate policy: what a pull request is not
# allowed to weaken without a maintainer's approval. Everything else in the
# config (nothing today) is operational.
POLICY_FIELDS = (
    "paths", "ignore_dirs", "extensions", "exempt_marker", "exclude_tests", "allow_empty",
    "claims_file", "required_claims", "fail_on_weakened", "claims_optional", "command_change",
    "receipt_dir", "timeout_s", "max_output_bytes", "output",
)

# Directories never scanned, in any repository. Build output and vendored
# dependencies are not the agent's work; scanning them only produces noise.
DEFAULT_IGNORE_DIRS: frozenset[str] = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", "target",
    ".next", ".nuxt", ".wrangler", ".cache", ".countersign", "coverage",
    ".tox", ".idea", ".vscode", "vendor",
})

# Extensions the marker rules are known to behave on: source files where the
# rules were tuned. Markdown, JSON and prose are deliberately absent; the
# rules are for code.
DEFAULT_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".go", ".rs", ".rb", ".java", ".kt", ".swift", ".php", ".cs", ".scala",
})

# Test files legitimately fabricate data. They are excluded from the marker
# scan by default and the receipt says so; a repository can override with
# exclude_tests = false.
TEST_DIR_NAMES: frozenset[str] = frozenset({"tests", "test", "__tests__", "spec"})

TEST_FILE_PREFIXES: tuple[str, ...] = ("test_",)
TEST_FILE_SUFFIXES: tuple[str, ...] = (
    "_test.py", "_test.go", "_test.rs", "_spec.rb", "_spec.exs",
    ".test.ts", ".test.tsx", ".test.js", ".test.jsx", ".spec.ts", ".spec.tsx",
    ".spec.js", ".spec.jsx", ".test.mjs", ".spec.mjs",
)


class ConfigError(ValueError):
    """The config file exists but cannot be honoured as written."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_test_file(relative: Path) -> bool:
    name = relative.name
    if any(name.startswith(prefix) for prefix in TEST_FILE_PREFIXES):
        return True
    if any(name.endswith(suffix) for suffix in TEST_FILE_SUFFIXES):
        return True
    return any(part in TEST_DIR_NAMES for part in relative.parts[:-1])


def _did_you_mean(unknown: str, known: frozenset[str]) -> str:
    close = difflib.get_close_matches(unknown, sorted(known), n=1, cutoff=0.6)
    return f" (did you mean '{close[0]}'?)" if close else ""


def _refuse_unknown(entry: dict[str, Any], known: frozenset[str], where: str) -> None:
    unknown = sorted(set(entry) - known)
    if not unknown:
        return
    listed = ", ".join(f"'{key}'{_did_you_mean(key, known)}" for key in unknown)
    raise ConfigError(
        f"{where} declares {listed}, which Countersign does not understand. Known keys are {sorted(known)}. "
        "An unrecognised key is refused rather than ignored, because ignoring it would quietly change what the gate enforces."
    )


def _table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    _refuse_unknown(value, KNOWN_SECTIONS[name], f"[{name}]")
    return value


def _choice(table: dict[str, Any], section: str, key: str, default: str, choices: tuple[str, ...]) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or value not in choices:
        raise ConfigError(f"[{section}] {key} must be one of {list(choices)}")
    return value


def _string_list(table: dict[str, Any], section: str, key: str, default: frozenset[str] | list[str]) -> list[str]:
    value = table.get(key, list(default))
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"[{section}] {key} must be a list of strings")
    return list(value)


def _string(table: dict[str, Any], section: str, key: str, default: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"[{section}] {key} must be a non-empty string")
    return value


def _boolean(table: dict[str, Any], section: str, key: str, default: bool) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"[{section}] {key} must be true or false")
    return value


def _integer(table: dict[str, Any], section: str, key: str, default: int, *, minimum: int) -> int:
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"[{section}] {key} must be an integer of at least {minimum}")
    return value


@dataclass
class Config:
    """Everything one verification run needs, resolved from countersign.toml."""

    root: Path
    config_path: Path
    paths: list[str] = field(default_factory=lambda: ["."])
    ignore_dirs: set[str] = field(default_factory=lambda: set(DEFAULT_IGNORE_DIRS))
    extensions: set[str] = field(default_factory=lambda: set(DEFAULT_EXTENSIONS))
    exempt_marker: str = DEFAULT_EXEMPT_MARKER
    exclude_tests: bool = True
    allow_empty: bool = False
    claims_file: str | None = "claims.toml"
    required_claims: list[str] = field(default_factory=list)
    fail_on_weakened: bool = True
    claims_optional: bool = False
    command_change: str = "fail"
    receipt_dir: str = ".countersign"
    timeout_s: int = 300
    max_output_bytes: int = 20000
    output: str = "excerpt"
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Config":
        path = Path(path).resolve()
        text: bytes | None = None
        if path.exists():
            text = path.read_bytes()
        return cls.from_bytes(text, root=path.parent, config_path=path)

    @classmethod
    def from_bytes(cls, text: bytes | None, *, root: Path, config_path: Path) -> "Config":
        """Parse config text as if it lived at ``config_path`` under ``root``.

        Used for the file on disk and for the same file at a base revision,
        so a pull request's code can be checked against the policy of the
        branch it targets."""
        raw: dict[str, Any] = {}
        if text is not None:
            try:
                raw = tomllib.loads(text.decode("utf-8-sig"))
            except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
                raise ConfigError(f"{Path(config_path).name} is not valid TOML: {exc}") from None
        if not isinstance(raw, dict):
            raise ConfigError(f"{Path(config_path).name} is not a table")
        _refuse_unknown(raw, frozenset(KNOWN_SECTIONS), Path(config_path).name)
        path = Path(config_path)
        scan = _table(raw, "scan")
        claims = _table(raw, "claims")
        receipts = _table(raw, "receipts")
        run = _table(raw, "run")

        claims_file_raw = claims.get("file", "claims.toml")
        if not isinstance(claims_file_raw, str):
            raise ConfigError('[claims] file must be a string; use "" to run the marker scan only')
        claims_file = claims_file_raw.strip() or None

        return cls(
            root=path.parent,
            config_path=path,
            paths=_string_list(scan, "scan", "paths", ["."]),
            ignore_dirs=set(_string_list(scan, "scan", "ignore_dirs", DEFAULT_IGNORE_DIRS)),
            extensions=set(_string_list(scan, "scan", "extensions", DEFAULT_EXTENSIONS)),
            exempt_marker=_string(scan, "scan", "exempt_marker", DEFAULT_EXEMPT_MARKER),
            exclude_tests=_boolean(scan, "scan", "exclude_tests", True),
            allow_empty=_boolean(scan, "scan", "allow_empty", False),
            claims_file=claims_file,
            required_claims=_string_list(claims, "claims", "required", []),
            fail_on_weakened=_boolean(claims, "claims", "fail_on_weakened", True),
            claims_optional=_boolean(claims, "claims", "optional", False),
            command_change=_choice(claims, "claims", "command_change", "fail", COMMAND_CHANGE_POLICIES),
            receipt_dir=_string(receipts, "receipts", "dir", ".countersign"),
            timeout_s=_integer(run, "run", "timeout_s", 300, minimum=1),
            max_output_bytes=_integer(run, "run", "max_output_bytes", 20000, minimum=0),
            output=_choice(run, "run", "output", "excerpt", OUTPUT_POLICIES),
            extra=raw,
        )

    def policy(self) -> dict[str, Any]:
        """The policy fields in canonical form: lists sorted, so two configs
        that enforce the same thing fingerprint the same."""
        out: dict[str, Any] = {}
        for name in POLICY_FIELDS:
            value = getattr(self, name)
            if isinstance(value, (set, frozenset, list)):
                value = sorted(value)
            out[name] = value
        return out

    def policy_sha256(self) -> str:
        return hashlib.sha256(json.dumps(self.policy(), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def with_policy_of(self, other: "Config") -> "Config":
        """This checkout, enforced under ``other``'s policy."""
        merged = Config(root=self.root, config_path=self.config_path, extra=self.extra)
        for name in POLICY_FIELDS:
            value = getattr(other, name)
            setattr(merged, name, set(value) if isinstance(value, set) else (list(value) if isinstance(value, list) else value))
        return merged

    def claims_path(self) -> Path | None:
        if not self.claims_file:
            return None
        candidate = (self.root / self.claims_file).resolve()
        return candidate if candidate.exists() else None

    def register_path(self) -> Path:
        return self.root / self.receipt_dir / "register.jsonl"

    def receipts_root(self) -> Path:
        return self.root / self.receipt_dir / "receipts"

    def collect_files(self) -> list[Path]:
        """Every in-scope source file, deterministically ordered.

        The root is resolved once and everything is computed against the
        resolved form, so symlinked roots (/var against /private/var on
        macOS) cannot split one tree into two spellings.

        A scan path that does not exist, or that points outside the root, is
        a ConfigError: a typo in ``paths`` must not become a scan of nothing
        that then passes.
        """
        root = Path(self.root).resolve()
        collected: list[Path] = []
        if not self.paths:
            raise ConfigError("[scan] paths is empty; a scan of nothing cannot pass. Name at least one path, or set allow_empty = true to say so on purpose")
        if not self.extensions:
            raise ConfigError("[scan] extensions is empty; a scan of nothing cannot pass. Name at least one extension, or set allow_empty = true to say so on purpose")
        for base in self.paths:
            base_path = (root / base).resolve()
            if not base_path.is_relative_to(root):
                raise ConfigError(f"scan path '{base}' is outside the repository root {root}")
            if base_path.is_file():
                candidates = [base_path]
            elif base_path.is_dir():
                candidates = sorted(base_path.rglob("*"))
            else:
                raise ConfigError(f"scan path '{base}' does not exist under {root}")
            for candidate in candidates:
                if not candidate.is_file():
                    continue
                relative = candidate.relative_to(root)
                if any(part in self.ignore_dirs for part in relative.parts):
                    continue
                if candidate.suffix not in self.extensions:
                    continue
                if self.exclude_tests and is_test_file(relative):
                    continue
                collected.append(candidate)
        return sorted(set(collected))
