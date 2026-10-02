"""让测试能 import 到 deployer 下的 store / cli。

cli.py 里是 `import store`，所以得把 deployer 目录本身加进 sys.path。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "deployer"))
