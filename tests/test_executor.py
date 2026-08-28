import ast

import pytest

from sandbox.executor import SandboxViolation, check_imports


def test_authorized_import_passes() -> None:
    check_imports(ast.parse("import math"), ["math"])


def test_unauthorized_import_raises() -> None:
    with pytest.raises(SandboxViolation):
        check_imports(ast.parse("import os"), ["math"])


def test_glob_pattern_authorizes_submodule() -> None:
    check_imports(ast.parse("import collections.abc"), ["collections.*"])


def test_glob_pattern_does_not_authorize_bare_module() -> None:
    with pytest.raises(SandboxViolation):
        check_imports(ast.parse("import collections"), ["collections.*"])


def test_importfrom_authorized_module_passes() -> None:
    check_imports(ast.parse("from math import sqrt"), ["math"])


def test_importfrom_unauthorized_module_raises() -> None:
    with pytest.raises(SandboxViolation):
        check_imports(ast.parse("from os import path"), ["math"])


def test_relative_import_is_blocked() -> None:
    with pytest.raises(SandboxViolation):
        check_imports(ast.parse("from . import foo"), ["foo"])
