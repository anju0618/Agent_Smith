"""sandbox/executor.py(セクション4.2のセキュリティ制約)のテスト。

メモリ制限の強制は、分離されたワーカープロセス経由でテストする
(test_memory_limit_is_enforcedを参照)。これにより、RLIMIT_ASを下げても
pytestプロセス自体やセッション内の他のテストに影響しないようにしている。
"""
import subprocess  # メモリ制限テストをサブプロセスとして分離実行するために使用
import sys  # サブプロセスでの実行に使うPythonインタプリタのパス取得に使用
import textwrap  # 複数行コード文字列のインデント調整に使用
from pathlib import Path  # ファイルパス操作に使用
from typing import List, Optional  # 型ヒントに使用

import pytest  # pytest.raisesに使用

from models import SandboxConfig  # サンドボックスの設定モデル
from sandbox.executor import DEFAULT_AUTHORIZED_IMPORTS, FinalAnswer, Sandbox  # テスト対象のサンドボックス実装


def _sandbox(
    allowed_directories: Optional[List[str]] = None, authorized_imports: Optional[List[str]] = None
) -> Sandbox:
    # テスト用にSandboxインスタンスを組み立てるヘルパー関数
    imports = authorized_imports if authorized_imports is not None else DEFAULT_AUTHORIZED_IMPORTS  # 未指定ならデフォルトの許可import一覧を使う
    directories = allowed_directories if allowed_directories is not None else []  # 未指定なら許可ディレクトリなし
    config = SandboxConfig(authorized_imports=imports, allowed_directories=directories)
    return Sandbox(config, apply_process_memory_limit=False)  # テスト中はプロセス全体のメモリ制限を無効化


def test_authorized_import_is_allowed() -> None:
    # 許可されたモジュール(math)のimportと使用が正常に動作することを検証
    sandbox = _sandbox()
    output = sandbox.run("import math\nprint(math.sqrt(4))")
    assert output.strip() == "2.0"  # sqrt(4)の計算結果が正しく出力されること


def test_authorized_nested_and_from_imports_are_allowed() -> None:
    # ネストしたモジュール(collections.abc)やfrom-import形式のimportも許可されることを検証
    sandbox = _sandbox()
    output = sandbox.run(
        "import collections.abc\nfrom math import sqrt\n"
        "print(isinstance([], collections.abc.Iterable), sqrt(9))"
    )
    assert output.strip() == "True 3.0"  # 両方のimportが正しく機能していること


def test_unauthorized_import_is_blocked() -> None:
    # 許可リストにないモジュール(os)のimportがブロックされることを検証
    sandbox = _sandbox()
    output = sandbox.run("import os\nprint(os.getcwd())")
    assert "[SandboxViolation]" in output  # サンドボックス違反として検出されること


def test_dynamic_import_bypass_is_blocked() -> None:
    # __import__()による動的なimportの迂回もブロックされることを検証
    sandbox = _sandbox()
    output = sandbox.run("m = __import__('os')\nprint(m)")
    assert "[SandboxViolation]" in output  # サンドボックス違反として検出されること


