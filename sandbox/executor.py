"""
ast = Abstract Syntax Tree(抽象構文木)
    pythonのソースコード（文字列）をその文法的な構造を表す木構造のオブジェクトに変換したもの．

"""

import ast
import builtins
import contextlib
import fnmatch
import io
from collections.abc import Callable
from types import ModuleType
from typing import Any

from models import SandboxConfig

DEFAULT_AUTHORIZED_IMPORTS = [
    "math", "math.*",
    "collections", "collections.*",
    "itertools", "re", "json",
    "typing", "typing.*",
    "functools", "operator",
    "heapq", "bisect", "copy",
    "string", "random",
    "datetime", "datetime.*",
    "array", "cmath",
]


class SandboxViolation(Exception):
    """
    AIが出してきたものが，何かに違反していた時
    つかうエラー
    """


class FinalAnswer(Exception):
    """
    サンドボックス内のコードが final_answer(answer) を呼んだときに
    投げる例外．answer 属性に渡された値を保持する．
    """

    def __init__(self, answer: Any) -> None:
        super().__init__(answer)
        self.answer = answer


def check_imports(tree: ast.AST, authorized: list[str]) -> None:
    """
    インポチェック
    違反してたら
    raise SandboxViolation
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_authorized(alias.name, authorized):
                    raise SandboxViolation(f"{alias.name} is not permitted")
        elif isinstance(node, ast.ImportFrom):
            if node.module is None or not _is_authorized(node.module, authorized):
                raise SandboxViolation(f"{node.module} is not permitted")


def _is_authorized(module_name: str, authorized: list[str]) -> bool:
    """
    module_name が authorized のいずれかのパターンに一致するか判定する．
    """
    return any(fnmatch.fnmatch(module_name, pattern) for pattern in authorized)


def _make_restricted_import(authorized: list[str]) -> Callable[..., ModuleType]:
    """
    __import__ 自体を差し替えて，`__import__("os")` のような
    ast の import 文チェックを迂回する呼び方も弾けるようにする．
    """
    real_import = builtins.__import__

    def restricted_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> ModuleType:
        """
        本物の __import__ の前に authorized チェックを挟むラッパー．
        """
        if not _is_authorized(name, authorized):
            raise SandboxViolation(f"{name} is not permitted")
        return real_import(name, globals, locals, fromlist, level)

    return restricted_import


def final_answer(answer: Any) -> None:
    """
    サンドボックス内のコードから呼ばれる関数．FinalAnswer(answer) を投げる．
    """
    raise FinalAnswer(answer)


class Sandbox:
    """
    許可された import だけを使えるコードを exec するサンドボックス．
    """

    def __init__(self, config: SandboxConfig | None = None) -> None:
        """
        config が None の場合は DEFAULT_AUTHORIZED_IMPORTS で SandboxConfig を作る．
        """
        if config is None:
            config = SandboxConfig(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
        self.config = config
        self.namespace = self._build_namespace()

    def _build_namespace(self) -> dict:
        """
        exec() に渡す globals 辞書を組み立てる．
        __import__ を制限付きのものに差し替え，final_answer を登録する．
        """
        restricted_builtins = builtins.__dict__.copy()
        restricted_builtins["__import__"] = _make_restricted_import(self.config.authorized_imports)
        return {"__builtins__": restricted_builtins, "final_answer": final_answer}

    def run(self, code: str) -> str:
        """
        code を ast チェックしたうえで exec し，標準出力を文字列として返す．
        エラー系は例外にせず [SyntaxError]/[SandboxViolation]/[Error] 付き文字列で返す．
        FinalAnswer だけは呼び出し元まで re-raise する．
        """

        if not code.strip():
            return "[Error] empty code"
        try:
            tree = ast.parse(code)
            check_imports(tree, self.config.authorized_imports)
            compiled_code = compile(tree, "<agent>", "exec")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(compiled_code, self.namespace)
            return output.getvalue()
        except SyntaxError as e:
            return f"[SyntaxError] {e}"
        except SandboxViolation as e:
            return f"[SandboxViolation] {e}"
        except FinalAnswer:
            raise
        except Exception as e:
            return f"[Error] {type(e).__name__}: {e}"


if __name__ == "__main__":
    sandbox = Sandbox()
    demo_code = "import math\ndef test():\n\treturn sum([1, 2, 3, 4]) / 4\nprint(test())"
    print(sandbox.run(demo_code))
