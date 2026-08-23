"""测试共享配置。

让 tests/ 目录可被导入（共享 helper `_fakes`），
使 unit/ 与 integration/ 子目录下的测试都能 `from _fakes import ...`。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
