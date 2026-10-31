"""导入对话框的界面冒烟测试：进程内打开对话框，不做任何真实导入。

用法（先关掉正在运行的 CTMD）：
    python tests/gui_import_smoke.py [程序根目录]

Tkinter 不吃后台合成鼠标事件，所以直接构造 _ImportDialog 而不是模拟点击。
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctmd.gui as gui                      # noqa: E402
from ctmd import instance as inst_mod       # noqa: E402
from ctmd.config import Config              # noqa: E402

LOG = ROOT / "tests" / "gui_import_smoke.log"


def note(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


gui.messagebox.askyesno = lambda *a, **k: False
gui.messagebox.showinfo = lambda *a, **k: None
gui.messagebox.showwarning = lambda *a, **k: None
gui.messagebox.showerror = lambda *a, **k: note(f"ERRORBOX: {a}")

root_arg = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
PCL_MC = Path(r"E:\Laboratory\PCL\.minecraft")

cfg = Config(root_arg)
cfg.ensure_dirs()
gui.enable_dpi()
app = gui.App(cfg)

state = {"dialog": None}


def show_dialog() -> None:
    app.nb.select(app.tab_inst)
    app._reload_instances()
    note("instances tab selected")
    note(f"btn_import text = {app.btn_import.cget('text')!r}")
    if not PCL_MC.is_dir():
        note(f"skip: {PCL_MC} 不存在")
        return
    vids = inst_mod.list_versions(PCL_MC)
    note(f"detected {len(vids)} versions")
    dlg = gui._ImportDialog(app, PCL_MC, vids,
                            lambda mc, v, m, s: note(f"start clicked: {m}/{s}"))
    state["dialog"] = dlg
    note("dialog shown")


def dump_selection() -> None:
    dlg = state.get("dialog")
    if dlg is not None and dlg.winfo_exists():
        note(f"default mode={dlg.mode.get()} share={dlg.share.get()}")


app.root.after(2000, show_dialog)
app.root.after(25000, dump_selection)
app.root.after(40000, app.root.destroy)
app.run()
note("exited")
