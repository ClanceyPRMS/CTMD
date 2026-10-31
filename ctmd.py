#!/usr/bin/env python
"""CTMD · Creative Trinity Minecraft Downloader

用法：
    python ctmd.py                      打开图形界面
    python ctmd.py list --type release  列版本
    python ctmd.py install 26.3 --name 生存服
    python ctmd.py instances            看已装实例
    python ctmd.py verify 生存服         校验文件
    python ctmd.py doctor               体检
"""

import sys
from pathlib import Path

# PyInstaller 打包后 __file__ 指向临时解压目录（_MEIPASS），
# 用它当根目录会导致 config.json / store / instances 写到临时区，重启就丢。
# 打包后用 exe 自己所在的目录。
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from ctmd.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(ROOT))
