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

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_EXEMPT_MARKER = "countersign: exempt"

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


def _table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
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
    claims_file: str | None = "claims.toml"
    receipt_dir: str = ".countersign"
    timeout_s: int = 300
    max_output_bytes: int = 20000
    extra: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "Config":
        path = Path(path).resolve()
        raw: dict[str, Any] = {}
        if path.exists():
            try:
                with path.open("rb") as handle:
                    raw = tomllib.load(handle)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f"{path.name} is not valid TOML: {exc}") from None
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
            claims_file=claims_file,
            receipt_dir=_string(receipts, "receipts", "dir", ".countersign"),
            timeout_s=_integer(run, "run", "timeout_s", 300, minimum=1),
            max_output_bytes=_integer(run, "run", "max_output_bytes", 20000, minimum=0),
            extra=raw,
        )

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
