# audited on 20260903
"""Executable claim: a tampered register must be caught by the chain check.

Run by the repository's own claims.toml. Exit 0 only when tampering a
recorded entry is detected.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from countersign.register import Register  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        register = Register(Path(tmp) / "register.jsonl")
        register.append("run_started", {"run_id": "demo-run"})
        register.append("finding", {"run_id": "demo-run", "path": "app.py", "line": 1})

        intact, note = register.verify_chain()
        print(f"before tampering: {note}")
        if not intact:
            print("a fresh register must verify intact; the check itself is broken")
            return 1

        path = Path(tmp) / "register.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace('"line": 1', '"line": 900')
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        intact, note = register.verify_chain()
        print(f"after tampering:  {note}")
        if intact:
            print("tampering was NOT detected")
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())
