"""安全かつ設定可能なPythonコード実行サンドボックス(仕様書 4.2節)。

デフォルトでは、生成されたコードは ``unshare`` と ``bubblewrap`` によって
作られたネットワーク無効化済みのuser/PID/mount名前空間内の、永続的な
ワーカープロセス内で実行される。制限された名前空間およびbuiltinsのチェックは
多層防御(defense in depth)として引き続き機能し、OS境界がオブジェクト
イントロスペクション(introspection)による脱出をエージェントのホスト
プロセスに到達させないようにする。MCPツール関数は小さなJSONプロトコル経由で
信頼された親プロセスに橋渡しされるため、ツールの発見や呼び出しは動的なまま
行え、変数もエージェントの各ステップ間で保持される。

``isolated=False`` は、OS境界が既に確立された後にワーカー自身がコードを
実行するためだけに使われるモードである。
"""
from __future__ import annotations

import ast
import builtins
import contextlib
import fnmatch
import io
import os
import signal
from collections.abc import Callable
from types import ModuleType
from typing import Any, Dict, Optional

from models import SandboxConfig
from sandbox.isolated_process import IsolatedSandboxProcess

try:
    import resource
except ImportError:
    resource = None  # type: ignore[assignment]


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


DEFAULT_ALLOWED_DIRECTORIES = ["/testbed", "/tmp/agent"]


_UNSAFE_BUILTINS = {
    "eval", "exec", "compile", "input", "breakpoint",
    "help", "exit", "quit", "__import__", "open", "vars",
}

_RESERVED_GLOBAL_NAMES = {"__builtins__", "final_answer"}
_FORBIDDEN_PUBLIC_ATTRIBUTES = {"format"}

_FORBIDDEN_MODULE_ATTRIBUTES = {"string": {"Formatter"}}


class SandboxViolation(Exception):
    """サンドボックス化されたコードがimportまたはファイルシステム制限に違反した際に送出される例外。"""


class SandboxTimeoutError(Exception):
    """sandbox.run()の呼び出しがmax_execution_time_secondsを超えた際に内部的に送出される例外。"""


class FinalAnswer(Exception):
    """注入されたfinal_answer()組み込み関数がタスク完了を知らせるために送出する例外。

    送信された回答を`.answer`属性に保持する。オーケストレーター側がこの例外を
    捕捉してエージェントループを終了しSolutionOutputを構築する - そのため
    Sandbox.run()の汎用的なエラーハンドリングに飲み込まれてはならない
    (仕様書4.2節の例外伝播要件はKeyboardInterrupt/SystemExitを明示的に
    対象としているが、FinalAnswerはサンドボックス独自の同等な制御フロー
    シグナルであり、同じ扱いを受ける必要がある)。
    """

    def __init__(self, answer: Any) -> None:
        super().__init__(answer)
        self.answer = answer


def _is_authorized(module_name: str, authorized: list) -> bool:

    return any(fnmatch.fnmatch(module_name, pattern) for pattern in authorized)


_SAFE_DUNDER_ATTRS = {
    "__init__", "__name__", "__doc__", "__module__", "__qualname__",
    "__repr__", "__str__", "__format__", "__hash__",
    "__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__",
    "__bool__", "__len__", "__iter__", "__next__", "__contains__",
    "__getitem__", "__setitem__", "__delitem__", "__call__",
    "__enter__", "__exit__",
    "__add__", "__radd__", "__sub__", "__rsub__", "__mul__", "__rmul__",
    "__truediv__", "__rtruediv__", "__floordiv__", "__rfloordiv__",
    "__mod__", "__rmod__", "__pow__", "__rpow__",
    "__neg__", "__pos__", "__abs__", "__round__", "__divmod__",
}


def _is_forbidden_attribute(name: object) -> bool:


    return (
        isinstance(name, str)
        and (
            name in _FORBIDDEN_PUBLIC_ATTRIBUTES
            or (name.startswith("_") and name not in _SAFE_DUNDER_ATTRS)
        )
    )


