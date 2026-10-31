"""界面冒烟测试：在进程内驱动 CTMD 窗口，配合截图做视觉验收。

用法（先关掉正在运行的 CTMD）：
    python tests/gui_smoke.py instances        # 打开「已装实例」页
    python tests/gui_smoke.py install 1.8.9    # 自动开始安装某个版本

说明：Tkinter 不吃后台合成鼠标事件，所以直接调 App 的方法而不是模拟点击。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctmd.gui as gui          # noqa: E402
from ctmd.config import Config  # noqa: E402

LOG = ROOT / "tests" / "gui_smoke.log"


def note(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


# 测试时不要弹模态框，否则会卡住
gui.messagebox.askyesno = lambda *a, **k: False
gui.messagebox.showinfo = lambda *a, **k: None
gui.messagebox.showwarning = lambda *a, **k: None
gui.messagebox.showerror = lambda *a, **k: print("ERRORBOX:", a)

cfg = Config(ROOT)
cfg.ensure_dirs()
gui.enable_dpi()
app = gui.App(cfg)
mode = sys.argv[1] if len(sys.argv) > 1 else "instances"


def show_instances() -> None:
    app.nb.select(app.tab_inst)
    app._reload_instances()
    note("switched to instances tab")

    def pick_first() -> None:
        rows = app.itree.get_children()
        if rows:
            app.itree.selection_set(rows[0])
            app._inst_selected_detail()

    app.root.after(2000, pick_first)


def start_install(version: str) -> None:
    app.nb.select(app.tab_install)
    try:
        app.tree.selection_set(version)
        app.tree.see(version)
    except Exception as e:  # noqa: BLE001
        note(f"select failed: {e}")
    app._on_select()
    app.root.after(700, app._start_install)
    note(f"started install: {version}")


if mode == "instances":
    app.root.after(2500, show_instances)
elif mode == "install":
    app.root.after(2500, lambda: start_install(sys.argv[2]))
elif mode == "loader":
    # 打开「模组加载器」页：选第一个实例，列版本，可选直接装
    def show_loader() -> None:
        app.nb.select(app.tab_loader)
        app._reload_instances()
        app.root.after(1500, app._li_on_change)
        note("switched to loader tab")
        if len(sys.argv) > 2:
            kind = sys.argv[2].lower()
            app.li_kind_var.set("Forge" if kind == "forge" else "Fabric")
            app.root.after(3500, app._li_load_versions)
            app.root.after(6500, app._li_install)
            note(f"triggered loader install: {kind}")
    app.root.after(2500, show_loader)

app.run()
