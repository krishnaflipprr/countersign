"""Configuration: one TOML file per repository, everything overridable.

The defaults are the ones the underlying checks were tuned against (a
production-certified tree of 546+ source files producing zero false
positives). Repositories can narrow paths, extend ignores, or turn off the
test-file exclusion, but the exemption marker mechanism is fixed: it is the
honest way to say "this line is a false positive" in the file itself, where
a reviewer sees it.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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
        raw: dict = {}
        if path.exists():
            with path.open("rb") as handle:
                raw = tomllib.load(handle)
        scan = raw.get("scan", {})
        claims = raw.get("claims", {})
        receipts = raw.get("receipts", {})
        run = raw.get("run", {})
        return cls(
            root=path.parent,
            config_path=path,
            paths=list(scan.get("paths", ["."])),
            ignore_dirs=set(scan.get("ignore_dirs", DEFAULT_IGNORE_DIRS)),
            extensions=set(scan.get("extensions", DEFAULT_EXTENSIONS)),
            exempt_marker=str(scan.get("exempt_marker", DEFAULT_EXEMPT_MARKER)),
            exclude_tests=bool(scan.get("exclude_tests", True)),
            claims_file=claims.get("file", "claims.toml"),
            receipt_dir=str(receipts.get("dir", ".countersign")),
            timeout_s=int(run.get("timeout_s", 300)),
            max_output_bytes=int(run.get("max_output_bytes", 20000)),
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
        """
        root = Path(self.root).resolve()
        collected: list[Path] = []
        for base in self.paths:
            base_path = (root / base).resolve()
            if base_path.is_file():
                candidates = [base_path]
            elif base_path.is_dir():
                candidates = sorted(base_path.rglob("*"))
            else:
                continue
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