def check_dunder_attribute_access(tree: ast.AST) -> None:
    """_SAFE_DUNDER_ATTRSに含まれない、明示的なプライベート属性アクセスを拒否する。

    これは古典的なプロセス内サンドボックス脱出手法を塞ぐためのものである:
    ``().__class__.__bases__[0].__subclasses__()`` は既にロード済みのクラス群を
    たどり、``__init__.__globals__['__builtins__']`` が *本物の*、制限されて
    いないbuiltinsであるようなクラスを見つけ出す - これはこのサンドボックスの
    名前空間内にある制限された__import__/openを完全にバイパスしてしまう。
    なぜなら、それらはサンドボックス化されたコード内での名前の*ルックアップ*
    だけを守るものであり、その経路を一切通らずに行われる任意のオブジェクトへの
    イントロスペクションまでは守らないからである。下の_make_restricted_getattrは、
    ドット構文ではなく``getattr(obj, "__subclasses__")``のように動的にアクセスされる
    同じ脱出経路を塞ぐものである。

    また、これは許可リストに載っているモジュールが``random._os``のような
    プライベートな実装詳細を通じて特権モジュールを漏らしてしまうことも防ぐ。
    公開されているモジュール型の属性については、下の_RestrictedModuleが別途
    チェックする。

    ``str.format``も禁止されている。これはその属性ミニ言語が実行時に文字列から
    名前をパースするため、AST上のAttributeノードのチェックをすり抜けてしまう
    ためである(例: ``"{0.__class__}".format(x)``)。f文字列や通常の文字列操作は
    引き続き利用可能である。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_forbidden_attribute(node.attr):

            raise SandboxViolation(f"access to '{node.attr}' is not permitted")


def check_imports(tree: ast.AST, authorized: list) -> None:
    """静的チェック: 許可リスト外のImport/ImportFromノードを全て拒否する。

    この静的チェックだけでは、import文を書く代わりに``__import__("os")``を
    ただの関数として呼び出すことで回避できてしまう。そのため、下の
    _make_restricted_importでは、サンドボックス化されたコードがどの経路で
    たどり着いたかによらず、呼び出し時に改めてチェックを行っている。
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):

            for alias in node.names:
                if not _is_authorized(alias.name, authorized):
                    raise SandboxViolation(f"import of '{alias.name}' is not permitted")
        elif isinstance(node, ast.ImportFrom):

            if node.module is None or not _is_authorized(node.module, authorized):
                raise SandboxViolation(f"import of '{node.module}' is not permitted")


            if any(alias.name == "*" for alias in node.names):
                raise SandboxViolation("star imports are not permitted")


class _RestrictedModule(ModuleType):
    """許可リストに載っているモジュールの読み取り専用ビュー。

    制限された``__import__``から本物のモジュールをそのまま返すのは、たとえ
    モジュール名自体が許可リストに載っていても安全ではない: 標準ライブラリの
    いくつかのモジュールは、そのグローバル変数の中に特権的なモジュールを
    保持している(例えば``random._os``や``typing.sys``)。このプロキシは
    プライベート属性へのアクセスをブロックし、入れ子になったモジュールも、
    それが独立して許可リストに載っていない限り公開しないようにする。
    """

    def __init__(
        self,
        module: ModuleType,
        authorized: list,
        wrap_module: Callable[[ModuleType], ModuleType],
    ) -> None:

        super().__init__(module.__name__, module.__doc__)


        object.__setattr__(self, "_restricted_module", module)
        object.__setattr__(self, "_authorized_modules", authorized)
        object.__setattr__(self, "_wrap_module", wrap_module)

    def __getattribute__(self, name: str) -> Any:

        if name in {"__name__", "__doc__", "__package__"}:
            module = object.__getattribute__(self, "_restricted_module")
            return getattr(module, name, None)

        if _is_forbidden_attribute(name):
            raise SandboxViolation(f"access to '{name}' is not permitted")

        module = object.__getattribute__(self, "_restricted_module")


        if module.__name__ == "operator" and name in {"attrgetter", "methodcaller"}:
            raise SandboxViolation(f"operator.{name} is not permitted")

        if name in _FORBIDDEN_MODULE_ATTRIBUTES.get(module.__name__, set()):
            raise SandboxViolation(f"{module.__name__}.{name} is not permitted")

        value = getattr(module, name)
        if isinstance(value, ModuleType):


            authorized = object.__getattribute__(self, "_authorized_modules")
            if not _is_authorized(value.__name__, authorized):
                raise SandboxViolation(
                    f"module attribute '{module.__name__}.{name}' exposes "
                    f"unauthorized module '{value.__name__}'"
                )

            wrap_module = object.__getattribute__(self, "_wrap_module")
            return wrap_module(value)
        return value

    def __repr__(self) -> str:

        module = object.__getattribute__(self, "_restricted_module")
        return f"<restricted module {module.__name__!r}>"


