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

import ast  # コードを構文木として静的解析するため(import禁止・dunder属性禁止のチェックに使用)
import builtins  # 制限済みbuiltins辞書を組み立てるために元のbuiltinsを参照
import contextlib  # 標準出力のリダイレクト(redirect_stdout)に使用
import fnmatch  # 許可importパターン("math.*"等)のワイルドカードマッチに使用
import io  # 標準出力キャプチャ用のインメモリバッファ
import os  # パス正規化やファイルアクセス制限のチェックに使用
import signal  # SIGALRMによる実行時間タイムアウトの実装に使用
from collections.abc import Callable  # 型ヒント用
from types import ModuleType  # モジュールオブジェクトかどうかの判定・型ヒントに使用
from typing import Any, Dict, Optional

from models import SandboxConfig  # サンドボックスの設定(許可import・許可ディレクトリ等)を表す型
from sandbox.isolated_process import IsolatedSandboxProcess  # OS隔離ワーカーを制御するコントローラ

try:
    import resource  # プロセスのメモリ上限(RLIMIT_AS)設定に使用(Unix専用モジュール)
except ImportError:  # pragma: no cover - resourceはUnix系OS専用のため
    resource = None  # type: ignore[assignment]  # Windows等では利用不可なのでNoneにしておく

# サンドボックス内コードがデフォルトでimportを許可されるモジュール(またはパターン)の一覧。
# "math.*"のようなワイルドカードはmathパッケージのサブモジュールを許可する。
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

# デフォルトでファイルアクセスが許可されるディレクトリ一覧
DEFAULT_ALLOWED_DIRECTORIES = ["/testbed", "/tmp/agent"]

# サンドボックス化されたコードが以下の制限を回避できてしまう危険なbuiltins群。
# eval/execは任意コード実行、__import__/openは制限をバイパスした直接呼び出し、
# vars()はオブジェクトの__dict__経由での内部状態アクセスに使われうるため、通常のbuiltinsから除外する。
_UNSAFE_BUILTINS = {
    "eval", "exec", "compile", "input", "breakpoint",
    "help", "exit", "quit", "__import__", "open", "vars",
}

_RESERVED_GLOBAL_NAMES = {"__builtins__", "final_answer"}  # extra_namespaceで上書きしてはいけない予約名
_FORBIDDEN_PUBLIC_ATTRIBUTES = {"format"}  # 公開属性だが危険なため常に禁止する名前(str.formatなど)
_FORBIDDEN_MODULE_ATTRIBUTES = {"string": {"Formatter"}}  # モジュールごとに個別禁止する属性(string.Formatterは書式ミニ言語経由の脱出経路になるため)


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
        super().__init__(answer)  # 基底のExceptionにも回答値を渡しておく(トレースバック表示等のため)
        self.answer = answer  # 呼び出し元が取り出せるよう回答値を属性として保持


def _is_authorized(module_name: str, authorized: list) -> bool:
    # module_nameが許可リストのいずれかのパターンにfnmatch(ワイルドカード)で一致するか判定
    return any(fnmatch.fnmatch(module_name, pattern) for pattern in authorized)


# 通常のMBPP/SWE-benchの解答コードが明示的にアクセスする可能性のあるdunder属性
# (演算子オーバーロード、イテレーション、コンテキストマネージャ等)。
# それ以外の「プライベートっぽい」属性はすべてデフォルトで拒否する(下記の
# check_dunder_attribute_accessを参照)。危険な属性を1つずつ列挙してブロック
# リスト化するのではなく許可リスト方式にしているのは、稼働中のCPython
# プロセスのイントロスペクション可能な表面積が大きすぎて完全にブロック
# リスト化できないため。ここで問題になる脱出経路(__subclasses__,
# __globals__, __bases__/__base__/__mro__, __builtins__,
# __code__/__closure__, __getattribute__/__reduce__/__reduce_ex__)は、
# まさにこのデフォルト拒否の許可リストによって塞がれる対象であり、
# サンドボックス化されたコードが元々必要としない名前を避けるよう求める
# ことにはならない。
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
    # nameが「禁止属性」かどうかを判定する:
    # (1) 文字列であり、かつ (2a) 明示的に禁止された公開属性(str.format等)である
    # か、(2b) アンダースコアで始まる「プライベートっぽい」名前で、かつ安全と
    # 認定されたdunder属性の許可リストに含まれない場合、禁止対象とみなす。
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
    for node in ast.walk(tree):  # AST内の全ノードを走査
        if isinstance(node, ast.Attribute) and _is_forbidden_attribute(node.attr):
            # 属性アクセスノードであり、かつその属性名が禁止対象なら例外を送出
            raise SandboxViolation(f"access to '{node.attr}' is not permitted")


