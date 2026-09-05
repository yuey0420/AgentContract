"""Detect evidence weakening in existing tests using syntax, not model claims."""

import ast
from pathlib import PurePosixPath


def _assertions(source):
    tree = ast.parse(source)
    assertions, skips, mocks = set(), 0, 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Assert):
            assertions.add(ast.dump(node, include_attributes=False))
        if isinstance(node, ast.Call):
            name = getattr(node.func, "attr", getattr(node.func, "id", ""))
            if name.startswith("assert"):
                assertions.add(ast.dump(node, include_attributes=False))
            if name in {"skip", "skipIf", "skipUnless", "skipTest", "xfail"}:
                skips += 1
            if name in {"patch", "Mock", "MagicMock", "mock"}:
                mocks += 1
        if isinstance(node, ast.Attribute) and node.attr in {"skip", "xfail"}:
            skips += 1
    return assertions, skips, mocks


def acceptance_semantics_changed(filepath, before, after):
    path = PurePosixPath(filepath.replace("\\", "/"))
    is_test = "tests" in path.parts or path.name.startswith("test_") or any(marker in path.name for marker in (".test.", ".spec.", "_test."))
    if not is_test or before == after:
        return False
    if path.suffix != ".py":
        return True
    try:
        old, old_skips, old_mocks = _assertions(before)
        new, new_skips, new_mocks = _assertions(after)
    except SyntaxError:
        return True
    return not old.issubset(new) or new_skips > old_skips or new_mocks > old_mocks
