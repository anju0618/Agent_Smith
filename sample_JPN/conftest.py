"""Ensures the project root is importable regardless of how pytest is invoked."""
# ここは pytest 用のブートストラップファイル。中身はたった1行の実処理だが役割は重要。
#
# 背景: このプロジェクトには setup.py や pip install -e のようなパッケージインストール
# 手順が無く、`models.py`・`orchestrator.py`・`sandbox/` などをただの相対パスの
# モジュールとして import している（例: `from models import SolutionOutput`）。
# これは「リポジトリのルート（sample/ ディレクトリ）が sys.path に入っている」時にしか
# 動かない。ふだん `uv run python -m agent_mbpp ...` のように実行する分にはカレント
# ディレクトリが sample/ になるので問題ないが、pytest はテストファイルのある場所や
# 呼び出し元のカレントディレクトリによって sys.path の組み立て方が変わることがある。
#
# pytest は「conftest.py という名前のファイルを見つけたら、テスト収集の前に必ず
# 読み込む」という特別な規約を持っている。そこでこのファイルの存在自体が
# 「プロジェクトルートを import 可能にする」というブートストラップの実行場所になる。
import sys
from pathlib import Path

# __file__ はこの conftest.py 自身の場所。.resolve() でシンボリックリンクなどを解決した
# 絶対パスにし、.parent でその1つ上のディレクトリ（= sample/ そのもの）を取る。
# それを sys.path の先頭（index 0）に差し込むことで、
# 「pytest がどこから呼ばれても、tests/ の中のテストファイルから
#  `from models import ...` や `from sandbox.executor import ...` が必ず解決できる」
# ようになる。先頭に挿入するのは、万が一同名のパッケージが他の場所にもあった場合に、
# こちらのプロジェクトルートを優先させるため。
sys.path.insert(0, str(Path(__file__).resolve().parent))
