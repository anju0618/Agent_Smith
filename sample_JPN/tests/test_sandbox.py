"""Tests for sandbox/executor.py (Section 4.2's security constraints).

Memory-limit enforcement is tested through the isolated worker (see
test_memory_limit_is_enforced), so lowering RLIMIT_AS cannot affect the pytest
process or later tests in this session.
"""
# ============================================================================
# 日本語解説: このファイルは sandbox/executor.py（LLMが生成した「信頼できない
# コード」を安全に実行するためのサンドボックス本体）をテストするファイルです。
#
# LLMが書いたコードは、悪意が無くても（あるいはあっても）危険なことをしようと
# する可能性があります。このサンドボックスは多層防御になっていて、
#   1. importのホワイトリストチェック（静的AST解析 + 実行時の __import__ パッチ）
#   2. ファイルアクセス制限（open() を許可ディレクトリだけに制限）
#   3. 危険な組み込み関数の除去（eval/exec/compile/input/__import__/open/vars）
#   4. 「サンドボックスエスケープ」対策（後述）
#   5. タイムアウト（SIGALRM）とメモリ上限（RLIMIT_AS）
# という複数の層を持っています。このテストファイルの各関数名は、
# それぞれがどの防御層・どの攻撃手法にピンポイントで対応しているかを
# そのままテスト名にしているので、テスト名の一覧＝脅威モデルの一覧として
# 読むことができます。
#
# 特に重要な概念:「サンドボックスエスケープ」とは、Pythonのオブジェクトが
# 誰でも辿れる属性（__class__ や __subclasses__ など）を持っていることを
# 悪用し、サンドボックスが一度も許可していない「本物の制限なしbuiltins」
# オブジェクトに、名前ルックアップを経由せず直接辿り着いてしまう手法です。
# 古典的な例:
#     ().__class__.__bases__[0].__subclasses__()
# これは「空のタプル→そのクラス(tuple)→その親クラス(object)→objectを継承する
# 全クラスの一覧」を辿り、その中から「__init__.__globals__['__builtins__']が
# 制限されていない本物のbuiltinsであるクラス」を見つけ出す手口です。
# __import__ や open を差し替えるだけの対策では、この経路は防げません
# （名前で "os" をimportしようとしたわけではなく、既にロード済みのオブジェクトを
# 属性アクセスで辿っているだけだから）。そのため executor.py は
# 「_ で始まる名前で、かつ明示的に許可されたダンダー属性のリストに無いものは
# 全部拒否する」というデフォルト拒否方式のアローリストを持っています。
# ============================================================================
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import List, Optional

import pytest

from models import SandboxConfig
from sandbox.executor import DEFAULT_AUTHORIZED_IMPORTS, FinalAnswer, Sandbox


def _sandbox(
    allowed_directories: Optional[List[str]] = None, authorized_imports: Optional[List[str]] = None
) -> Sandbox:
    # テスト用のSandboxインスタンスを組み立てるヘルパー関数。
    # apply_process_memory_limit=False にしているのは、このヘルパーで作る
    # サンドボックスは「同じpytestプロセス内」で動くものが多いため、ここで
    # メモリ上限(RLIMIT_AS)をかけてしまうとpytestプロセス自体やその後の
    # テストにまで影響してしまうから（メモリ上限そのもののテストは、
    # 下の test_memory_limit_is_enforced のように別プロセスに分離して行う）。
    imports = authorized_imports if authorized_imports is not None else DEFAULT_AUTHORIZED_IMPORTS
    directories = allowed_directories if allowed_directories is not None else []
    config = SandboxConfig(authorized_imports=imports, allowed_directories=directories)
    return Sandbox(config, apply_process_memory_limit=False)