def _make_restricted_import(authorized: list) -> Callable[..., ModuleType]:


    real_import = builtins.__import__
    proxy_cache: Dict[str, ModuleType] = {}

    def wrap_module(module: ModuleType) -> ModuleType:


        cached = proxy_cache.get(module.__name__)
        if cached is not None:
            return cached
        proxy = _RestrictedModule(module, authorized, wrap_module)
        proxy_cache[module.__name__] = proxy
        return proxy

    def restricted_import(
        name: str,
        globals: Optional[dict] = None,
        locals: Optional[dict] = None,
        fromlist: tuple = (),
        level: int = 0,
    ) -> ModuleType:


        if not _is_authorized(name, authorized):
            raise SandboxViolation(f"import of '{name}' is not permitted")
        module = real_import(name, globals, locals, fromlist, level)
        return wrap_module(module)

    return restricted_import


def _make_restricted_open(allowed_directories: list) -> Callable[..., Any]:


    real_open = builtins.open

    resolved_allowed = [os.path.realpath(d) for d in allowed_directories]

    def restricted_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, bytes, os.PathLike)):


            target = os.path.realpath(os.fspath(file))

            allowed = any(target == base or target.startswith(base + os.sep) for base in resolved_allowed)
            if not allowed:
                raise SandboxViolation(
                    f"path '{file!r}' is outside the allowed directories {allowed_directories}"
                )
        return real_open(file, mode, *args, **kwargs)

    return restricted_open


def _make_restricted_getattr() -> Callable[..., Any]:


    real_getattr = builtins.getattr

    def restricted_getattr(obj: Any, name: Any, *default: Any) -> Any:
        if _is_forbidden_attribute(name):
            raise SandboxViolation(f"access to '{name}' is not permitted")
        return real_getattr(obj, name, *default)

    return restricted_getattr


def _make_restricted_setattr() -> Callable[..., Any]:


    real_setattr = builtins.setattr

    def restricted_setattr(obj: Any, name: Any, value: Any) -> None:
        if _is_forbidden_attribute(name):
            raise SandboxViolation(f"access to '{name}' is not permitted")
        real_setattr(obj, name, value)

    return restricted_setattr


def final_answer(answer: Any) -> None:
    """全てのサンドボックス名前空間に注入される関数 - MCPツールではない(仕様書 4.2節)。
    エージェントが最終回答を提出するために呼ぶと、FinalAnswer例外を送出して
    そのままrun()の外側(呼び出し元)まで伝播させ、タスク完了を知らせる。"""
    raise FinalAnswer(answer)


def _alarm_handler(signum: int, frame: Any) -> None:


    raise SandboxTimeoutError()


