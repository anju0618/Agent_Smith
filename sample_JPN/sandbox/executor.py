"""Secure, configurable Python execution sandbox (Section 4.2).

By default, generated code runs in a persistent worker inside a network-
disabled user/PID/mount namespace created by ``unshare`` and ``bubblewrap``.
The restricted namespace and builtins checks remain defense in depth, while
the OS boundary prevents an object-introspection escape from reaching the
agent's host process. MCP tool functions are bridged back to the trusted
parent process over a small JSON protocol, so tool discovery and calls remain
dynamic and variables persist between agent steps.

``isolated=False`` is used only by the worker itself to execute code after the
OS boundary has already been established.

# ============================================================================
# 【日本語解説】このファイルの立ち位置
# ============================================================================
# ここは「LLMが生成した信頼できないPythonコード」を安全に実行するための
# サンドボックス本体です。プロジェクト全体の中で最もセキュリティ上重要な
# ファイルと言ってよく、EXPLAINED.md の §8.1 で詳細に解説されています。
#
# 防御は大きく2層に分かれています:
#   1. このファイル(executor.py)がPythonレベルで行う制限
#      - importのアローリスト（静的+動的の二重チェック）
#      - 危険なbuiltins（eval/exec/open/__import__など）の除去・差し替え
#      - "ダンダー属性"（__xxx__ という名前の特殊属性）への
#        アクセス制限（後述する脱出手口への対策）
#      - signal.alarm()による実行時間制限、resource.setrlimitによる
#        メモリ制限
#   2. isolated_process.py / isolated_worker.py が行うOSレベルの隔離
#      （unshare + bubblewrapによるnamespace分離。ネットワークなし、
#        読み取り専用ルート、専用UID）
#
# このファイル単体の防御（1.）は「多層防御の1枚」に過ぎません。
# なぜなら、Pythonという言語自体が非常に強力な内省(introspection)機能を
# 持っており、"名前のルックアップ"を差し替えるだけでは防ぎきれない
# 脱出経路が存在するからです（下の check_dunder_attribute_access の
# docstringで具体例を説明します）。だからこそ、最終防衛ラインとして
# OSレベルの隔離（2.）が本丸になっています。
# ============================================================================
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
except ImportError:  # pragma: no cover - resource is Unix-only
    resource = None  # type: ignore[assignment]

# ----------------------------------------------------------------------------
# 【日本語解説】デフォルトのインポート許可リスト
# ----------------------------------------------------------------------------
# ここに列挙されたモジュール名（および "math.*" のようなglobパターン）
# だけがLLM生成コードからimportできます。デフォルト"拒否"方式なので、
# ここに無いモジュール（os, sys, subprocess, socket など）は
# 一切importできません。中身を見ると分かる通り、すべて「計算用の
# 純粋なライブラリ」であり、ファイルI/O・ネットワーク・プロセス起動が
# できるモジュールは意図的に1つも含まれていません。
# ----------------------------------------------------------------------------
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

# Builtins that would let sandboxed code escape the restrictions below.
# ----------------------------------------------------------------------------
# 【日本語解説】危険な組み込み関数(builtins)のブロックリスト
# ----------------------------------------------------------------------------
# 標準のPythonの組み込み関数(builtins)の中には、それ単体でサンドボックスの
# 意味を無くしてしまうものがあります:
#   - eval / exec / compile : 任意の文字列をPythonコードとして実行できる。
#     ASTベースの静的チェック(check_imports等)を素通りする最短の脱出経路。
#   - __import__ : import文を経由せず、関数として直接モジュールを
#     読み込める。制限された__import__を"差し替える"意味が無くなるので
#     忘れずに元のものを除去する必要がある。
#   - open : ファイルシステムへの生アクセス。あとで制限付きバージョンに
#     差し替えるが、まず元のものはbuiltinsから消しておく。
#   - input / breakpoint / help / exit / quit : サンドボックス実行が
#     対話端末を乗っ取ったりプロセスを止めたりする経路を塞ぐ。
#   - vars() : 引数なしで呼ぶと呼び出し元のローカル変数辞書を返す。
#     これ経由で内部実装の変数（例えば下のnamespace辞書そのもの）に
#     アクセスされる可能性があるため禁止。
# これらは「名前として存在すると危険」なので、_build_namespace() で
# builtins辞書から丸ごと除去されます（下記参照）。
# ----------------------------------------------------------------------------
_UNSAFE_BUILTINS = {
    "eval", "exec", "compile", "input", "breakpoint",
    "help", "exit", "quit", "__import__", "open", "vars",
}

# ----------------------------------------------------------------------------
# 【日本語解説】予約名・追加禁止属性
# ----------------------------------------------------------------------------
# _RESERVED_GLOBAL_NAMES: extra_namespace（MCPツール群など、サンドボックスの
# 名前空間に追加注入される関数群）が、サンドボックス自身が使う特別な名前
# （__builtins__ や final_answer）を上書きしてしまわないようにするための
# 予約リスト。もしMCPサーバー側がたまたま "final_answer" という名前の
# ツールを公開してしまったら、Sandbox初期化時に即座にエラーになる
# （静かに上書きされて動作がおかしくなる、という事故を防ぐ）。
#
# _FORBIDDEN_PUBLIC_ATTRIBUTES: "_"で始まらない（一見公開されている）
# 属性の中にも、危険なので禁止するものがある。"format" がその代表例で、
# 理由は check_dunder_attribute_access のdocstringで説明する
# str.format()の脱出経路。
#
# _FORBIDDEN_MODULE_ATTRIBUTES: モジュールごとに個別禁止したい属性。
# 例えば string.Formatter は str.format と同種の「文字列から属性名を
# 動的に解決する」機能を持つため、stringモジュールを許可しつつ
# Formatterクラスだけを個別に禁止している。
# ----------------------------------------------------------------------------
_RESERVED_GLOBAL_NAMES = {"__builtins__", "final_answer"}
_FORBIDDEN_PUBLIC_ATTRIBUTES = {"format"}
_FORBIDDEN_MODULE_ATTRIBUTES = {"string": {"Formatter"}}


class SandboxViolation(Exception):
    """Raised when sandboxed code violates an import or filesystem restriction."""
    # 【日本語解説】
    # 「LLMが書いたコードが制限に違反した」ことを表す例外。あとで
    # Sandbox.run() がこれをキャッチし、プロセスをクラッシュさせずに
    # "[SandboxViolation] ..." という文字列としてLLMへのObservationに
    # 変換する（＝呼び出し元には決して例外のまま伝播しない）。


class SandboxTimeoutError(Exception):
    """Raised internally when a sandbox.run() call exceeds max_execution_time_seconds."""
    # 【日本語解説】
    # 下の _alarm_handler が signal.alarm() のタイムアウト発火時に
    # この例外を送出する。これも SandboxViolation と同様、Sandbox.run()
    # の中で捕まえられ "[Timeout] ..." という文字列に変換される。


class FinalAnswer(Exception):
    """Raised by the injected final_answer() builtin to signal task completion.

    Carries the submitted answer in `.answer`. The orchestrator catches this to
    end the agent loop and build SolutionOutput - it must NOT be swallowed by
    Sandbox.run()'s generic error handling (Section 4.2's exception propagation
    requirement covers KeyboardInterrupt/SystemExit explicitly; FinalAnswer is
    the sandbox's own equivalent control-flow signal and gets the same treatment).
    """
    # 【日本語解説】
    # LLMが final_answer(...) を呼んだことを表す、"エラーではない"特別な
    # 例外。Pythonでは「関数呼び出しから抜けて遠くの呼び出し元まで一気に
    # 制御を戻す」手段が例外機構くらいしかないため、あえて例外として実装
    # されている。KeyboardInterrupt/SystemExitと同格の「制御フロー用の
    # シグナル」であり、Sandbox.run()内の汎用 `except Exception:` に
    # 握りつぶされないよう、個別に再送出(raise)される（下のrun()参照）。

    def __init__(self, answer: Any) -> None:
        super().__init__(answer)
        self.answer = answer


def _is_authorized(module_name: str, authorized: list) -> bool:
    # 【日本語解説】
    # module_name（例: "math.isclose" のようなドット区切り名）が、
    # authorizedリストのいずれかのパターンにglobマッチするかを判定する。
    # fnmatch.fnmatch を使っているので "math.*" のようなワイルドカードが
    # 使える（"math.isclose" は "math.*" にマッチする）。
    return any(fnmatch.fnmatch(module_name, pattern) for pattern in authorized)


# Dunder attributes ordinary MBPP/SWE-bench solution code plausibly needs to
# access explicitly (operator overloading, iteration, context managers, ...).
# Every other private-looking attribute is denied by default (see
# check_dunder_attribute_access below) rather than trying to enumerate every
# dangerous one - the introspection surface of a live CPython process is too
# large to blocklist completely, and the escapes that matter here
# (__subclasses__, __globals__, __bases__/__base__/__mro__, __builtins__,
# __code__/__closure__, __getattribute__/__reduce__/__reduce_ex__) are exactly
# the ones a default-deny allowlist closes without asking sandboxed code to
# avoid a name it never needed anyway.
# ----------------------------------------------------------------------------
# 【日本語解説】"デフォルト拒否"のダンダー属性アローリスト（このファイルで
# 最重要のセキュリティ機構、check_dunder_attribute_access と対になる）
# ----------------------------------------------------------------------------
# Pythonの `__xxx__` という名前の属性（"ダンダー"属性、double underscore
# の意）は、演算子オーバーロード(__add__)やイテレーション(__iter__)のような
# 「正当なコードが普通に使う」ものもあれば、__subclasses__ や __globals__
# のように「Pythonインタプリタの内部構造そのものを覗き見る／操作する」
# 非常に強力なものもあります。
#
# 後者を使うと、たとえ __import__ や open を差し替えていても、次のような
# コード（Pythonの古典的なサンドボックス脱出テクニック）でサンドボックスを
# 完全に脱出できてしまいます:
#
#     ().__class__.__bases__[0].__subclasses__()
#
# これは「空タプル () のクラス(tuple) → その基底クラス(object) →
# objectを継承している、現在ロード済みの全クラスのリスト」を取得する
# コードです。ロード済みの全クラスの中には、例えば
# subprocess.Popen のようなクラスも含まれているため、そのクラスの
# `__init__.__globals__['__builtins__']` を辿ると、**制限を一切
# かけていない、本物のbuiltinsモジュール**にたどり着けてしまいます。
# つまり:
#
#     for cls in ().__class__.__bases__[0].__subclasses__():
#         if cls.__name__ == "Popen":
#             real_builtins = cls.__init__.__globals__["__builtins__"]
#             real_builtins["__import__"]("os").system("...")  # 脱出成功
#
# なぜこれが__import__/openの差し替えだけでは防げないのか?
# ── _make_restricted_import() や _make_restricted_open() は、
# 「サンドボックスコードが `import os` や `open(...)` という
# **名前を直接ルックアップした**とき」だけをフックしています。
# しかし上記の脱出コードは、サンドボックスが一度も名前として
# 手渡していない「既にロード済みのオブジェクト」を辿って、その
# 属性(__bases__, __subclasses__, __globals__...)を読んでいるだけです。
# 名前ルックアップの差し替えは、この種の「任意オブジェクトへの
# 属性アクセスによる内省」には無力です。
#
# 対策として、このプロジェクトは「危険そうな属性を1つずつ列挙して
# 禁止する」のではなく、逆に「正当なコードが本当に必要とする最小限の
# ダンダー属性だけ」を _SAFE_DUNDER_ATTRS としてアローリスト化し、
# それ以外の "_" で始まる属性名へのアクセスを**すべて**拒否します
# （check_dunder_attribute_access / _is_forbidden_attribute）。
# これにより、__subclasses__, __globals__, __bases__/__base__/__mro__,
# __builtins__, __code__/__closure__, __getattribute__, __reduce__/
# __reduce_ex__ といった危険な属性を個別に思いつく必要なく、まとめて
# 塞げます（生きたCPythonプロセスの内省サーフェスは膨大すぎて、
# ブロックリスト方式では網羅しきれないため）。
# ----------------------------------------------------------------------------
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
    # 【日本語解説】
    # 「この属性名へのアクセスを拒否すべきか」を判定する中心関数。
    # 静的チェック(check_dunder_attribute_access)と動的チェック
    # (_make_restricted_getattr / _make_restricted_setattr /
    #  _RestrictedModule.__getattribute__)の**全て**がこの1つの関数を
    # 呼び出しているため、判定ルールが1箇所に集約されている（ルールを
    # 変えるときにここだけ直せばよい、という設計）。
    #
    # 条件は2つのORで、どちらかに当てはまれば禁止:
    #   1. name が _FORBIDDEN_PUBLIC_ATTRIBUTES（現状は "format" のみ）
    #      に含まれる ── 一見公開属性に見えても個別に禁止したいもの。
    #   2. name が "_" で始まり、かつ _SAFE_DUNDER_ATTRS のアローリストに
    #      含まれていない ── これが「デフォルト拒否」の本体。
    return (
        isinstance(name, str)
        and (
            name in _FORBIDDEN_PUBLIC_ATTRIBUTES
            or (name.startswith("_") and name not in _SAFE_DUNDER_ATTRS)
        )
    )


def check_dunder_attribute_access(tree: ast.AST) -> None:
    """Reject explicit private-attribute access outside _SAFE_DUNDER_ATTRS.

    Closes the classic in-process sandbox escape:
    ``().__class__.__bases__[0].__subclasses__()`` walks already-loaded
    classes to find one whose ``__init__.__globals__['__builtins__']`` is the
    *real*, unrestricted builtins - completely bypassing the restricted
    __import__/open in this sandbox's namespace, since those only guard name
    *lookups* in sandboxed code, not arbitrary introspection of objects that
    were never looked up through them. _make_restricted_getattr below closes
    the same escape reached dynamically via ``getattr(obj, "__subclasses__")``
    instead of dot syntax.

    It also prevents allowlisted modules from exposing privileged modules through
    private implementation details such as ``random._os``. Public module-valued
    attributes are checked separately by _RestrictedModule below.

    ``str.format`` is also denied because its attribute mini-language parses
    names from a string at runtime, bypassing AST Attribute checks (e.g.
    ``"{0.__class__}".format(x)``). F-strings and ordinary string operations
    remain available.
    """
    # 【日本語解説】
    # ここは「静的」チェックです。ast.walk(tree) でコードの構文木を
    # 全ノード巡回し、`obj.attr` という**ドット記法での属性アクセス**
    # (ast.Attribute ノード)を見つけるたびに、その属性名 node.attr が
    # 禁止対象かどうかを _is_forbidden_attribute() で判定します。
    # 禁止対象なら SandboxViolation を送出してコード実行そのものを
    # 未然に止めます（compile()にすら到達させない）。
    #
    # 注意: これは「静的」チェックなので、`obj.__subclasses__` のように
    # ソースコード上に**そのままの文字列として属性名が書かれている**
    # 場合しか検出できません。`getattr(obj, "__sub" + "classes__")` の
    # ように実行時に文字列を組み立てて渡すケースはこの静的チェックを
    # すり抜けます。そのバイパスは _make_restricted_getattr() が
    # 実行時（動的）に同じルールを再チェックすることで塞いでいます。
    # 「静的で先回りして止める」＋「動的で取りこぼしを拾う」の
    # 二重チェックになっている点に注目してください。
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_forbidden_attribute(node.attr):
            raise SandboxViolation(f"access to '{node.attr}' is not permitted")


def check_imports(tree: ast.AST, authorized: list) -> None:
    """Static check: reject any Import/ImportFrom node outside the allowlist.

    This alone can be bypassed by calling ``__import__("os")`` as a plain
    function rather than writing an import statement, which is why
    _make_restricted_import below re-checks at call time regardless of how
    the sandboxed code reached it.
    """
    # 【日本語解説】
    # こちらも静的チェック。ast.Import（`import os` 形式）と
    # ast.ImportFrom（`from os import path` 形式）のノードを構文木から
    # 探し、モジュール名がauthorizedリストに含まれるかを確認します。
    # ImportFromでは `from os import *` のような**star import**も
    # 個別に禁止しています（*で何が入ってくるか静的に予測できず、
    # アローリストの意味が薄れてしまうため）。
    #
    # docstringにある通り、このチェックだけでは
    # `__import__("os")` という**関数呼び出し**によるバイパスを
    # 防げません（import文ではなくただの関数呼び出しなので、
    # ast.Import/ImportFromノードとして現れないから）。そのため
    # _make_restricted_import() が builtins.__import__ 自体を
    # 差し替えて、実行時にも同じアローリストを強制します。
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
    """Read-only view of an allowlisted module.

    Returning a real module from restricted ``__import__`` is unsafe even when
    the module name itself is allowlisted: several standard-library modules
    retain privileged modules in their globals (for example ``random._os`` and
    ``typing.sys``). This proxy blocks private attributes and refuses to expose
    nested modules unless those modules are independently allowlisted.
    """
    # 【日本語解説】
    # 「モジュール名自体はアローリストにあるから安全」とは限らない、
    # という点への対策クラス。例えば random モジュールは、内部実装として
    # `random._os`（osモジュールへの参照）を自分のグローバル名前空間に
    # 持っています。もし `import random` の結果として本物の random
    # モジュールをそのまま返してしまうと、サンドボックスコードは
    # `random._os.system(...)` のようにして、許可していないはずの
    # os モジュールに間接的にアクセスできてしまいます（typing.sys も同様）。
    #
    # この _RestrictedModule は ModuleType を継承した「読み取り専用の
    # ラッパー」で、本物のモジュールへの属性アクセスを全て
    # __getattribute__ 経由でフィルタします。危険な属性は拒否し、
    # 属性の値がさらに別のモジュールだった場合（random._os のケース）は、
    # そのモジュールが**独立してアローリストに含まれているか**を
    # 再チェックしてから返す（含まれていなければ拒否）。

    def __init__(
        self,
        module: ModuleType,
        authorized: list,
        wrap_module: Callable[[ModuleType], ModuleType],
    ) -> None:
        # 【日本語解説】
        # object.__setattr__ を直接使っているのは、このクラス自身の
        # __getattribute__ をここでは経由させたくない（初期化時に
        # まだ _restricted_module 等が設定されていない状態で
        # __getattribute__ が呼ばれると壊れる）ための回避策。
        super().__init__(module.__name__, module.__doc__)
        object.__setattr__(self, "_restricted_module", module)
        object.__setattr__(self, "_authorized_modules", authorized)
        object.__setattr__(self, "_wrap_module", wrap_module)

    def __getattribute__(self, name: str) -> Any:
        # 【日本語解説】
        # このモジュールプロキシに対する**あらゆる**属性アクセス
        # (proxy.something) がここを通ります。
        if name in {"__name__", "__doc__", "__package__"}:
            # モジュールとして最低限必要なメタ属性は素通しする。
            module = object.__getattribute__(self, "_restricted_module")
            return getattr(module, name, None)
        if _is_forbidden_attribute(name):
            # ここでも同じ _is_forbidden_attribute() を使い、
            # ダンダー属性のルールをモジュール属性アクセスにも
            # 一貫して適用している。
            raise SandboxViolation(f"access to '{name}' is not permitted")

        module = object.__getattribute__(self, "_restricted_module")
        if module.__name__ == "operator" and name in {"attrgetter", "methodcaller"}:
            # 【日本語解説】
            # operator.attrgetter / operator.methodcaller は、
            # 文字列で指定した属性名・メソッド名を実行時に解決して
            # 呼び出せる機能。str.formatと同種の「文字列から属性名を
            # 動的に組み立てる」経路になり得るため、operatorモジュール
            # 自体は許可しつつ、この2つの関数だけ個別に禁止している。
            raise SandboxViolation(f"operator.{name} is not permitted")
        if name in _FORBIDDEN_MODULE_ATTRIBUTES.get(module.__name__, set()):
            # string.Formatter などモジュール別の個別禁止属性。
            raise SandboxViolation(f"{module.__name__}.{name} is not permitted")

        value = getattr(module, name)
        if isinstance(value, ModuleType):
            # 【日本語解説】
            # ここが random._os 対策の核心。属性の値自体がモジュール
            # だった場合、それが独立してアローリストに含まれているかを
            # 再確認する。含まれていなければ SandboxViolation。
            # 含まれていれば、そのモジュールも再帰的に _RestrictedModule
            # でラップしてから返す（入れ子のモジュール参照を辿っても
            # 常に制限がかかり続けるようにするため）。
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
    # 【日本語解説】
    # `__import__` 自体を差し替えるためのクロージャを組み立てるファクトリ
    # 関数。これが check_imports() の静的チェックだけでは防げない
    # `__import__("os")` という**関数呼び出し形式のバイパス**を、
    # 実行時にも同じアローリストで再チェックすることで塞いでいる。
    real_import = builtins.__import__
    proxy_cache: Dict[str, ModuleType] = {}  # 同じモジュールを何度importしても同じプロキシを再利用する

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
        # 【日本語解説】
        # `import foo` 文も `__import__("foo")` の関数呼び出しも、
        # 最終的にはこの関数を通る（Pythonのimport文は内部的に
        # __import__を呼ぶ実装になっているため）。まずアローリストで
        # 名前を確認し、通れば本物の__import__(real_import)を呼んで
        # 実際にモジュールをロードし、その結果を必ず wrap_module() で
        # ラップしてから返す ── 生のモジュールオブジェクトが
        # サンドボックスコードに渡ることは決してない。
        if not _is_authorized(name, authorized):
            raise SandboxViolation(f"import of '{name}' is not permitted")
        module = real_import(name, globals, locals, fromlist, level)
        return wrap_module(module)

    return restricted_import


def _make_restricted_open(allowed_directories: list) -> Callable[..., Any]:
    # 【日本語解説】
    # 組み込みの open() を差し替えるファクトリ。SandboxConfig の
    # allowed_directories に列挙されたディレクトリの配下だけ、
    # ファイルの読み書きを許可する。
    real_open = builtins.open
    # os.path.realpath で、シンボリックリンクや ".." を含む相対パスを
    # あらかじめ正規化しておく（allowed_directories側もここで正規化）。
    resolved_allowed = [os.path.realpath(d) for d in allowed_directories]

    def restricted_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, bytes, os.PathLike)):
            # 【日本語解説】
            # 渡されたパスも同様に realpath() で正規化してから比較する。
            # これにより `open("/testbed/../etc/passwd")` のような
            # ディレクトリトラバーサル攻撃や、シンボリックリンク経由で
            # 許可ディレクトリの外に出ようとする経路を弾ける。
            target = os.path.realpath(os.fspath(file))
            allowed = any(target == base or target.startswith(base + os.sep) for base in resolved_allowed)
            if not allowed:
                raise SandboxViolation(
                    f"path '{file!r}' is outside the allowed directories {allowed_directories}"
                )
        return real_open(file, mode, *args, **kwargs)

    return restricted_open


def _make_restricted_getattr() -> Callable[..., Any]:
    # 【日本語解説】
    # 組み込みの getattr() を差し替える。これが
    # check_dunder_attribute_access() の**静的**チェックをすり抜ける
    # 動的バイパス ── `getattr(obj, "__sub" + "classes__")` のように
    # 実行時に文字列を組み立てて属性名を渡すケース ── を塞ぐための
    # 「動的」版の対策。ロジックは _is_forbidden_attribute() を
    # 呼ぶだけで、静的チェックと**全く同じルール**を使っている点が重要
    # （ルールが1箇所に集約されているので、片方だけ穴が開くことがない）。
    real_getattr = builtins.getattr

    def restricted_getattr(obj: Any, name: Any, *default: Any) -> Any:
        if _is_forbidden_attribute(name):
            raise SandboxViolation(f"access to '{name}' is not permitted")
        return real_getattr(obj, name, *default)

    return restricted_getattr


def _make_restricted_setattr() -> Callable[..., Any]:
    # 【日本語解説】
    # 同様に setattr() も差し替える。属性の"読み取り"だけでなく
    # "書き込み"側も同じルールで制限しないと、例えば危険な属性を
    # 書き換えて防御そのものを無効化するような経路が残ってしまう
    # ため（例: setattr(obj, "__class__", 何か) のような操作）。
    real_setattr = builtins.setattr

    def restricted_setattr(obj: Any, name: Any, value: Any) -> None:
        if _is_forbidden_attribute(name):
            raise SandboxViolation(f"access to '{name}' is not permitted")
        real_setattr(obj, name, value)

    return restricted_setattr


def final_answer(answer: Any) -> None:
    """Injected into every sandbox namespace - NOT an MCP tool (Section 4.2)."""
    # 【日本語解説】
    # LLMが「これが最終解答だ」と申告するために呼ぶ関数。MCPサーバー
    # 経由のツールとは違い、サンドボックスの名前空間に直接注入される
    # （_build_namespace() 参照）。呼ばれると即座に FinalAnswer 例外を
    # 送出し、Orchestrator側までその例外が伝播してエージェントループを
    # 終了させる（Section 5参照）。
    raise FinalAnswer(answer)


def _alarm_handler(signum: int, frame: Any) -> None:
    # 【日本語解説】
    # signal.alarm() が時間切れになったときにOSから呼ばれるハンドラ。
    # 単に SandboxTimeoutError を送出するだけ。Pythonのシグナルハンドラは
    # 「次にバイトコードが実行されるタイミングで」割り込むので、
    # 無限ループのような重い処理の途中でも比較的早く割り込める
    # （C拡張の中でブロックしている場合などは例外）。
    raise SandboxTimeoutError()


class Sandbox:
    """Executes untrusted, LLM-generated Python under the configured restrictions."""
    # 【日本語解説】
    # このファイルの公開API。agent_mbpp.py / agent_swebench.py /
    # mcp_tools_mbpp.py など、サンドボックスを使いたいコードは
    # すべてこのクラスをインスタンス化して使う。

    def __init__(
        self,
        config: Optional[SandboxConfig] = None,
        extra_namespace: Optional[Dict[str, Callable]] = None,
        apply_process_memory_limit: bool = True,
        isolated: bool = True,
    ) -> None:
        # 【日本語解説】
        # config が省略された場合はデフォルト値(DEFAULT_AUTHORIZED_IMPORTS
        # など)を使う。extra_namespace は、MCPツールのラッパー関数群
        # （sandbox/mcp_client.py の MCPToolProxy.build_namespace() が
        # 生成したもの）をサンドボックスの名前空間に追加注入するための
        # 引数 ── これによりサンドボックス内のコードから
        # `result = search_code("foo")` のように、ただのPython関数を
        # 呼ぶのと同じ感覚でMCPツールを使える。
        if config is None:
            config = SandboxConfig(
                authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,
                allowed_directories=DEFAULT_ALLOWED_DIRECTORIES,
            )
        extra = extra_namespace or {}
        # 予約名（__builtins__, final_answer）をMCPツール側が上書き
        # しようとしていないか、初期化時点で即座にチェックする。
        collisions = sorted(_RESERVED_GLOBAL_NAMES & extra.keys())
        if collisions:
            raise ValueError(f"extra_namespace contains reserved name(s): {', '.join(collisions)}")

        self.config = config
        self._isolated_process: Optional[IsolatedSandboxProcess] = None
        if isolated:
            # 【日本語解説】
            # デフォルト経路（isolated=True）。実際のコード実行は
            # このプロセス自身では行わず、IsolatedSandboxProcess
            # （sandbox/isolated_process.py）に**丸ごと委譲**する。
            # そちらが unshare + bwrap で隔離された別プロセスを起動し、
            # そのプロセスの中で（isolated=False で）このクラスの
            # 残りのロジックが実際に動く、という二段構え。
            self.namespace = {}
            self._isolated_process = IsolatedSandboxProcess(
                config,
                extra,
                apply_process_memory_limit,
            )
            return

        # 【日本語解説】
        # isolated=False は「OS隔離が既に確立されたあとの、隔離ワーカー
        # プロセス自身の内部モード」。isolated_worker.py からのみ
        # 呼ばれることを想定しており、信頼できないコードを直接この
        # モードで実行してはいけない（docstringにもその旨明記されている）。
        if apply_process_memory_limit:
            self._apply_memory_limit()
        self.namespace = self._build_namespace(extra)

    def _apply_memory_limit(self) -> None:
        """Cap this process's address space so runaway allocations raise
        MemoryError instead of triggering the OS OOM killer.

        This is applied once to the worker process's lifetime. The public
        Sandbox normally runs in a dedicated worker, so lowering RLIMIT_AS
        does not affect the agent or test runner. ``isolated=False`` is an
        internal worker-only mode and should not be used for untrusted code.
        """
        # 【日本語解説】
        # resource.setrlimit(RLIMIT_AS, ...) で「このプロセスが確保できる
        # 仮想アドレス空間の総量」に上限をかける。これが無いと、
        # LLMが書いた `x = [0] * 10**12` のような暴走コードがメモリを
        # 食い尽くし、最悪の場合OS側のOOM Killerが**無関係な他プロセス**
        # を巻き添えで殺してしまう可能性がある。RLIMIT_ASを設定して
        # おけば、上限に達した瞬間にPython側で綺麗な MemoryError として
        # 捕捉できる（下のrun()のexcept MemoryError節を参照）。
        #
        # 「一度だけ、ワーカープロセスの生存期間全体に対して」適用される
        # 点に注意 ── このプロセス自体のメモリ上限を下げる操作なので、
        # 通常のSandboxはこの処理が走る隔離ワーカーの中でのみ実行される。
        # pytestプロセス自身やagent本体のプロセスには影響しない
        # （もしisolated=Falseでagent本体プロセスにこれを適用すると、
        #  agent自身のメモリまで制限されてしまうため要注意）。
        if resource is None or not hasattr(resource, "RLIMIT_AS"):
            return
        limit_bytes = self.config.max_memory_mb * 1024 * 1024
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            new_hard = hard if hard != resource.RLIM_INFINITY and hard < limit_bytes else limit_bytes
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, new_hard))
        except (ValueError, OSError):
            pass  # best-effort: some sandboxed CI environments forbid lowering limits further
            # 【日本語解説】CI環境などでは、既にリソース制限が別の仕組みで
            # かけられていて、これ以上厳しくする操作自体が権限エラーに
            # なることがある。その場合は「ベストエフォート」として
            # 静かに諦める（メモリ制限が失敗してもOS隔離という
            # もう1つの防御層は依然として機能しているため）。

    def _build_namespace(self, extra_namespace: Dict[str, Callable]) -> dict:
        # 【日本語解説】
        # LLM生成コードを exec() するときに使う「グローバル名前空間」の
        # 辞書を組み立てる。ここで作られる辞書こそが、サンドボックスの
        # 制限が実際に効く場所そのもの。
        collisions = sorted(_RESERVED_GLOBAL_NAMES & extra_namespace.keys())
        if collisions:
            raise ValueError(f"extra_namespace contains reserved name(s): {', '.join(collisions)}")

        # 【日本語解説】
        # 標準の builtins モジュールが持つ全ての名前から、
        # _UNSAFE_BUILTINS に列挙された危険な名前だけを除いた辞書を作る。
        restricted_builtins = {
            name: value for name, value in vars(builtins).items() if name not in _UNSAFE_BUILTINS
        }
        # 【日本語解説】
        # その上で、__import__ / open / getattr / setattr の4つを
        # それぞれ「制限付きバージョン」に差し替える。これらは
        # _UNSAFE_BUILTINSには入っていない（完全に消すと正当なコードも
        # 動かなくなるため）が、代わりに関数の中身を丸ごと差し替えて
        # チェック機構を組み込んでいる。
        restricted_builtins["__import__"] = _make_restricted_import(self.config.authorized_imports)
        restricted_builtins["open"] = _make_restricted_open(self.config.allowed_directories)
        restricted_builtins["getattr"] = _make_restricted_getattr()
        restricted_builtins["setattr"] = _make_restricted_setattr()

        # 【日本語解説】
        # exec()に渡すグローバル辞書は、通常のPythonモジュール実行と
        # 同じ形で "__builtins__" キーに上のrestricted_builtinsを持つ。
        # これにより、サンドボックスコードが `open(...)` と書いたとき、
        # Python内部の名前解決ルールに従って自動的にこの制限版が使われる
        # （明示的にimportし直す必要はない）。final_answerも同様に
        # トップレベル関数として直接注入されている。
        namespace: dict = {"__builtins__": restricted_builtins, "final_answer": final_answer}
        namespace.update(extra_namespace)  # MCP tool wrappers, if any (Section 4.2)
        return namespace

    def run(self, code: str) -> str:
        """Execute one snippet and return its captured stdout, or an explicit error string.

        Never raises for ordinary code errors - those come back as
        "[ErrorKind] ..." text so the agent loop can feed them to the LLM as an
        Observation (Section 4.1's mandatory explicit-feedback requirement).
        FinalAnswer, KeyboardInterrupt, and SystemExit are the only things
        propagated to the caller.
        """
        # 【日本語解説】
        # このメソッドこそが「LLMが書いた1ターン分のコードを実行する」
        # 唯一の入口。返り値ポリシーが重要: **通常のコードエラーで
        # 例外を投げることは絶対にない**。すべて"[ErrorKind] ..."という
        # 文字列に変換して返す ── これは Orchestrator（Section 5）が
        # その文字列をそのままLLMへの次のObservationとして使えるように
        # するための設計。FinalAnswer / KeyboardInterrupt / SystemExit
        # の3つだけが例外のまま呼び出し元へ伝播する（制御フロー用の
        # シグナルであり、"エラー"ではないため）。
        if self._isolated_process is not None:
            # isolated=True（通常経路）の場合、実処理は
            # IsolatedSandboxProcess.run() に丸ごと委譲する。
            return self._isolated_process.run(code)

        # 【日本語解説】
        # ここから先は isolated=False（隔離ワーカー内部）での実処理。
        if not code.strip():
            return "[NoCodeBlock] The submitted code was empty."

        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            # 構文エラーはコンパイルすら試みず、その場でLLMに返す。
            return f"[SyntaxError] {exc}"

        try:
            # 【日本語解説】
            # 実行前に、まず静的チェックを2つとも通す:
            #   1. check_imports: importのアローリスト違反がないか
            #   2. check_dunder_attribute_access: 危険なダンダー属性への
            #      ドット記法アクセスがないか
            # どちらかに違反があれば、compile()にすら進まずここで
            # 弾かれる ── 「実行してから制限にかかる」のではなく、
            # 「実行する前に構文レベルで拒否する」という先回りの防御。
            check_imports(tree, self.config.authorized_imports)
            check_dunder_attribute_access(tree)
        except SandboxViolation as exc:
            return f"[SandboxViolation] {exc}"

        compiled = compile(tree, "<agent>", "exec")
        output = io.StringIO()
        has_alarm = hasattr(signal, "SIGALRM")
        previous_handler = None
        if has_alarm:
            # 【日本語解説】
            # signal.alarm() はUnix系OS限定の機能（Windowsには無い）。
            # has_alarmでその可用性を確認したうえで、SIGALRMハンドラを
            # 一時的にこの実行専用のものに差し替え、
            # max_execution_time_seconds秒後にアラームが鳴るよう予約する。
            previous_handler = signal.signal(signal.SIGALRM, _alarm_handler)
            signal.alarm(self.config.max_execution_time_seconds)

        try:
            # 【日本語解説】
            # ここが実際にコードを実行する箇所。
            # contextlib.redirect_stdout(output) で標準出力を
            # StringIOにキャプチャしながら、exec(compiled, self.namespace)
            # で先ほど組み立てた制限付き名前空間の中でコードを実行する。
            # namespaceが第2引数（globals）としてしか渡されていないので、
            # コード内で定義された変数はすべてこのnamespace辞書に
            # 書き込まれる ── これが「変数がターン間で永続する」仕組み
            # （Sandboxインスタンスがnamespaceを保持し続ける限り）。
            with contextlib.redirect_stdout(output):
                exec(compiled, self.namespace)
            return self._truncate(output.getvalue())
        except SandboxTimeoutError:
            # タイムアウトしても、それまでに出力されていた部分結果は
            # 捨てずにLLMへ見せる（何が起きていたかのヒントになるため）。
            partial = self._truncate(output.getvalue())
            return (
                f"[Timeout] Execution exceeded {self.config.max_execution_time_seconds}s "
                f"and was interrupted. Partial output before timeout:\n{partial}"
            )
        except MemoryError:
            # _apply_memory_limit() で設定したRLIMIT_ASに引っかかると
            # ここに来る。
            return (
                f"[MemoryLimitExceeded] Execution exceeded "
                f"{self.config.max_memory_mb}MB and was interrupted."
            )
        except SandboxViolation as exc:
            # 静的チェックをすり抜けた動的バイパス
            # （_make_restricted_import/_open/_getattr/_setattrや
            #  _RestrictedModuleが実行時に検知したもの）はここに来る。
            return f"[SandboxViolation] {exc}"
        except FinalAnswer:
            # 【日本語解説】
            # ここが「握りつぶさない」ことの実装そのもの。下に汎用の
            # `except Exception` があるが、その手前でFinalAnswerだけを
            # 個別に再送出(raise)することで、汎用ハンドラに捕まる前に
            # 呼び出し元まで確実に伝播させている。
            raise
        except (KeyboardInterrupt, SystemExit):
            # 同様にプロセス制御用のシグナルも握りつぶさず伝播させる。
            raise
        except Exception as exc:  # noqa: BLE001 - intentional: all other errors become Observations
            # 【日本語解説】
            # ここが「それ以外の全てのエラー」の受け皿。ZeroDivisionError
            # やTypeErrorのような、ごく普通のPythonの実行時エラーは
            # すべてここに落ちてきて、例外のまま外に投げるのではなく
            # 文字列に変換してLLMに返す ── LLM自身がエラーメッセージを
            # 読んで次のコードを直せるようにするため（Section 4.1の
            # 「暗黙に何かをしたら必ずそれをLLMに伝える」方針の一部）。
            return f"[Error] {type(exc).__name__}: {exc}"
        finally:
            # 【日本語解説】
            # 成功・失敗どちらの経路でも、必ずアラームを解除
            # (signal.alarm(0))し、シグナルハンドラを元に戻す。
            # これを忘れると、次にrun()を呼んだときに前回のタイマーが
            # 残っていて予期せぬタイミングでタイムアウトしたり、
            # このSandbox専用のハンドラが他のコードに影響したりする
            # バグの元になる。
            if has_alarm:
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous_handler)

    def _truncate(self, text: str) -> str:
        # 【日本語解説】
        # 標準出力が長すぎるとLLMのトークン予算を圧迫するため、
        # max_output_chars（SandboxConfig側の設定、デフォルト20,000文字）
        # を超えた分は切り捨て、「何文字省略したか」を明示するメッセージを
        # 末尾に付ける。省略したことを黙って隠さない、という一貫した方針。
        limit = self.config.max_output_chars
        if len(text) <= limit:
            return text
        omitted = len(text) - limit
        return (
            text[:limit]
            + f"\n[TruncatedOutput] {omitted} additional characters were cut off "
            f"(output limit: {limit} chars)."
        )

    def close(self) -> None:
        """Stop the isolated worker, if this Sandbox owns one."""
        # 【日本語解説】
        # isolated=Trueで作られたSandboxが所有する、常駐ワーカー
        # プロセス（unshare/bwrap配下）を終了させる。isolated=Falseの
        # 場合（＝ワーカー自身の内部モード）は_isolated_processが
        # Noneなので何もしない。
        if self._isolated_process is not None:
            self._isolated_process.close()

    def __del__(self) -> None:
        # 【日本語解説】
        # Sandboxオブジェクトがガベージコレクトされる際の保険として、
        # close()し忘れても隔離ワーカープロセスが残り続けないようにする。
        # __del__内で例外を投げるとPython側で無視されつつ警告が出るなど
        # 扱いにくいため、明示的に握りつぶしている。
        try:
            self.close()
        except Exception:
            pass


if __name__ == "__main__":
    # 【日本語解説】
    # このファイルを直接 `python executor.py` として実行したときの
    # 簡易動作確認用コード。デフォルト設定でSandboxを作り、
    # 許可されているmathモジュールを使った簡単な計算コードを実行して
    # みせるだけの、最小のスモークテスト。
    sandbox = Sandbox()
    demo_code = "import math\ndef test():\n    return sum([1, 2, 3, 4]) / 4\nprint(test())"
    print(sandbox.run(demo_code))
