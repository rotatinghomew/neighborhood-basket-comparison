"""测试包引导：隔离模式（python3 -I）下把仓库根加入 sys.path。"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