def test_authorized_import_is_allowed() -> None:
    # 正常系: authorized_imports に含まれるモジュール（この場合はデフォルトの
    # ホワイトリストに含まれる math）は普通にimportして使えることを確認する。
    # 防御を固めすぎて正当な用途まで壊していないか、という基本の健全性チェック。
    sandbox = _sandbox()
    output = sandbox.run("import math\nprint(math.sqrt(4))")
    assert output.strip() == "2.0"


def test_authorized_nested_and_from_imports_are_allowed() -> None:
    # import collections.abc のような「入れ子のモジュール」形式や、
    # from math import sqrt のような「from import」形式でも、
    # 許可されたモジュールなら正しく通ることを確認する。
    sandbox = _sandbox()
    output = sandbox.run(
        "import collections.abc\nfrom math import sqrt\n"
        "print(isinstance([], collections.abc.Iterable), sqrt(9))"
    )
    assert output.strip() == "True 3.0"


def test_unauthorized_import_is_blocked() -> None:
    # 基本のインポート制限テスト: authorized_imports に無い os を
    # 普通にimport文で書いたら [SandboxViolation] として拒否されること。
    # os はファイルシステム/プロセスへのアクセス手段を提供する典型的な危険モジュール。
    sandbox = _sandbox()
    output = sandbox.run("import os\nprint(os.getcwd())")
    assert "[SandboxViolation]" in output


def test_dynamic_import_bypass_is_blocked() -> None:
    # import文ではなく __import__('os') という「関数呼び出し」経由での
    # バイパスを試すテスト。AST上の import 文だけを静的チェックしていると
    # このパターンをすり抜けてしまうため、executor.py は __import__ 自体を
    # 制限付きの版に差し替えている。その動的パッチが機能していることの確認。
    sandbox = _sandbox()
    output = sandbox.run("m = __import__('os')\nprint(m)")
    assert "[SandboxViolation]" in output