class Sandbox:
    """設定された制限のもとで、信頼できないLLM生成Pythonコードを実行するクラス。"""

    def __init__(
        self,
        config: Optional[SandboxConfig] = None,
        extra_namespace: Optional[Dict[str, Callable]] = None,
        apply_process_memory_limit: bool = True,
        isolated: bool = True,
    ) -> None:
        if config is None:

            config = SandboxConfig(
                authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,
                allowed_directories=DEFAULT_ALLOWED_DIRECTORIES,
            )
        extra = extra_namespace or {}

        collisions = sorted(_RESERVED_GLOBAL_NAMES & extra.keys())
        if collisions:
            raise ValueError(f"extra_namespace contains reserved name(s): {', '.join(collisions)}")

        self.config = config
        self._isolated_process: Optional[IsolatedSandboxProcess] = None
        if isolated:

            self.namespace = {}
            self._isolated_process = IsolatedSandboxProcess(
                config,
                extra,
                apply_process_memory_limit,
            )
            return


        if apply_process_memory_limit:
            self._apply_memory_limit()
        self.namespace = self._build_namespace(extra)

    def _apply_memory_limit(self) -> None:
        """このプロセスのアドレス空間に上限を設け、暴走したメモリ確保がOSの
        OOM killerを発動させるのではなくMemoryErrorとして捕捉できるようにする。

        これはワーカープロセスの生存期間中に一度だけ適用される。公開APIの
        Sandboxは通常専用のワーカープロセス内で動くため、RLIMIT_ASを下げても
        エージェント本体やテストランナーには影響しない。``isolated=False``は
        内部のワーカー専用モードであり、信頼できないコードに対して直接使う
        べきではない。
        """
        if resource is None or not hasattr(resource, "RLIMIT_AS"):
            return
        limit_bytes = self.config.max_memory_mb * 1024 * 1024
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)

            new_hard = hard if hard != resource.RLIM_INFINITY and hard < limit_bytes else limit_bytes
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, new_hard))
        except (ValueError, OSError):
            pass

    def _build_namespace(self, extra_namespace: Dict[str, Callable]) -> dict:


        collisions = sorted(_RESERVED_GLOBAL_NAMES & extra_namespace.keys())
        if collisions:
            raise ValueError(f"extra_namespace contains reserved name(s): {', '.join(collisions)}")


        restricted_builtins = {
            name: value for name, value in vars(builtins).items() if name not in _UNSAFE_BUILTINS
        }

        restricted_builtins["__import__"] = _make_restricted_import(self.config.authorized_imports)
        restricted_builtins["open"] = _make_restricted_open(self.config.allowed_directories)
        restricted_builtins["getattr"] = _make_restricted_getattr()
        restricted_builtins["setattr"] = _make_restricted_setattr()


        namespace: dict = {"__builtins__": restricted_builtins, "final_answer": final_answer}
        namespace.update(extra_namespace)
        return namespace

    def run(self, code: str) -> str:
        """1つのコードスニペットを実行し、キャプチャした標準出力、または明示的な
        エラー文字列を返す。

        通常のコードエラーでは決して例外を送出しない - それらは
        "[ErrorKind] ..." という形式のテキストとして返され、エージェントループが
        それをLLMへのObservationとしてそのまま渡せるようにするためである
        (仕様書4.1節の「明示的なフィードバックを必須とする」要件)。
        FinalAnswer・KeyboardInterrupt・SystemExitだけが例外的に呼び出し元へ
        そのまま伝播される。
        """
        if self._isolated_process is not None:

            return self._isolated_process.run(code)

        if not code.strip():
            return "[NoCodeBlock] The submitted code was empty."

        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            return f"[SyntaxError] {exc}"

        try:

            check_imports(tree, self.config.authorized_imports)
            check_dunder_attribute_access(tree)
        except SandboxViolation as exc:
            return f"[SandboxViolation] {exc}"

        compiled = compile(tree, "<agent>", "exec")
        output = io.StringIO()
        has_alarm = hasattr(signal, "SIGALRM")
        previous_handler = None
        if has_alarm:

            previous_handler = signal.signal(signal.SIGALRM, _alarm_handler)
            signal.alarm(self.config.max_execution_time_seconds)

        try:
            with contextlib.redirect_stdout(output):
                exec(compiled, self.namespace)
            return self._truncate(output.getvalue())
        except SandboxTimeoutError:

            partial = self._truncate(output.getvalue())
            return (
                f"[Timeout] Execution exceeded {self.config.max_execution_time_seconds}s "
                f"and was interrupted. Partial output before timeout:\n{partial}"
            )
        except MemoryError:

            return (
                f"[MemoryLimitExceeded] Execution exceeded "
                f"{self.config.max_memory_mb}MB and was interrupted."
            )
        except SandboxViolation as exc:

            return f"[SandboxViolation] {exc}"
        except FinalAnswer:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:  # noqa: BLE001
            return f"[Error] {type(exc).__name__}: {exc}"
        finally:
            if has_alarm:

                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous_handler)

    def _truncate(self, text: str) -> str:
        """先頭だけを残すと、長い出力の末尾に出るテスト結果や例外が消えてしまう
        (例: SWE-benchのeval.shはgit diffなどの前置きノイズの後、末尾で
        ようやくpytestのPASSED/FAILEDを出す)。末尾を手厚く残しつつ先頭にも
        少し文脈を残す。
        """
        limit = self.config.max_output_chars
        if len(text) <= limit:
            return text
        omitted = len(text) - limit
        head = limit // 4
        tail = limit - head
        return (
            text[:head]
            + f"\n[TruncatedOutput] {omitted} chars omitted - kept head+tail; the "
            f"tail usually holds the test result/traceback (output limit: {limit} chars).\n"
            + text[-tail:]
        )

    def close(self) -> None:
        """このSandboxがOS隔離ワーカーを所有している場合、それを停止する。"""
        if self._isolated_process is not None:
            self._isolated_process.close()

    def __del__(self) -> None:


        try:
            self.close()
        except Exception:
            pass


if __name__ == "__main__":

    sandbox = Sandbox()
    demo_code = "import math\ndef test():\n    return sum([1, 2, 3, 4]) / 4\nprint(test())"
    print(sandbox.run(demo_code))