def check_imports(tree: ast.AST, authorized: list) -> None:
    """静的チェック: 許可リスト外のImport/ImportFromノードを全て拒否する。

    この静的チェックだけでは、import文を書く代わりに``__import__("os")``を
    ただの関数として呼び出すことで回避できてしまう。そのため、下の
    _make_restricted_importでは、サンドボックス化されたコードがどの経路で
    たどり着いたかによらず、呼び出し時に改めてチェックを行っている。
    """
    for node in ast.walk(tree):  # AST内の全ノードを走査
        if isinstance(node, ast.Import):
            # "import foo, bar"形式: 各エイリアス(モジュール名)ごとに許可判定
            for alias in node.names:
                if not _is_authorized(alias.name, authorized):
                    raise SandboxViolation(f"import of '{alias.name}' is not permitted")
        elif isinstance(node, ast.ImportFrom):
            # "from foo import bar"形式: モジュール自体が許可されているか判定
            if node.module is None or not _is_authorized(node.module, authorized):
                raise SandboxViolation(f"import of '{node.module}' is not permitted")
            # "from foo import *"はどの名前がインポートされるか静的に分からず
            # チェックを回避できてしまうため一律禁止する
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
        # 見た目上は本物のモジュールのように振る舞わせるため、name/docを継承元に渡す
        super().__init__(module.__name__, module.__doc__)
        # __setattr__を経由せずobject.__setattr__で直接設定することで、
        # 後述の__getattribute__の制限ロジックに巻き込まれないようにする
        object.__setattr__(self, "_restricted_module", module)  # ラップ対象の本物のモジュール
        object.__setattr__(self, "_authorized_modules", authorized)  # 許可importパターンのリスト
        object.__setattr__(self, "_wrap_module", wrap_module)  # 入れ子モジュールを再帰的にラップする関数

    def __getattribute__(self, name: str) -> Any:
        # __name__/__doc__/__package__のような基本的なメタ属性は無条件に許可し、本物のモジュールから取得
        if name in {"__name__", "__doc__", "__package__"}:
            module = object.__getattribute__(self, "_restricted_module")
            return getattr(module, name, None)
        # それ以外の禁止属性(プライベート属性やstr.format等)へのアクセスは拒否
        if _is_forbidden_attribute(name):
            raise SandboxViolation(f"access to '{name}' is not permitted")

        module = object.__getattribute__(self, "_restricted_module")
        # operator.attrgetter/methodcallerは任意の属性アクセス・メソッド呼び出しを
        # 動的に構築できてしまいAST上のチェックを回避しうるため個別に禁止
        if module.__name__ == "operator" and name in {"attrgetter", "methodcaller"}:
            raise SandboxViolation(f"operator.{name} is not permitted")
        # モジュールごとに個別指定された禁止属性(string.Formatter等)もここで弾く
        if name in _FORBIDDEN_MODULE_ATTRIBUTES.get(module.__name__, set()):
            raise SandboxViolation(f"{module.__name__}.{name} is not permitted")

        value = getattr(module, name)  # 本物のモジュールから実際の属性値を取得
        if isinstance(value, ModuleType):
            # 取得した属性が別のモジュールだった場合(入れ子モジュール)、
            # それが独立して許可リストに載っていなければ公開せず例外を送出する
            authorized = object.__getattribute__(self, "_authorized_modules")
            if not _is_authorized(value.__name__, authorized):
                raise SandboxViolation(
                    f"module attribute '{module.__name__}.{name}' exposes "
                    f"unauthorized module '{value.__name__}'"
                )
            # 許可されているモジュールであれば、それも同様にラップしてから返す(再帰的な保護)
            wrap_module = object.__getattribute__(self, "_wrap_module")
            return wrap_module(value)
        return value  # モジュールでない普通の値(関数・定数等)はそのまま返す

    def __repr__(self) -> str:
        # デバッグ表示用: 制限付きモジュールであることが分かる文字列表現を返す
        module = object.__getattribute__(self, "_restricted_module")
        return f"<restricted module {module.__name__!r}>"