def test_private_module_reference_escape_is_blocked() -> None:
    # 許可されたモジュール(random)自身が、内部で非許可モジュール(os)への
    # 参照を非公開属性(random._os)として持っているケース。
    # 「random は許可されているから中身も無条件に信用してよい」わけではなく、
    # 許可モジュールをラップする _RestrictedModule が、モジュール内部の
    # 非公開属性・非許可の入れ子モジュールへのアクセスも塞いでいることを確認する。
    sandbox = _sandbox(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
    output = sandbox.run("import random\nprint(random._os.listdir('/'))")
    assert "[SandboxViolation]" in output
    assert "_os" in output


def test_public_unauthorized_nested_module_is_blocked() -> None:
    # 上と似ているが、今度は「公開属性」経由での漏洩ケース。
    # typing.sys のように、許可モジュール(typing)が公開属性として
    # 非許可モジュール(sys)への参照を持っている場合も、同じくブロックされる
    # ことを確認する（非公開/公開を問わず、非許可モジュールの露出は防ぐ）。
    sandbox = _sandbox(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
    output = sandbox.run("import typing\nprint(typing.sys.modules)")
    assert "[SandboxViolation]" in output
    assert "unauthorized module 'sys'" in output


def test_operator_attrgetter_private_attribute_bypass_is_blocked() -> None:
    # operator.attrgetter('_os')(random) という、属性名を「文字列として」
    # 渡す関数型の属性アクセス手段を使ったバイパスのテスト。
    # obj.__subclasses__ のような「ドット記法」の静的AST検査だけでは、
    # このように関数経由で文字列から属性アクセスする経路は検出できない。
    sandbox = _sandbox(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
    output = sandbox.run(
        "import operator\nimport random\nprint(operator.attrgetter('_os')(random))"
    )
    assert "[SandboxViolation]" in output
    assert "operator.attrgetter" in output


def test_vars_builtin_and_star_import_are_blocked() -> None:
    # vars() は危険なbuiltin（オブジェクトの__dict__を丸ごと覗ける）として
    # 除去済みなので、呼ぼうとすると単なる NameError（そんな名前は無い）になる
    # ことを確認する。また from random import * のようなワイルドカードimportも
    # [SandboxViolation] として拒否されることを確認する
    # （* は何がimportされるか静的に把握しづらく、ホワイトリスト管理と相性が悪いため）。
    sandbox = _sandbox(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
    assert "NameError" in sandbox.run("print(vars(object))")
    assert "[SandboxViolation]" in sandbox.run("from random import *")


def test_subclasses_escape_via_dot_attribute_is_blocked() -> None:
    """Classic in-process sandbox escape: walk already-loaded classes via
    __subclasses__ to find one whose __init__.__globals__ holds the real,
    unrestricted builtins - completely bypassing the restricted __import__."""
    # 日本語解説: このファイル冒頭で説明した、古典的なサンドボックスエスケープ
    # 手法そのものをテストしている。().__class__.__bases__[0] で tuple の
    # 親クラス(object)を取り、.__subclasses__() でロード済みの全クラスを
    # 列挙し、その中から __init__.__globals__ に本物の制限なしbuiltinsを
    # 持つクラスを探し出して real_import("os") のように呼ぼうとする。
    # これは __import__ の名前ルックアップを一切経由しないため、
    # __import__ パッチだけでは防げない。executor.py の
    # 「ドット記法での __subclasses__ / __globals__ などのダンダー属性
    # アクセスを静的AST検査で拒否する」対策が効いていることを確認する。
    sandbox = _sandbox(authorized_imports=["math"])
    code = textwrap.dedent(
        """
        for cls in ().__class__.__bases__[0].__subclasses__():
            try:
                g = cls.__init__.__globals__
            except Exception:
                continue
            if "__builtins__" in g:
                b = g["__builtins__"]
                real_import = b["__import__"] if isinstance(b, dict) else b.__import__
                real_import("os")
                print("ESCAPED")
                break
        """
    )
    output = sandbox.run(code)
    assert "[SandboxViolation]" in output
    assert "__subclasses__" in output
    # "ESCAPED" が出力に含まれていないことこそが、脱出が実際に阻止された証拠。
    assert "ESCAPED" not in output


def test_format_attribute_escape_is_blocked() -> None:
    # "{0.__class__}".format(1) という、str.format のミニ言語を悪用した
    # 属性アクセスのテスト。"{0.__class__}" のような書式指定文字列は、
    # Pythonの実行時にformat()の内部でドット記法を「文字列から」解釈するため、
    # ソースコード上はASTの Attribute ノードとして現れない
    # （静的チェックをすり抜けてしまう）。そのためexecutor.pyは
    # str.format 自体の呼び出しを禁止属性として明示的にブロックしている。
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run('print("{0.__class__}".format(1))')
    assert "[SandboxViolation]" in output


def test_formatter_field_escape_is_blocked() -> None:
    # string.Formatter().get_field('0.__class__', ...) という、str.format と
    # 同じ弱点を持つ別の経路（Formatterクラスを直接使う場合）も同様に
    # ブロックされることを確認する。str.format 自体を禁止するだけでは
    # 不十分で、同じ仕組みを提供する Formatter クラスの該当メソッドも
    # 塞ぐ必要があることを示すテスト。
    sandbox = _sandbox(authorized_imports=["string"])
    output = sandbox.run(
        "import string\n"
        "formatter = string.Formatter()\n"
        "formatter.get_field('0.__class__', ((),), {})"
    )
    assert "[SandboxViolation]" in output


def test_isolated_worker_cannot_see_host_root_files() -> None:
    # サンドボックス内から os.path.exists('/etc/passwd') を呼んでも False に
    # なる、つまりホストのファイルシステムのルート('/')が丸ごと見えている
    # わけではないことを確認する。これはPythonレベルのopen制限だけでなく、
    # OSレベルの隔離（unshare/bwrapによるマウント名前空間の分離、
    # Section 8.2）が実際に機能していることの傍証にもなっている。
    sandbox = _sandbox(authorized_imports=["os", "posixpath"])
    try:
        output = sandbox.run("import os\nprint(os.path.exists('/etc/passwd'))")
    finally:
        sandbox.close()
    assert output.strip() == "False"


def test_subclasses_escape_via_getattr_is_blocked() -> None:
    # __subclasses__ 脱出手法の変種その1: obj.__subclasses__ という
    # 「ドット記法」ではなく getattr(object, '__subclasses__')() という
    # 「関数呼び出し」経由でアクセスするパターン。ドット記法の静的AST検査
    # だけでは検出できないため、executor.py は getattr 自体も制限版に
    # 差し替えて、同じ禁止ルールをランタイムでも強制している。
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run("getattr(object, '__subclasses__')()")
    assert "[SandboxViolation]" in output


def test_subclasses_escape_via_dynamically_built_name_is_blocked() -> None:
    """getattr's guard must check the resolved name, not the literal source
    text, since the name can be built at runtime to dodge a naive string scan."""
    # 日本語解説: __subclasses__ 脱出手法の変種その2、さらに巧妙なケース。
    # ソースコード中に "__subclasses__" という文字列がそのまま書かれておらず、
    # '__sub' + 'classes__' のように実行時に文字列連結で組み立てている。
    # もし対策が「ソースコードの文字列を単純にスキャンして
    # "__subclasses__" という部分文字列を探す」ような素朴な実装だったら、
    # これは簡単にすり抜けられる。executor.py の getattr パッチは
    # 「実行時に解決された名前そのもの」をチェックしているので、
    # 文字列がどう組み立てられたかに関わらず正しくブロックできることを確認する。
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run("name = '__sub' + 'classes__'\ngetattr(object, name)()")
    assert "[SandboxViolation]" in output


def test_globals_attribute_access_is_blocked() -> None:
    # 関数オブジェクトの __globals__ 属性（その関数が定義されたモジュールの
    # グローバル名前空間の辞書。__builtins__ を含む）へのアクセスも、
    # サンドボックスエスケープの経路になりうるため禁止されていることを確認する。
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run("def f(): pass\nprint(f.__globals__)")
    assert "[SandboxViolation]" in output


def test_setattr_on_dangerous_dunder_is_blocked() -> None:
    # getattr だけでなく setattr 経由での攻撃、たとえば
    # setattr(Foo, '__bases__', (Bar,)) のようにクラスの継承関係そのものを
    # 実行時に書き換えようとする操作もブロックされることを確認する。
    # これもgetattrと同様、executor.pyがsetattrを制限版に差し替えている
    # ことの確認。
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run("class Foo: pass\nclass Bar: pass\nsetattr(Foo, '__bases__', (Bar,))")
    assert "[SandboxViolation]" in output


def test_common_dunders_still_work_for_legitimate_code() -> None:
    """The dunder blocklist must not break ordinary operator overloading,
    iteration, or repr - only the introspection/escape-relevant ones."""
    # 日本語解説: ここまでのテストは「危険な操作がちゃんとブロックされるか」
    # だったが、これは逆に「防御を固めすぎて、正当なコードまで壊していないか」
    # を確認する回帰テスト。__init__、__repr__、__add__、__eq__ のような
    # ごく普通の演算子オーバーロード・データクラス的な使い方は、
    # ダンダー属性の中でも危険度が低いものとしてアローリストに入っており、
    # 問題なく動作し続けることを確認している。
    sandbox = _sandbox(authorized_imports=["math"])
    code = textwrap.dedent(
        """
        class Point:
            def __init__(self, x, y):
                self.x, self.y = x, y
            def __repr__(self):
                return f"Point({self.x}, {self.y})"
            def __add__(self, other):
                return Point(self.x + other.x, self.y + other.y)
            def __eq__(self, other):
                return (self.x, self.y) == (other.x, other.y)

        p = Point(1, 2) + Point(3, 4)
        print(repr(p))
        print(p == Point(4, 6))
        print(len([1, 2, 3]))
        print(getattr(p, "x"))
        """
    )
    output = sandbox.run(code)
    assert "[SandboxViolation]" not in output
    assert "Point(4, 6)" in output
    assert "True" in output


def test_reserved_namespace_names_cannot_override_sandbox_controls() -> None:
    # サンドボックスは final_answer という特別な名前空間を予約している
    # （LLMが解答を提出するための仕組み）。MCPツール等が誤って（あるいは
    # 悪意を持って）"final_answer" という同名のツールを extra_namespace 経由で
    # 注入しようとしても、コンストラクタの時点で ValueError を出して
    # 予約名の上書きを拒否することを確認する。
    config = SandboxConfig(authorized_imports=[], allowed_directories=[])
    with pytest.raises(ValueError, match="final_answer"):
        Sandbox(
            config,
            extra_namespace={"final_answer": lambda answer: answer},
            apply_process_memory_limit=False,
        )


def test_variables_persist_between_calls() -> None:
    # サンドボックスは1タスクにつき1つのワーカーが「常駐」する設計なので、
    # ある呼び出し(run)で定義した変数(x)が、次の呼び出しでも
    # そのまま参照できる(変数の永続性)ことを確認する。これによりLLMは
    # 毎ターン変数を再定義する必要がなく、Thought→Code→Observationの
    # 複数ターンにまたがる状態を持てる。
    sandbox = _sandbox()
    sandbox.run("x = 41")
    output = sandbox.run("print(x + 1)")
    assert output.strip() == "42"


def test_final_answer_raises_and_carries_value() -> None:
    # final_answer(...) はサンドボックス内から呼ばれると、普通の戻り値では
    # なく FinalAnswer という例外として送出される。これはOrchestratorの
    # ループを即座に終わらせるための制御フロー用シグナルであり、
    # 汎用的な except Exception では握りつぶされない特別扱いを受ける
    # （Section 8参照）。ここではその例外が実際に投げられ、
    # 渡した値(answer)がそのまま例外オブジェクトに保持されていることを確認する。
    sandbox = _sandbox()
    try:
        sandbox.run("final_answer('done')")
    except FinalAnswer as fa:
        assert fa.answer == "done"
    else:
        raise AssertionError("expected FinalAnswer to be raised")


def test_syntax_error_is_explicit() -> None:
    # 構文エラーのあるコードを渡した場合、サンドボックスが例外を投げて
    # Orchestratorをクラッシュさせるのではなく、[SyntaxError]という
    # 明示的な文字列として結果を返すことを確認する。これにより
    # LLMは「構文が壊れていた」と分かって書き直せる（Section 8のポリシー:
    # 通常のコードエラーで例外を投げることは絶対にない）。
    sandbox = _sandbox()
    output = sandbox.run("def broken(:\n    pass")
    assert output.startswith("[SyntaxError]")


def test_empty_code_is_explicit() -> None:
    # 空白だけのコード（extract_codeが何もコードを見つけられなかった場合の
    # 空文字列相当）を渡したときも、黙って何もしないのではなく
    # 「[NoCodeBlock] The submitted code was empty.」という明示的な
    # メッセージを返すことを確認する。
    sandbox = _sandbox()
    assert sandbox.run("   ") == "[NoCodeBlock] The submitted code was empty."


def test_filesystem_restriction_blocks_outside_paths(tmp_path: Path) -> None:
    # allowed_directories に含まれないパス（tmp_path、つまりpytestが
    # このテスト用に用意した一時ディレクトリ）へのopen()が
    # [SandboxViolation]として拒否されることを確認する。
    outside_file = str(tmp_path / "secret.txt")
    sandbox = _sandbox(allowed_directories=["/tmp/agent-smith-allowed"])
    output = sandbox.run(f"open({outside_file!r}, 'w')")
    assert "[SandboxViolation]" in output


def test_filesystem_restriction_allows_configured_directory(tmp_path: Path) -> None:
    # 逆に、allowed_directoriesに明示的に含めたディレクトリへのopen()は
    # 正常に成功することを確認する（過剰ブロックしていないかの確認）。
    sandbox = _sandbox(allowed_directories=[str(tmp_path)])
    target = str(tmp_path / "allowed.txt")
    output = sandbox.run(f"open({target!r}, 'w').write('hi')\nprint('ok')")
    assert output.strip() == "ok"


def test_relative_allowed_directory_is_mounted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # allowed_directoriesに相対パスを指定した場合でも、正しくカレント
    # ディレクトリを基準に解決されてマウント/許可されることを確認する
    # （monkeypatch.chdirでカレントディレクトリを変えた上でテストしている）。
    monkeypatch.chdir(tmp_path.parent)
    sandbox = _sandbox(allowed_directories=[tmp_path.name])
    target = str(tmp_path / "relative.txt")
    output = sandbox.run(f"open({target!r}, 'w').write('hi')\nprint('ok')")
    assert output.strip() == "ok"


def test_output_is_truncated() -> None:
    # max_output_charsを非常に小さく(20文字)設定し、それを超える出力を
    # 生成するコードを実行すると、[TruncatedOutput]という印付きで
    # 出力が切り詰められることを確認する。これが無いと、暴走した
    # print文が延々とトークンを消費し続けてしまう。
    config = SandboxConfig(authorized_imports=[], allowed_directories=[], max_output_chars=20)
    sandbox = Sandbox(config, apply_process_memory_limit=False)
    output = sandbox.run("print('x' * 100)")
    assert "[TruncatedOutput]" in output


def test_timeout_interrupts_infinite_loop() -> None:
    # while True: pass という無限ループを、1秒のタイムアウト設定で
    # 実行しても、サンドボックスがハングせず[Timeout]で正しく打ち切ることを
    # 確認する。これは signal.alarm() + SIGALRM ハンドラによる
    # 壁時計タイムアウトの仕組みが機能していることの確認。
    config = SandboxConfig(
        authorized_imports=[], allowed_directories=[], max_execution_time_seconds=1
    )
    sandbox = Sandbox(config, apply_process_memory_limit=False)
    output = sandbox.run("while True:\n    pass")
    assert output.startswith("[Timeout]")


def test_memory_limit_is_enforced() -> None:
    """Runs in a subprocess so lowering RLIMIT_AS cannot affect the test runner."""
    # 日本語解説: メモリ上限(RLIMIT_AS)のテストだけは、あえて独立した
    # サブプロセス(subprocess.run)の中で行っている。理由はコメント通りで、
    # resource.setrlimit(RLIMIT_AS, ...) はプロセス全体に対してかかる制限
    # なので、もしpytestを実行している「このプロセス自身」に適用してしまうと、
    # pytestプロセス自体や、その後に実行される他のテストにまで
    # メモリ上限がかかってしまい、テストスイート全体が不安定になる。
    # そのため、python -c "..." で完全に別プロセスを起動し、そちらの中で
    # Sandboxを作ってメモリ上限を適用し、500MBのbytearrayという
    # 明らかに上限(32MB)を超えるメモリ確保を試みさせている。
    script = textwrap.dedent(
        """
        from models import SandboxConfig
        from sandbox.executor import Sandbox

        config = SandboxConfig(authorized_imports=[], allowed_directories=[], max_memory_mb=32)
        sandbox = Sandbox(config)
        print(sandbox.run("data = bytearray(500 * 1024 * 1024)"))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )
    # [MemoryLimitExceeded]という文字列が出力に含まれていれば、
    # OSのOOM killerに強制終了させられるのではなく、Pythonの
    # MemoryErrorとしてきちんと捕捉・変換できていることの証明になる。
    assert "[MemoryLimitExceeded]" in result.stdout
