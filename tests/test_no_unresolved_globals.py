"""Static guarantee that every name referenced in any function body of
`src/wallet/**.py` resolves to a module global or builtin.

This is the same bug class as the original Tier 0.1 fix: a CLI command
body references a helper that was never imported, and because `--help`
doesn't execute the function body, the bug ships invisibly until the
user runs the actual command.

Three real hits this caught in the wild on the repo's first run:
  - cli/aave.py:positions  (`format_units` unimported — `wallet aave
    positions` always NameErrors)
  - cli/aave.py:supply     (`format_units` in InsufficientAllowance path)
  - cli/aave.py:repay      (`format_units` in InsufficientAllowance path)
all from the same missing line `from wallet.core.rpc import format_units`.

We use Python's `symtable` module — the exact scope analysis the
compiler runs — instead of `co_names` (which includes attribute access
strings and produces ~1400 false positives) or naive AST walking (which
fudges comprehension targets and closure vars).

This test is intentionally repo-wide, not just CLI-wide. Backend modules
that NameError at runtime would be just as bad."""

from __future__ import annotations

import builtins
import importlib
import symtable
from pathlib import Path


_BUILTINS = set(dir(builtins))
_SRC_ROOT = Path(__file__).parent.parent / "src" / "wallet"


def _collect_unresolved(t: symtable.SymbolTable, mod_globals: set[str]) -> list[tuple[int, str, str]]:
    """Return [(lineno, function-qualified-name, missing-name), …] for every
    name referenced inside a function body that isn't bound locally and
    isn't present in the module's resolved globals."""
    out: list[tuple[int, str, str]] = []

    def walk(tbl: symtable.SymbolTable, qual: str) -> None:
        if tbl.get_type() == "function":
            for sym in tbl.get_symbols():
                if (
                    sym.is_referenced()
                    and not sym.is_local()
                    and not sym.is_parameter()
                    and not sym.is_assigned()
                    and not sym.is_free()
                    and not sym.is_imported()
                ):
                    name = sym.get_name()
                    if name not in mod_globals and name not in _BUILTINS:
                        out.append((tbl.get_lineno(), qual, name))
        for child in tbl.get_children():
            walk(child, f"{qual}>{child.get_name()}")

    walk(t, "<module>")
    return out


def _module_name_for(path: Path) -> str:
    rel = path.relative_to(_SRC_ROOT.parent)  # relative to src/
    return ".".join(rel.with_suffix("").parts)


def test_no_unresolved_globals_in_function_bodies():
    """Walk every `.py` in src/wallet and assert no function body references
    a name that doesn't resolve at module load time. If this trips, you have
    a latent NameError that will fire on the first real invocation of the
    affected command — exactly the bug Tier 0.1 / approve.py:102 / aave.py
    were.
    """
    findings: list[str] = []
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        mod_name = _module_name_for(path)
        mod = importlib.import_module(mod_name)
        mod_globals = set(vars(mod).keys())
        tbl = symtable.symtable(path.read_text(), str(path), "exec")
        # Augment with names symtable considers top-level
        mod_globals |= {
            s.get_name()
            for s in tbl.get_symbols()
            if s.is_assigned() or s.is_imported()
        }
        for lineno, qual, name in _collect_unresolved(tbl, mod_globals):
            findings.append(
                f"{path.relative_to(_SRC_ROOT.parent.parent)}:{lineno}: "
                f"{qual}() references unbound name {name!r}"
            )

    assert not findings, (
        "Found latent NameError sites (same bug class as Tier 0.1 — "
        "function body references a helper that isn't imported):\n  "
        + "\n  ".join(findings)
    )
