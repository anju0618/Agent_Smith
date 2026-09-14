"""pytestがどこから実行されても、プロジェクトルートをimport可能にするための設定ファイル。"""
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
