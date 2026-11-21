#!/usr/bin/env python3
"""命令行薄封装：隔离模式下先登记仓库根路径，再转交 basket.cli。

示例：
  python3 -I run_basket.py briefing --today 2026-09-02
  python3 -I run_basket.py plan --week 2026-08-31 --household 单次取货 --district 上街
  python3 -I run_basket.py triage
  python3 -I run_basket.py corrections
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from basket.cli import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
