"""双击运行 CTMD 图形界面（.pyw 不会弹出黑色控制台窗口）。"""

import sys
from pathlib import Path

ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ctmd.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.argv = [sys.argv[0], "gui"]
    sys.exit(main(ROOT))