def _make_restricted_import(authorized: list) -> Callable[..., ModuleType]:
    # 制限された__import__の実装を1つ生成して返すファクトリ関数。
    # authorized(許可importパターンのリスト)をクロージャで捕捉する。
    real_import = builtins.__import__  # 本物の__import__を退避しておく(実際のimport処理に使う)
    proxy_cache: Dict[str, ModuleType] = {}  # モジュール名ごとに_RestrictedModuleを使い回すキャッシュ

    def wrap_module(module: ModuleType) -> ModuleType:
        # 本物のモジュールを_RestrictedModuleでラップする。同じモジュールに対しては
        # キャッシュした同一のプロキシを返すことでidentityの一貫性を保つ。
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
        # importの実行時にも改めて許可チェックを行う(静的チェックだけでは
        # __import__を直接関数として呼ぶ回避策を防げないため、ここが最後の砦)
        if not _is_authorized(name, authorized):
            raise SandboxViolation(f"import of '{name}' is not permitted")
        module = real_import(name, globals, locals, fromlist, level)  # 本物のimport処理を実行
        return wrap_module(module)  # 結果を制限付きプロキシでラップして返す

    return restricted_import


def _make_restricted_open(allowed_directories: list) -> Callable[..., Any]:
    # 制限されたopen()の実装を生成するファクトリ関数。allowed_directories
    # (アクセスを許可するディレクトリのリスト)をクロージャで捕捉する。
    real_open = builtins.open  # 本物のopen()を退避しておく
    # 許可ディレクトリをあらかじめ絶対パス・シンボリックリンク解決済みの実パスにしておく
    resolved_allowed = [os.path.realpath(d) for d in allowed_directories]

    def restricted_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        if isinstance(file, (str, bytes, os.PathLike)):
            # 対象パスをシンボリックリンク解決した実パスに変換してから比較することで、
            # シンボリックリンクを使った許可ディレクトリ外への脱出を防ぐ
            target = os.path.realpath(os.fspath(file))
            # 対象パスが許可ディレクトリそのもの、またはその配下にあるかを判定
            allowed = any(target == base or target.startswith(base + os.sep) for base in resolved_allowed)
            if not allowed:
                raise SandboxViolation(
                    f"path '{file!r}' is outside the allowed directories {allowed_directories}"
                )
        return real_open(file, mode, *args, **kwargs)  # 許可されていれば本物のopen()を呼ぶ

    return restricted_open


def _make_restricted_getattr() -> Callable[..., Any]:
    # 制限されたgetattr()の実装を生成するファクトリ関数。
    # ドット構文(obj.attr)ではなくgetattr(obj, "attr")のように動的に
    # 属性名を渡すケースでも、AST上のAttributeノードチェックをすり抜けられない
    # ようにするためのもの。
    real_getattr = builtins.getattr  # 本物のgetattr()を退避

    def restricted_getattr(obj: Any, name: Any, *default: Any) -> Any:
        if _is_forbidden_attribute(name):
            raise SandboxViolation(f"access to '{name}' is not permitted")
        return real_getattr(obj, name, *default)  # 許可された属性名なら本物のgetattr()を呼ぶ

    return restricted_getattr


def _make_restricted_setattr() -> Callable[..., Any]:
    # 制限されたsetattr()の実装を生成するファクトリ関数。getattrと対になる、
    # 属性書き換え経由での内部状態改変を防ぐためのもの。
    real_setattr = builtins.setattr  # 本物のsetattr()を退避

    def restricted_setattr(obj: Any, name: Any, value: Any) -> None:
        if _is_forbidden_attribute(name):
            raise SandboxViolation(f"access to '{name}' is not permitted")
        real_setattr(obj, name, value)  # 許可された属性名なら本物のsetattr()を呼ぶ

    return restricted_setattr


def final_answer(answer: Any) -> None:
    """全てのサンドボックス名前空間に注入される関数 - MCPツールではない(仕様書 4.2節)。
    エージェントが最終回答を提出するために呼ぶと、FinalAnswer例外を送出して
    そのままrun()の外側(呼び出し元)まで伝播させ、タスク完了を知らせる。"""
    raise FinalAnswer(answer)


