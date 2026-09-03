"""pytestがどこから実行されても、プロジェクトルートをimport可能にするための設定ファイル。"""
import sys  # コマンドライン引数やパス操作のために使用
from pathlib import Path  # ファイルパスをオブジェクトとして扱うために使用

# このconftest.pyがあるディレクトリ(プロジェクトルート)をsys.pathの先頭に追加し、
# テストファイルからプロジェクト直下のモジュールをimportできるようにする
sys.path.insert(0, str(Path(__file__).resolve().parent))
