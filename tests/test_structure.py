"""Guards the module split: no undefined names, no module alias hidden by a local variable."""
import ast
import builtins
import symtable
import unittest
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "telegram_migration"


def _walk(table, defined, aliases, found, path):
    for symbol in table.get_symbols():
        name = symbol.get_name()
        if table.get_type() != "function":
            continue
        if symbol.is_global() and symbol.is_referenced() and name not in defined and not hasattr(builtins, name):
            found.append(f"{path.name}: {table.get_name()}() uses undefined name '{name}'")
        if name in aliases and (symbol.is_local() or symbol.is_parameter()) and symbol.is_referenced():
            found.append(f"{path.name}: {table.get_name()}() has a local '{name}' hiding the module of that name")
    for child in table.get_children():
        _walk(child, defined, aliases, found, path)


class StructureTests(unittest.TestCase):
    def test_every_module_resolves_all_of_its_names(self):
        found = []
        for path in sorted(PACKAGE.glob("*.py")):
            code = path.read_text(encoding="utf-8")
            top = symtable.symtable(code, str(path), "exec")
            defined = {s.get_name() for s in top.get_symbols() if s.is_assigned() or s.is_imported()}
            aliases = set()
            for node in ast.parse(code).body:
                if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module is None:
                    aliases.update(a.asname or a.name for a in node.names)
            _walk(top, defined, aliases, found, path)
        self.assertEqual(found, [])

    def test_no_module_is_a_monolith_again(self):
        longest = max(PACKAGE.glob("*.py"), key=lambda p: len(p.read_text(encoding="utf-8").splitlines()))
        self.assertLess(len(longest.read_text(encoding="utf-8").splitlines()), 500, longest.name)


if __name__ == "__main__":
    unittest.main()