def _alarm_handler(signum: int, frame: Any) -> None:
    # SIGALRMシグナルを受け取ったときに呼ばれるハンドラ。実行時間超過を
    # SandboxTimeoutError例外に変換し、exec()中のコードに割り込む。
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
            # 設定が渡されなければデフォルトの許可import・許可ディレクトリで構築
            config = SandboxConfig(
                authorized_imports=DEFAULT_AUTHORIZED_IMPORTS,
                allowed_directories=DEFAULT_ALLOWED_DIRECTORIES,
            )
        extra = extra_namespace or {}  # MCPツール等の追加名前空間(未指定なら空辞書)
        # extra_namespaceが__builtins__やfinal_answerといった予約名を上書きしないことを確認
        collisions = sorted(_RESERVED_GLOBAL_NAMES & extra.keys())
        if collisions:
            raise ValueError(f"extra_namespace contains reserved name(s): {', '.join(collisions)}")

        self.config = config  # 設定を保存
        self._isolated_process: Optional[IsolatedSandboxProcess] = None  # OS隔離ワーカー(未使用ならNone)
        if isolated:
            # OS隔離モード: 実際のコード実行はunshare/bwrapで隔離された別プロセスに委譲する
            self.namespace = {}  # このプロセス自身では名前空間を使わない(ワーカー側で構築される)
            self._isolated_process = IsolatedSandboxProcess(
                config,
                extra,
                apply_process_memory_limit,
            )
            return

        # isolated=False: OS隔離を使わず、このプロセス内で直接実行する
        # (ワーカープロセス自身が、既に隔離済みの状態でこのモードを使う)
        if apply_process_memory_limit:
            self._apply_memory_limit()  # プロセスのメモリ上限を設定
        self.namespace = self._build_namespace(extra)  # 制限付きexec()名前空間を構築

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
            return  # resourceモジュールが使えない(Windows等)、またはRLIMIT_AS未対応なら何もしない
        limit_bytes = self.config.max_memory_mb * 1024 * 1024  # 設定値(MB)をバイト数に変換
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)  # 現在のソフト・ハード上限を取得
            # ハード上限が無制限、または新しい上限より大きい場合のみ新しい上限に合わせて下げる
            new_hard = hard if hard != resource.RLIM_INFINITY and hard < limit_bytes else limit_bytes
            resource.setrlimit(resource.RLIMIT_AS, (limit_bytes, new_hard))  # 実際に上限を適用
        except (ValueError, OSError):
            pass  # ベストエフォート: 一部のサンドボックス化されたCI環境ではこれ以上上限を下げられないことがある

    def _build_namespace(self, extra_namespace: Dict[str, Callable]) -> dict:
        # exec()に渡す名前空間(グローバル辞書)を構築する。
        # extra_namespaceが予約名と衝突していないか改めて確認
        collisions = sorted(_RESERVED_GLOBAL_NAMES & extra_namespace.keys())
        if collisions:
            raise ValueError(f"extra_namespace contains reserved name(s): {', '.join(collisions)}")

        # 通常のbuiltinsから危険なもの(_UNSAFE_BUILTINS)を除いたコピーを作る
        restricted_builtins = {
            name: value for name, value in vars(builtins).items() if name not in _UNSAFE_BUILTINS
        }
        # __import__/open/getattr/setattrは個別の制限版に差し替える
        restricted_builtins["__import__"] = _make_restricted_import(self.config.authorized_imports)
        restricted_builtins["open"] = _make_restricted_open(self.config.allowed_directories)
        restricted_builtins["getattr"] = _make_restricted_getattr()
        restricted_builtins["setattr"] = _make_restricted_setattr()

        # 制限済みbuiltinsとfinal_answer関数を持つ基本の名前空間を作る
        namespace: dict = {"__builtins__": restricted_builtins, "final_answer": final_answer}
        namespace.update(extra_namespace)  # MCPツールのラッパー関数があればここで追加(仕様書 4.2節)
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
            # OS隔離モードの場合は、実際の実行を隔離ワーカープロセス側に委譲する
            return self._isolated_process.run(code)

        if not code.strip():
            return "[NoCodeBlock] The submitted code was empty."  # 空コードは実行せずエラー文字列を返す

        try:
            tree = ast.parse(code)  # コード文字列をASTにパース
        except SyntaxError as exc:
            return f"[SyntaxError] {exc}"  # 構文エラーはそのままエラーメッセージとして返す

        try:
            # importの静的チェックとdunder属性アクセスの静的チェックを実行前に行う
            check_imports(tree, self.config.authorized_imports)
            check_dunder_attribute_access(tree)
        except SandboxViolation as exc:
            return f"[SandboxViolation] {exc}"  # 制限違反が見つかれば実行せずに終える

        compiled = compile(tree, "<agent>", "exec")  # ASTを実行可能なコードオブジェクトにコンパイル
        output = io.StringIO()  # 標準出力をキャプチャするためのバッファ
        has_alarm = hasattr(signal, "SIGALRM")  # SIGALRMが使える環境かどうか(Windowsでは使えない)
        previous_handler = None
        if has_alarm:
            # SIGALRMハンドラを差し替え、設定された最大実行時間後にアラームが鳴るようにする
            previous_handler = signal.signal(signal.SIGALRM, _alarm_handler)
            signal.alarm(self.config.max_execution_time_seconds)

        try:
            with contextlib.redirect_stdout(output):
                exec(compiled, self.namespace)  # 制限済み名前空間の中でコードを実際に実行
            return self._truncate(output.getvalue())  # キャプチャした標準出力を(必要なら切り詰めて)返す
        except SandboxTimeoutError:
            # タイムアウトが発生した場合、それまでに出力されていた分だけでも返す
            partial = self._truncate(output.getvalue())
            return (
                f"[Timeout] Execution exceeded {self.config.max_execution_time_seconds}s "
                f"and was interrupted. Partial output before timeout:\n{partial}"
            )
        except MemoryError:
            # _apply_memory_limitで設定した上限に達した場合の専用エラーメッセージ
            return (
                f"[MemoryLimitExceeded] Execution exceeded "
                f"{self.config.max_memory_mb}MB and was interrupted."
            )
        except SandboxViolation as exc:
            # 実行時に検出された制限違反(制限import・open・getattr/setattr経由)
            return f"[SandboxViolation] {exc}"
        except FinalAnswer:
            raise  # final_answer()による正常終了シグナルはそのまま呼び出し元に伝播させる
        except (KeyboardInterrupt, SystemExit):
            raise  # 割り込み・明示的終了もそのまま呼び出し元に伝播させる
        except Exception as exc:  # noqa: BLE001 - 意図的: それ以外のあらゆるエラーはObservationにする
            return f"[Error] {type(exc).__name__}: {exc}"
        finally:
            if has_alarm:
                # 実行が終わったら必ずアラームを解除し、シグナルハンドラも元に戻す
                signal.alarm(0)
                signal.signal(signal.SIGALRM, previous_handler)

    def _truncate(self, text: str) -> str:
        # 出力文字列が設定上限を超えていれば切り詰めて、切り詰めた旨のメッセージを付加する
        limit = self.config.max_output_chars
        if len(text) <= limit:
            return text  # 上限以内ならそのまま返す
        omitted = len(text) - limit  # 省略された文字数を計算
        return (
            text[:limit]
            + f"\n[TruncatedOutput] {omitted} additional characters were cut off "
            f"(output limit: {limit} chars)."
        )

    def close(self) -> None:
        """このSandboxがOS隔離ワーカーを所有している場合、それを停止する。"""
        if self._isolated_process is not None:
            self._isolated_process.close()

    def __del__(self) -> None:
        # ガベージコレクション時にも後始末を試みる(ベストエフォート)。
        # __del__内での例外はインタプリタに警告を出すだけで邪魔になるため握りつぶす。
        try:
            self.close()
        except Exception:
            pass


if __name__ == "__main__":
    # このファイルを直接実行した場合の簡単な動作確認用デモコード
    sandbox = Sandbox()  # デフォルト設定でサンドボックスを生成
    demo_code = "import math\ndef test():\n    return sum([1, 2, 3, 4]) / 4\nprint(test())"  # サンプルコード
    print(sandbox.run(demo_code))  # 実行結果を表示
