"""
ast = Abstract Syntax Tree(抽象構文木)
    pythonのソースコード（文字列）をその文法的な構造を表す木構造のオブジェクトに変換したもの．

"""

import ast
import builtins
import contextlib
import fnmatch
import io

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
    return any(fnmatch.fnmatch(module_name, pattern) for pattern in authorized)


def _make_restricted_import(authorized: list[str]):
    """
    __import__ 自体を差し替えて，`__import__("os")` のような
    ast の import 文チェックを迂回する呼び方も弾けるようにする．
    """
    # check_imports() は ast.parse した木を見て "import xxx" / "from xxx import ..."
    # という"文"だけをチェックしている．しかし Python の import は結局のところ
    # 組み込み関数 __import__() の呼び出しに変換されるだけなので，
    # コード側が __import__("os") のように関数として直接呼べば ast チェックを素通りしてしまう．
    # なので __builtins__["__import__"] 自体をここで検査付きのものに差し替える．
    real_import = builtins.__import__

    def restricted_import(name, globals=None, locals=None, fromlist=(), level=0):
        if not _is_authorized(name, authorized):
            raise SandboxViolation(f"{name} is not permitted")
        return real_import(name, globals, locals, fromlist, level)

    return restricted_import


def final_answer(answer):
    # サンドボックス内で実行されるコードから呼ばれることを想定した関数．
    # 戻り値として返すのではなく例外として投げることで，
    # exec() の呼び出し元（run()）まで一気に抜けて答えを伝搬させる．
    raise FinalAnswer(answer)


class Sandbox:

    def __init__(self, config=None) -> None:
        if config is None:
            config = SandboxConfig(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
        self.config = config
        # exec() に渡す globals 辞書．同じ Sandbox インスタンスで run() を
        # 複数回呼んでもこの辞書を使い回すことで，変数の状態が呼び出しをまたいで
        # 持続する（例: 1回目で x = 10 して，2回目で x を参照できる）．
        self.namespace = self._build_namespace()

    def _build_namespace(self) -> dict:
        # exec() は globals に "__builtins__" が無いと自動的に本物の
        # builtins モジュールを丸ごと差し込んでしまう．それだと __import__ を
        # 差し替えた意味が無くなるので，コピーした builtins 辞書を自前で渡し，
        # その中の __import__ だけを制限付きのものに置き換える．
        restricted_builtins = builtins.__dict__.copy()
        restricted_builtins["__import__"] = _make_restricted_import(self.config.authorized_imports)
        # final_answer をトップレベルの名前として登録しておくことで，
        # サンドボックス内のコードから普通の関数呼び出しとして final_answer(42) と書ける．
        return {"__builtins__": restricted_builtins, "final_answer": final_answer}

    def run(self, code: str) -> str:

        if not code.strip():
            return "[Error] empty code"
        try:
            tree = ast.parse(code)
            check_imports(tree, self.config.authorized_imports)
            compiled_code = compile(tree, "<agent>", "exec")
            # サンドボックス内の print() 出力を呼び出し元に文字列として返したいので，
            # 実プロセスの stdout ではなく StringIO にリダイレクトしてから実行する．
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(compiled_code, self.namespace)
            return output.getvalue()
        except SyntaxError as e:
            return f"[SyntaxError] {e}"
        except SandboxViolation as e:
            return f"[SandboxViolation] {e}"
        except FinalAnswer:
            # ここで文字列化して握りつぶさず，呼び出し元まで素通しする．
            # エージェントループ側は FinalAnswer.answer を見て最終出力として扱う想定．
            raise
        except Exception as e:
            # ZeroDivisionError など，サンドボックス内のコードが投げた実行時エラーは
            # プロセスを落とさず，LLM に見せられる文字列として返す．
            return f"[Error] {type(e).__name__}: {e}"


if __name__ == "__main__":
    sandbox = Sandbox()
    print(sandbox.run("import math\ndef test():\n\treturn sum([1,2,3,4]) / 4\nret_val = test()\nprint(ret_val)"))
