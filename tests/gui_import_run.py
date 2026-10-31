"""GUI 导入全链路冒烟：走 _run_import → 后台线程 → 队列 → 刷新列表。

用法：python tests/gui_import_run.py <程序根目录> [版本名]
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ctmd.gui as gui      # noqa: E402
from ctmd.config import Config  # noqa: E402

LOG = ROOT / "tests" / "gui_import_run.log"
PCL_MC = Path(r"E:\Laboratory\PCL\.minecraft")


def note(msg: str) -> None:
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


gui.messagebox.showinfo = lambda t, m, **k: note(f"INFO[{t}]: {m.splitlines()[0]}")
gui.messagebox.showwarning = lambda t, m, **k: note(f"WARN[{t}]: {m.splitlines()[0]}")
gui.messagebox.showerror = lambda t, m, **k: note(f"ERR[{t}]: {m.splitlines()[0]}")
gui.messagebox.askyesno = lambda *a, **k: False

# 探针：队列泵一旦抛异常就会停摆（末尾的 after 不再调度），这里抓出来
_orig_pump = gui.App._pump


def _pump_probe(self):
    try:
        _orig_pump(self)
    except Exception as e:  # noqa: BLE001
        note(f"PUMP ERROR: {type(e).__name__}: {e}")
        raise


gui.App._pump = _pump_probe

root = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT
vid = sys.argv[2] if len(sys.argv) > 2 else "1.1"

cfg = Config(root)
cfg.ensure_dirs()
gui.enable_dpi()
app = gui.App(cfg)


def kick() -> None:
    app.nb.select(app.tab_inst)
    note(f"start import {vid} -> {root}")
    app._run_import(PCL_MC, [vid], "copy", "local")


def report() -> None:
    rows = app.itree.get_children()
    note(f"tree rows after import: {len(rows)} -> {list(rows)}")
    note(f"btn_import state: {app.btn_import.cget('state')}")
    log_text = app.inst_log.get("1.0", "end").strip()
    note("inst_log tail:\n" + "\n".join(log_text.splitlines()[-8:]))


app.root.after(1500, kick)
app.root.after(15000, report)
app.root.after(18000, app.root.destroy)
app.run()
note("exited")
