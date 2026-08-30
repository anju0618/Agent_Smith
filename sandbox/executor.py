"""
ast = Abstract Syntax Tree(抽象構文木)
    pythonのソースコード（文字列）をその文法的な構造を表す木構造のオブジェクトに変換したもの．

"""

import ast
import fnmatch

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

    def __init__(self, answer):
        super().__init__(answer)
        self.answer = answer


def check_imports(tree: ast.AST, authorized: list[str]) -> None:
    """
    インポートチェック
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
    return any(fnmatch.fnmatch(module_name, pattern) for pattern in authorized)


class Sandbox:

    def __init__(self, config=None) -> None:
        if config is None:
            self.config = SandboxConfig(authorized_imports = DEFAULT_AUTHORIZED_IMPORTS)
            self.namespace = 
        else:
            self.config = config

    def run(self, code: str) -> None:

        if not code.strip():
            return "[Error] empty code"
        try:
            tree = ast.parse(code)
            check_imports(tree, self.config.authorized_imports)
            compiled_code = compile(tree, "<agent>", "exec")
            exec(compiled_code, namespace_dict)
        except SyntaxError as e:
            return f"[SyntaxError] {e}"
        except SandboxViolation as e:
            return f"[SandboxViolation] {e}"


if __name__ == "__main__":
    sandbox = Sandbox()
    Sandbox.run("import numpy\ndef test():\n\treturn numpy.mean([1,2,3,4])\nret_val = test()\nprint(ret_val)")
