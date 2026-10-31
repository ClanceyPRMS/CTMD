"""打包 CTMD 成单个 exe。

用法：
    python build.py            # 默认：单文件 + 无控制台窗口（双击即用）
    python build.py --console  # 带控制台，命令行输出可见（排错用）

产物：dist\\CTMD.exe

注意：
- config.json / store / instances 会创建在 **exe 所在目录**（见 ctmd.py 的 frozen 判断），
  所以别把 exe 塞进 Program Files（没写权限），放 D:\\ctmd 这类目录即可。
- PyInstaller 的 build/ 中间产物和 dist/ 都在项目里，不想要可以删。
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "ctmd.py"
ICON = ROOT / "assets" / "ctmd.ico"


def main() -> int:
    if not ENTRY.exists():
        print(f"找不到入口文件：{ENTRY}", file=sys.stderr)
        return 1

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile",
        "--name", "CTMD",
        # tkinter 的界面模块在函数里延迟导入，静态分析可能漏，显式声明
        "--hidden-import", "ctmd.gui",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build"),
        "--specpath", str(ROOT / "build"),
    ]
    if ICON.exists():
        cmd += ["--icon", str(ICON)]
        # tkinter 的窗口图标不走 exe 资源，得把 .ico 一起打进去，运行时手动设置
        cmd += ["--add-data", f"{ICON};assets"]
    if "--console" not in sys.argv:
        cmd.append("--windowed")
    cmd.append(str(ENTRY))

    print("执行：", " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode != 0:
        print("打包失败。", file=sys.stderr)
        return r.returncode

    exe = ROOT / "dist" / "CTMD.exe"
    if exe.exists():
        size = exe.stat().st_size / 1024 / 1024
        print(f"\n✓ 打包完成：{exe}  ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