def test_private_module_reference_escape_is_blocked() -> None:
    # 許可されたモジュール経由でプライベート属性(random._os)にアクセスして
    # 未許可モジュールへ脱出しようとする手口がブロックされることを検証
    sandbox = _sandbox(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
    output = sandbox.run("import random\nprint(random._os.listdir('/'))")
    assert "[SandboxViolation]" in output  # サンドボックス違反として検出されること
    assert "_os" in output  # 違反した属性名がメッセージに含まれること


def test_public_unauthorized_nested_module_is_blocked() -> None:
    # 許可されたモジュール(typing)から未許可のネストモジュール(sys)への
    # 参照(typing.sys)がブロックされることを検証
    sandbox = _sandbox(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
    output = sandbox.run("import typing\nprint(typing.sys.modules)")
    assert "[SandboxViolation]" in output  # サンドボックス違反として検出されること
    assert "unauthorized module 'sys'" in output  # 未許可モジュールsysである旨のメッセージが含まれること


def test_operator_attrgetter_private_attribute_bypass_is_blocked() -> None:
    # operator.attrgetterを使ったプライベート属性アクセスによる迂回もブロックされることを検証
    sandbox = _sandbox(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
    output = sandbox.run(
        "import operator\nimport random\nprint(operator.attrgetter('_os')(random))"
    )
    assert "[SandboxViolation]" in output  # サンドボックス違反として検出されること
    assert "operator.attrgetter" in output  # attrgetter経由の違反である旨が記録されること


def test_vars_builtin_and_star_import_are_blocked() -> None:
    # 組み込みvars()の使用と、from module import *形式のimportがそれぞれ
    # 適切にブロック/エラーになることを検証
    sandbox = _sandbox(authorized_imports=DEFAULT_AUTHORIZED_IMPORTS)
    assert "NameError" in sandbox.run("print(vars(object))")  # vars()は名前空間にないためNameError
    assert "[SandboxViolation]" in sandbox.run("from random import *")  # star importはサンドボックス違反


def test_subclasses_escape_via_dot_attribute_is_blocked() -> None:
    """典型的なインプロセスサンドボックス脱出手口: __subclasses__を使って
    既にロード済みのクラスをたどり、その__init__.__globals__に本物の
    制限されていないbuiltinsを持つクラスを見つけ出す - これは制限された
    __import__を完全に迂回する攻撃である。"""
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
    assert "[SandboxViolation]" in output  # サンドボックス違反として検出されること
    assert "__subclasses__" in output  # __subclasses__経由の違反である旨が記録されること
    assert "ESCAPED" not in output  # 実際には脱出に成功していないこと


def test_format_attribute_escape_is_blocked() -> None:
    # str.format()の"{0.__class__}"構文を使った属性アクセス経由の脱出がブロックされることを検証
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run('print("{0.__class__}".format(1))')
    assert "[SandboxViolation]" in output  # サンドボックス違反として検出されること


def test_formatter_field_escape_is_blocked() -> None:
    # string.Formatter().get_field()を使った同様の属性アクセス経由の脱出がブロックされることを検証
    sandbox = _sandbox(authorized_imports=["string"])
    output = sandbox.run(
        "import string\n"
        "formatter = string.Formatter()\n"
        "formatter.get_field('0.__class__', ((),), {})"
    )
    assert "[SandboxViolation]" in output  # サンドボックス違反として検出されること


def test_isolated_worker_cannot_see_host_root_files() -> None:
    # 分離されたワーカープロセスからは、ホスト環境のルートファイル(/etc/passwd)が
    # 見えないことを検証(osとposixpathのimportは許可した上で確認)
    sandbox = _sandbox(authorized_imports=["os", "posixpath"])
    try:
        output = sandbox.run("import os\nprint(os.path.exists('/etc/passwd'))")
    finally:
        sandbox.close()  # ワーカープロセスを確実に終了させる
    assert output.strip() == "False"  # ホストのファイルは見えず存在しないと判定されること


def test_subclasses_escape_via_getattr_is_blocked() -> None:
    # getattr()経由で__subclasses__属性を取得する脱出手口もブロックされることを検証
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run("getattr(object, '__subclasses__')()")
    assert "[SandboxViolation]" in output  # サンドボックス違反として検出されること


def test_subclasses_escape_via_dynamically_built_name_is_blocked() -> None:
    """getattrのガードは、ソースコード上のリテラル文字列ではなく、
    実際に解決された名前をチェックしなければならない。名前は実行時に
    組み立てることができ、単純な文字列スキャンでは回避されてしまうため。"""
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run("name = '__sub' + 'classes__'\ngetattr(object, name)()")
    assert "[SandboxViolation]" in output  # 動的に組み立てた名前でもブロックされること


def test_globals_attribute_access_is_blocked() -> None:
    # 関数オブジェクトの__globals__属性へのアクセスがブロックされることを検証
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run("def f(): pass\nprint(f.__globals__)")
    assert "[SandboxViolation]" in output  # サンドボックス違反として検出されること


def test_setattr_on_dangerous_dunder_is_blocked() -> None:
    # setattr()を使って__bases__のような危険なdunder属性を書き換える手口がブロックされることを検証
    sandbox = _sandbox(authorized_imports=["math"])
    output = sandbox.run("class Foo: pass\nclass Bar: pass\nsetattr(Foo, '__bases__', (Bar,))")
    assert "[SandboxViolation]" in output  # サンドボックス違反として検出されること


def test_common_dunders_still_work_for_legitimate_code() -> None:
    """dunderのブロックリストは、通常の演算子オーバーロード・イテレーション・
    reprなどの正当な用途を壊してはならず、あくまで内省/脱出に関連するものだけを
    ブロックすべきである。"""
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
    assert "[SandboxViolation]" not in output  # 正当なdunder使用は違反として扱われないこと
    assert "Point(4, 6)" in output  # __add__/__repr__が正しく機能していること
    assert "True" in output  # __eq__が正しく機能していること


def test_reserved_namespace_names_cannot_override_sandbox_controls() -> None:
    # extra_namespaceでfinal_answerのような予約された制御用名を上書きしようとすると
    # Sandboxのコンストラクタでエラーになることを検証
    config = SandboxConfig(authorized_imports=[], allowed_directories=[])
    with pytest.raises(ValueError, match="final_answer"):
        Sandbox(
            config,
            extra_namespace={"final_answer": lambda answer: answer},  # 予約名を上書きしようとする不正な設定
            apply_process_memory_limit=False,
        )


def test_variables_persist_between_calls() -> None:
    # 同一Sandboxインスタンスに対する複数回のrun()呼び出しの間で、
    # 変数の状態が保持され続けることを検証
    sandbox = _sandbox()
    sandbox.run("x = 41")
    output = sandbox.run("print(x + 1)")
    assert output.strip() == "42"  # 前回設定したxの値が引き継がれていること


def test_final_answer_raises_and_carries_value() -> None:
    # final_answer()の呼び出しがFinalAnswer例外として送出され、
    # その値(answer属性)が正しく保持されることを検証
    sandbox = _sandbox()
    try:
        sandbox.run("final_answer('done')")
    except FinalAnswer as fa:
        assert fa.answer == "done"  # 渡した値がanswer属性として取得できること
    else:
        raise AssertionError("expected FinalAnswer to be raised")  # 例外が発生しなかった場合はテスト失敗


def test_syntax_error_is_explicit() -> None:
    # 構文エラーのあるコードを実行すると、明示的な[SyntaxError]メッセージが返ることを検証
    sandbox = _sandbox()
    output = sandbox.run("def broken(:\n    pass")
    assert output.startswith("[SyntaxError]")  # 構文エラーである旨のメッセージで始まること


def test_empty_code_is_explicit() -> None:
    # 空白のみのコードを実行すると、明示的な[NoCodeBlock]メッセージが返ることを検証
    sandbox = _sandbox()
    assert sandbox.run("   ") == "[NoCodeBlock] The submitted code was empty."


def test_filesystem_restriction_blocks_outside_paths(tmp_path: Path) -> None:
    # 許可されたディレクトリ以外へのファイル書き込みがブロックされることを検証
    outside_file = str(tmp_path / "secret.txt")
    sandbox = _sandbox(allowed_directories=["/tmp/agent-smith-allowed"])  # 許可されているのは別のディレクトリのみ
    output = sandbox.run(f"open({outside_file!r}, 'w')")
    assert "[SandboxViolation]" in output  # サンドボックス違反として検出されること


def test_filesystem_restriction_allows_configured_directory(tmp_path: Path) -> None:
    # 明示的に許可したディレクトリへの書き込みは正常に行えることを検証
    sandbox = _sandbox(allowed_directories=[str(tmp_path)])
    target = str(tmp_path / "allowed.txt")
    output = sandbox.run(f"open({target!r}, 'w').write('hi')\nprint('ok')")
    assert output.strip() == "ok"  # 書き込みが成功し、後続のprintも実行されること


def test_relative_allowed_directory_is_mounted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # allowed_directoriesに相対パスを指定した場合でも、
    # カレントディレクトリを基準に正しくマウント(許可)されることを検証
    monkeypatch.chdir(tmp_path.parent)  # 作業ディレクトリをtmp_pathの親に変更
    sandbox = _sandbox(allowed_directories=[tmp_path.name])  # 相対パスで許可ディレクトリを指定
    target = str(tmp_path / "relative.txt")
    output = sandbox.run(f"open({target!r}, 'w').write('hi')\nprint('ok')")
    assert output.strip() == "ok"  # 相対パス指定でも書き込みが成功すること


def test_output_is_truncated() -> None:
    # 出力文字数の上限を超えた場合、[TruncatedOutput]の注記付きで切り詰められることを検証
    config = SandboxConfig(authorized_imports=[], allowed_directories=[], max_output_chars=20)  # 出力上限を小さく設定
    sandbox = Sandbox(config, apply_process_memory_limit=False)
    output = sandbox.run("print('x' * 100)")
    assert "[TruncatedOutput]" in output  # 切り詰められたことを示す注記が含まれること


def test_timeout_interrupts_infinite_loop() -> None:
    # 実行時間の上限を超える無限ループが、タイムアウトによって強制的に打ち切られることを検証
    config = SandboxConfig(
        authorized_imports=[], allowed_directories=[], max_execution_time_seconds=1  # 実行時間上限を1秒に設定
    )
    sandbox = Sandbox(config, apply_process_memory_limit=False)
    output = sandbox.run("while True:\n    pass")
    assert output.startswith("[Timeout]")  # タイムアウトである旨のメッセージで始まること


def test_memory_limit_is_enforced() -> None:
    """pytest実行プロセス自体に影響しないよう、サブプロセスの中で実行する。"""
    # サブプロセス内で実行させるスクリプト本体: メモリ上限32MBに対して500MBの
    # bytearrayを確保しようとし、メモリ制限違反が検出されるかを確認する
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
    assert "[MemoryLimitExceeded]" in result.stdout  # サブプロセスの標準出力にメモリ上限超過の注記があること
