"""CTMD 图形界面（tkinter，无第三方依赖）。"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

from . import __version__, instance as inst_mod, java, service
from . import net as net_mod
from .config import Config
from .downloader import Progress
from .fsutil import human_size
from .manifest import version_type_label
from .sources import SOURCE_CHOICES

FONT = "Microsoft YaHei UI"
BG = "#f6f7f9"
CARD = "#ffffff"
ACCENT = "#2f6feb"
TEXT = "#1f2328"
MUTED = "#6b7280"

TYPE_TAGS = {
    "release": ("#0a7f3f", "正式版"),
    "snapshot": ("#b06a00", "快照"),
    "old_beta": ("#7a5bb5", "远古 Beta"),
    "old_alpha": ("#7a5bb5", "远古 Alpha"),
}


def _fmt_eta(sec: float) -> str:
    if not sec or sec <= 0:
        return "--:--"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def enable_dpi() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def asset_path(name: str) -> "Path | None":
    """找一个随程序分发的资源文件。

    源码运行：项目根下的 assets/；
    PyInstaller 打包后：解压目录 _MEIPASS/assets/（由 build.py 的 --add-data 带入）。
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).resolve().parent.parent
    p = base / "assets" / name
    return p if p.exists() else None


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.q: "queue.Queue" = queue.Queue()
        self.versions: list = []
        self.filtered: list = []
        self.installer = None
        self.worker = None
        self.worker_cancel = None
        self.last_install_dir: Path | None = None
        self.detail_cache: dict = {}
        self._detail_seq = 0

        self.root = tk.Tk()
        self.root.title(f"CTMD · Creative Trinity Minecraft Downloader  v{__version__}")
        self.root.geometry("1180x880")
        self.root.minsize(1000, 720)
        self.root.configure(bg=BG)
        ico = asset_path("ctmd.ico")
        if ico:
            try:
                self.root.iconbitmap(default=str(ico))
            except Exception:
                pass
        self._style()
        self._build()
        self.root.after(80, self._pump)
        self.root.after(120, self._boot)

    # ------------------------------------------------------------ 样式
    def _style(self) -> None:
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", font=(FONT, 10), background=BG, foreground=TEXT)
        st.configure("TFrame", background=BG)
        st.configure("Card.TFrame", background=CARD, relief="flat")
        st.configure("TLabel", background=BG, foreground=TEXT)
        st.configure("Card.TLabel", background=CARD, foreground=TEXT)
        st.configure("Muted.TLabel", background=BG, foreground=MUTED)
        st.configure("CardMuted.TLabel", background=CARD, foreground=MUTED)
        st.configure("Title.TLabel", background=BG, font=(FONT, 15, "bold"), foreground=TEXT)
        st.configure("Sub.TLabel", background=BG, font=(FONT, 9), foreground=MUTED)
        st.configure("Section.TLabel", background=CARD, font=(FONT, 11, "bold"), foreground=TEXT)
        st.configure("TCheckbutton", background=CARD, foreground=TEXT)
        st.configure("TNotebook", background=BG, borderwidth=0)
        st.configure("TNotebook.Tab", font=(FONT, 10), padding=(18, 8))
        st.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", ACCENT)])
        st.configure("TButton", font=(FONT, 10), padding=(12, 6))
        st.configure("Accent.TButton", font=(FONT, 11, "bold"), padding=(14, 10),
                     background=ACCENT, foreground="white", borderwidth=0)
        st.map("Accent.TButton", background=[("active", "#2459c9"), ("disabled", "#9db8f5")])
        st.configure("TEntry", padding=4)
        st.configure("TCombobox", padding=4)
        st.configure("Treeview", font=(FONT, 10), rowheight=27, background=CARD,
                     fieldbackground=CARD, borderwidth=0)
        st.configure("Treeview.Heading", font=(FONT, 10, "bold"), background="#eceff3",
                     foreground=TEXT, relief="flat", padding=(6, 6))
        st.map("Treeview", background=[("selected", "#dbe7ff")], foreground=[("selected", TEXT)])
        st.configure("TLabelframe", background=CARD, borderwidth=1, relief="solid")
        st.configure("TLabelframe.Label", background=CARD, foreground=TEXT, font=(FONT, 10, "bold"))
        st.configure("Horizontal.TProgressbar", thickness=14, background=ACCENT,
                     troughcolor="#e6e9ef", borderwidth=0)

    # ------------------------------------------------------------ 界面
    def _build(self) -> None:
        # 顶部
        head = ttk.Frame(self.root, padding=(18, 10, 18, 6))
        head.pack(fill="x")
        left = ttk.Frame(head)
        left.pack(side="left")
        ttk.Label(left, text="CTMD", style="Title.TLabel").pack(side="left")
        ttk.Label(left, text="  Creative Trinity Minecraft Downloader",
                  style="Sub.TLabel").pack(side="left", pady=(6, 0))
        right = ttk.Frame(head)
        right.pack(side="right")
        ttk.Label(right, text="下载源", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.source_var = tk.StringVar(value=self._source_label())
        self.source_box = ttk.Combobox(right, textvariable=self.source_var, width=22,
                                       state="readonly", values=[c[1] for c in SOURCE_CHOICES])
        self.source_box.pack(side="left")
        self.source_box.bind("<<ComboboxSelected>>", self._on_source_change)
        self.btn_speed = ttk.Button(right, text="测速", command=self._measure)
        self.btn_speed.pack(side="left", padx=6)
        self.btn_refresh = ttk.Button(right, text="刷新清单", command=lambda: self._load_versions(True))
        self.btn_refresh.pack(side="left")

        # 选项卡
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=18, pady=(4, 8))
        self.tab_install = ttk.Frame(self.nb)
        self.tab_inst = ttk.Frame(self.nb)
        self.tab_loader = ttk.Frame(self.nb)
        self.nb.add(self.tab_install, text="安装新版本")
        self.nb.add(self.tab_inst, text="已装实例")
        self.nb.add(self.tab_loader, text="模组加载器")
        self._build_install_tab()
        self._build_instances_tab()
        self._build_loader_tab()

        # 状态栏
        bar = ttk.Frame(self.root, padding=(18, 4, 18, 8))
        bar.pack(fill="x")
        self.status = ttk.Label(bar, text=f"就绪 · 共享仓库 {self.cfg.store_dir}", style="Muted.TLabel")
        self.status.pack(side="left")

    def _build_install_tab(self) -> None:
        body = ttk.Frame(self.tab_install)
        body.pack(fill="both", expand=True, pady=(10, 0))

        # 左：版本表
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)

        bar = ttk.Frame(left)
        bar.pack(fill="x", pady=(0, 6))
        self.search_var = tk.StringVar()
        ent = ttk.Entry(bar, textvariable=self.search_var, width=26)
        ent.pack(side="left")
        ent.insert(0, "")
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        ttk.Label(bar, text="搜索版本号", style="Muted.TLabel").pack(side="left", padx=6)
        self.type_var = tk.StringVar(value="正式版")
        cb = ttk.Combobox(bar, textvariable=self.type_var, width=10, state="readonly",
                          values=["正式版", "快照", "远古", "全部"])
        cb.pack(side="left", padx=(10, 0))
        cb.bind("<<ComboboxSelected>>", lambda e: self._apply_filter())
        self.count_label = ttk.Label(bar, text="", style="Muted.TLabel")
        self.count_label.pack(side="right")

        wrap = ttk.Frame(left)
        wrap.pack(fill="both", expand=True)
        cols = ("id", "type", "date")
        self.tree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        self.tree.heading("id", text="版本")
        self.tree.heading("type", text="类型")
        self.tree.heading("date", text="发布日期")
        self.tree.column("id", width=180, anchor="w")
        self.tree.column("type", width=90, anchor="w")
        self.tree.column("date", width=110, anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for t, (color, _label) in TYPE_TAGS.items():
            self.tree.tag_configure(t, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda e: self._start_install())

        # 右：配置卡
        right = ttk.Frame(body, style="Card.TFrame", padding=(14, 12), width=352)
        right.pack(side="right", fill="y", padx=(14, 0))
        right.pack_propagate(False)

        ttk.Label(right, text="安装设置", style="Section.TLabel").pack(anchor="w")
        self.detail = ttk.Label(right, text="在左侧选一个版本", style="CardMuted.TLabel",
                                justify="left", wraplength=320)
        self.detail.pack(anchor="w", pady=(2, 6))

        name_row = ttk.Frame(right, style="Card.TFrame")
        name_row.pack(fill="x", pady=(0, 4))
        ttk.Label(name_row, text="实例名", style="CardMuted.TLabel", width=6).pack(side="left")
        self.name_var = tk.StringVar()
        self.name_entry = ttk.Entry(name_row, textvariable=self.name_var)
        self.name_entry.pack(side="left", fill="x", expand=True)

        loc = ttk.Frame(right, style="Card.TFrame")
        loc.pack(fill="x", pady=(0, 4))
        ttk.Label(loc, text="位置", style="CardMuted.TLabel", width=6).pack(side="left")
        self.loc_label = ttk.Label(loc, text=str(self.cfg.instances_dir), style="Card.TLabel",
                                   wraplength=210, font=(FONT, 9))
        self.loc_label.pack(side="left", fill="x", expand=True)
        ttk.Button(loc, text="更改", width=6, command=self._choose_root).pack(side="right")

        self.assets_var = tk.BooleanVar(value=bool(self.cfg.get("download_assets", True)))
        self.share_var = tk.BooleanVar(value=bool(self.cfg.get("share_assets", True)))
        self.verify_var = tk.BooleanVar(value=bool(self.cfg.get("verify_existing", True)))
        for text, var in (("下载资源文件（声音、语言）", self.assets_var),
                          ("共享 assets / libraries", self.share_var),
                          ("校验已有文件（SHA1）", self.verify_var)):
            cb = ttk.Checkbutton(right, text=text, variable=var, style="TCheckbutton")
            cb.pack(anchor="w")

        row = ttk.Frame(right, style="Card.TFrame")
        row.pack(fill="x", pady=(4, 0))
        ttk.Label(row, text="并发", style="CardMuted.TLabel").pack(side="left")
        self.workers_var = tk.IntVar(value=int(self.cfg.get("workers", 8)))
        ttk.Spinbox(row, from_=1, to=32, textvariable=self.workers_var, width=4).pack(side="left", padx=(4, 10))
        self.java_label = ttk.Label(row, text=java.summary_line(), style="CardMuted.TLabel",
                                    wraplength=200, justify="left", font=(FONT, 9))
        self.java_label.pack(side="left", fill="x", expand=True)

        self.btn_start = ttk.Button(right, text="开始下载并创建实例", style="Accent.TButton",
                                    command=self._start_install)
        self.btn_start.pack(fill="x", pady=(8, 4))
        pair = ttk.Frame(right, style="Card.TFrame")
        pair.pack(fill="x")
        self.btn_cancel = ttk.Button(pair, text="取消", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left", fill="x", expand=True)
        self.btn_open = ttk.Button(pair, text="打开文件夹", command=self._open_last, state="disabled")
        self.btn_open.pack(side="left", fill="x", expand=True, padx=(6, 0))

        # 进度区
        prog = ttk.Frame(self.tab_install, style="Card.TFrame", padding=8)
        prog.pack(fill="x", pady=(10, 0))
        self.pbar = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.pbar.pack(fill="x")
        line = ttk.Frame(prog, style="Card.TFrame")
        line.pack(fill="x", pady=(6, 0))
        self.prog_label = ttk.Label(line, text="等待开始", style="Card.TLabel")
        self.prog_label.pack(side="left")
        self.prog_right = ttk.Label(line, text="", style="CardMuted.TLabel")
        self.prog_right.pack(side="right")

        self.log = scrolledtext.ScrolledText(self.tab_install, height=3, font=(FONT, 9),
                                             bg="#0f172a", fg="#d6e2f5", insertbackground="#d6e2f5",
                                             relief="flat", padx=10, pady=8, wrap="word")
        self.log.pack(fill="both", expand=False, pady=(8, 0))
        self.log.configure(state="disabled")

    def _build_instances_tab(self) -> None:
        top = ttk.Frame(self.tab_inst)
        top.pack(fill="x", pady=(10, 6))
        ttk.Button(top, text="刷新", command=self._reload_instances).pack(side="left")
        self.btn_import = ttk.Button(top, text="导入 PCL 目录", command=self._inst_import)
        self.btn_import.pack(side="left", padx=6)
        ttk.Button(top, text="打开文件夹", command=self._inst_open).pack(side="left")
        ttk.Button(top, text="复制路径", command=self._inst_copy).pack(side="left", padx=6)
        ttk.Button(top, text="校验文件", command=self._inst_verify).pack(side="left")
        ttk.Button(top, text="删除实例", command=self._inst_delete).pack(side="left", padx=6)

        wrap = ttk.Frame(self.tab_inst)
        wrap.pack(fill="both", expand=True)
        cols = ("name", "ver", "src", "size", "created", "mode", "loaders")
        self.itree = ttk.Treeview(wrap, columns=cols, show="headings", selectmode="browse")
        heads = {"name": "实例名", "ver": "版本", "src": "下载源", "size": "占用",
                 "created": "创建时间", "mode": "资源方式", "loaders": "模组加载器"}
        widths = {"name": 150, "ver": 80, "src": 120, "size": 70, "created": 95,
                  "mode": 95, "loaders": 220}
        for c in cols:
            self.itree.heading(c, text=heads[c])
            self.itree.column(c, width=widths[c], anchor="w")
        sb = ttk.Scrollbar(wrap, orient="vertical", command=self.itree.yview)
        self.itree.configure(yscrollcommand=sb.set)
        self.itree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.itree.bind("<Double-1>", lambda e: self._inst_open())
        self.itree.bind("<<TreeviewSelect>>", self._inst_selected_detail)

        bottom = ttk.Frame(self.tab_inst)
        bottom.pack(fill="x", pady=(6, 0))
        self.store_label = ttk.Label(bottom, text="", style="Muted.TLabel")
        self.store_label.pack(side="left")

        self.inst_log = scrolledtext.ScrolledText(self.tab_inst, height=4, font=(FONT, 9),
                                                  bg="#0f172a", fg="#d6e2f5", relief="flat",
                                                  padx=10, pady=8, wrap="word")
        self.inst_log.pack(fill="x", pady=(6, 0))
        self.inst_log.configure(state="disabled")

    def _build_loader_tab(self) -> None:
        box = ttk.Frame(self.tab_loader, style="Card.TFrame", padding=(14, 12))
        box.pack(fill="x", pady=(10, 8))

        ttk.Label(box, text="给已有实例装模组加载器", style="Section.TLabel").pack(anchor="w")
        ttk.Label(box,
                  text="Fabric / Quilt 走 meta 接口，几秒就好；Forge / NeoForge 要跑官方安装器，"
                       "会下载几十 MB 并给客户端打补丁，通常 2–5 分钟。装完启动器里会多一个版本。",
                  style="CardMuted.TLabel", wraplength=1080, justify="left").pack(anchor="w", pady=(2, 10))

        row = ttk.Frame(box, style="Card.TFrame")
        row.pack(fill="x", pady=3)
        ttk.Label(row, text="实例", style="CardMuted.TLabel", width=8).pack(side="left")
        self.li_instance_var = tk.StringVar()
        self.li_instance = ttk.Combobox(row, textvariable=self.li_instance_var,
                                        state="readonly", width=26, values=[])
        self.li_instance.pack(side="left")
        self.li_instance.bind("<<ComboboxSelected>>", self._li_on_change)
        self.li_mc_label = ttk.Label(row, text="", style="CardMuted.TLabel")
        self.li_mc_label.pack(side="left", padx=12)

        row2 = ttk.Frame(box, style="Card.TFrame")
        row2.pack(fill="x", pady=3)
        ttk.Label(row2, text="加载器", style="CardMuted.TLabel", width=8).pack(side="left")
        self.li_kind_var = tk.StringVar(value="Fabric")
        self.li_kind = ttk.Combobox(row2, textvariable=self.li_kind_var, state="readonly",
                                    width=10, values=["Fabric", "Quilt", "Forge", "NeoForge"])
        self.li_kind.pack(side="left")
        self.li_kind.bind("<<ComboboxSelected>>", self._li_on_change)
        ttk.Label(row2, text="版本", style="CardMuted.TLabel").pack(side="left", padx=(14, 6))
        self.li_version_var = tk.StringVar()
        self.li_version = ttk.Combobox(row2, textvariable=self.li_version_var,
                                       state="readonly", width=30, values=[])
        self.li_version.pack(side="left")
        self.li_refresh = ttk.Button(row2, text="刷新列表", command=self._li_load_versions)
        self.li_refresh.pack(side="left", padx=8)
        self.li_install = ttk.Button(row2, text="安装到实例", style="Accent.TButton",
                                     command=self._li_install)
        self.li_install.pack(side="right")

        self.li_log = scrolledtext.ScrolledText(self.tab_loader, height=16, font=(FONT, 9),
                                                bg="#0f172a", fg="#d6e2f5", relief="flat",
                                                padx=10, pady=8, wrap="word")
        self.li_log.pack(fill="both", expand=True, pady=(2, 0))
        self.li_log.configure(state="disabled")

    def _li_kind_key(self) -> str:
        t = self.li_kind_var.get().lower()
        if t.startswith("neo"):
            return "neoforge"
        if t.startswith("quilt"):
            return "quilt"
        if t.startswith("forge"):
            return "forge"
        return "fabric"

    def _li_instance_info(self):
        name = self.li_instance_var.get().strip()
        return (self._rows or {}).get(name) if name else None

    def _li_on_change(self, _evt=None) -> None:
        info = self._li_instance_info()
        self.li_mc_label.configure(text=f"原版 {info.version}" if info else "")
        self._li_load_versions()

    def _li_load_versions(self) -> None:
        info = self._li_instance_info()
        if not info:
            return
        kind = self._li_kind_key()
        self.li_version.configure(values=[])
        self.li_version_var.set("")

        def work():
            try:
                items = service.loader_versions(kind, info.version)
                self.q.put(("li_versions", (kind, items)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("li_log", f"获取 {kind} 版本失败：{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _li_install(self) -> None:
        info = self._li_instance_info()
        if not info:
            messagebox.showinfo("CTMD", "先在「实例」里选一个")
            return
        kind = self._li_kind_key()
        raw = self.li_version_var.get().strip()
        version = raw.split()[0] if raw else ""   # 标签形如 "0.19.5 稳定"，只取版本号
        if not version:
            messagebox.showinfo("CTMD", "先选加载器版本（点一下「刷新列表」）")
            return
        if getattr(self, "_li_busy", False):
            return
        self._li_busy = True
        self.li_install.configure(state="disabled")
        self.li_refresh.configure(state="disabled")
        self._li_note(f"=== 把 {kind} {version} 装进「{info.name}」（原版 {info.version}）===")

        def log(msg: str) -> None:
            self.q.put(("li_log", msg))

        def work():
            try:
                res = service.install_loader(self.cfg, info.name, kind, version=version, on_log=log)
                self.q.put(("li_done", res))
            except Exception as e:  # noqa: BLE001
                self.q.put(("li_error", str(e)))
            finally:
                self.q.put(("li_busy", False))

        threading.Thread(target=work, daemon=True).start()

    def _li_note(self, msg: str) -> None:
        self.li_log.configure(state="normal")
        self.li_log.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        self.li_log.see("end")
        self.li_log.configure(state="disabled")

    # ------------------------------------------------------------ 启动
    def _boot(self) -> None:
        self._log(f"CTMD {__version__} 启动，程序目录：{self.cfg.root}")
        self._log(f"共享仓库：{self.cfg.store_dir}")
        self._reload_instances()
        self._load_versions(False)
        threading.Thread(target=self._measure_bg, daemon=True).start()

    def _source_key(self) -> str:
        label = self.source_var.get()
        for key, text in SOURCE_CHOICES:
            if text == label:
                return key
        return "auto"

    def _source_label(self) -> str:
        want = str(self.cfg.get("source", "auto"))
        for key, text in SOURCE_CHOICES:
            if key == want:
                return text
        return SOURCE_CHOICES[0][1]

    def _on_source_change(self, _evt=None) -> None:
        self.cfg.set("source", self._source_key())
        self._log(f"下载源已切换为：{self.source_var.get()}")

    def _measure_bg(self) -> None:
        try:
            sm = service.SourceManager(preferred="auto")
            lat = sm.measure(timeout=8)
            sm.apply_preference()
            desc = sm.describe()
            self.q.put(("status", f"测速完成 · {desc}"))
            self.q.put(("log", f"源测速：{desc}"))
        except Exception as e:  # noqa: BLE001
            self.q.put(("log", f"测速失败：{e}"))

    def _measure(self) -> None:
        self.status.configure(text="正在测速…")
        threading.Thread(target=self._measure_bg, daemon=True).start()

    # ------------------------------------------------------------ 版本表
    def _load_versions(self, force: bool) -> None:
        self.btn_refresh.configure(state="disabled")
        self.count_label.configure(text="加载版本清单…")

        def work():
            try:
                inst = service.Installer(self.cfg, source=self._source_key())
                inst.prepare_sources(measure=False)
                man = inst.fetch_versions(force=force)
                self.q.put(("versions", man.get("versions", [])))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", f"加载版本清单失败：{e}"))
            finally:
                self.q.put(("refresh_done", None))

        threading.Thread(target=work, daemon=True).start()

    def _apply_filter(self) -> None:
        want = self.type_var.get()
        q = self.search_var.get().strip().lower()
        allowed = {"正式版": {"release"}, "快照": {"snapshot"},
                   "远古": {"old_beta", "old_alpha"}, "全部": None}[want]
        rows = []
        for v in self.versions:
            if allowed and v.get("type") not in allowed:
                continue
            if q and q not in str(v.get("id", "")).lower():
                continue
            rows.append(v)
        self.filtered = rows
        self.tree.delete(*self.tree.get_children())
        for v in rows[:600]:
            self.tree.insert("", "end", iid=v["id"], values=(
                v["id"], version_type_label(v.get("type")),
                str(v.get("releaseTime", ""))[:10]), tags=(v.get("type"),))
        self.count_label.configure(text=f"{len(rows)} 个版本" +
                                        ("" if len(rows) <= 600 else "（显示前 600）"))
        if rows and not self.tree.selection():
            first = rows[0]["id"]
            self.tree.selection_set(first)
            self.tree.see(first)
            self._on_select()

    def _selected(self) -> str:
        sel = self.tree.selection()
        return sel[0] if sel else ""

    def _on_select(self, _evt=None) -> None:
        vid = self._selected()
        if not vid:
            return
        # 只有没改过名字时才跟着版本号走，别抢用户输入
        current = self.name_var.get().strip()
        if not current or current == getattr(self, "_last_auto", ""):
            self.name_var.set(vid)
            self._last_auto = vid
        base = f"版本 {vid}\n"
        cached = self.detail_cache.get(vid)
        if cached:
            self.detail.configure(text=base + cached)
        else:
            self.detail.configure(text=base + "正在读取版本信息…")
            self._detail_seq += 1
            seq = self._detail_seq

            def work(v=vid, s=seq):
                try:
                    inst = service.Installer(self.cfg, source=self._source_key())
                    inst.prepare_sources(measure=False)
                    d = inst.fetch_version(v)
                    jv = int((d.get("javaVersion") or {}).get("majorVersion") or 8)
                    txt = (f"{version_type_label(d.get('type', ''))} · "
                           f"{str(d.get('releaseTime', ''))[:10]}\n"
                           f"需要 Java {jv} · 依赖库 {len(d.get('libraries', []))} 个")
                    self.q.put(("detail", (v, txt, jv, s)))
                except Exception as e:  # noqa: BLE001
                    self.q.put(("detail", (v, f"读取失败：{e}", 0, s)))

            threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 安装
    def _choose_root(self) -> None:
        d = filedialog.askdirectory(title="选择实例存放目录", initialdir=str(self.cfg.instances_dir))
        if d:
            self.cfg.set("instances_dir", d)
            self.loc_label.configure(text=d)
            self._log(f"实例目录改为：{d}")

    def _start_install(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        vid = self._selected()
        if not vid:
            messagebox.showinfo("CTMD", "先在左边选一个版本吧")
            return
        name = self.name_var.get().strip() or vid
        workers = max(1, min(32, int(self.workers_var.get() or 8)))
        self.cfg.update(workers=workers,
                        download_assets=bool(self.assets_var.get()),
                        share_assets=bool(self.share_var.get()),
                        verify_existing=bool(self.verify_var.get()),
                        last_version=vid)
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.btn_open.configure(state="disabled")
        self.pbar["value"] = 0
        self.prog_label.configure(text="准备中…")
        self.prog_right.configure(text="")
        self._log("=" * 46)
        self._log(f"开始：{vid} → 实例「{name}」")

        inst = service.Installer(self.cfg, source=self._source_key(), workers=workers,
                                 verify_existing=bool(self.verify_var.get()),
                                 include_assets=bool(self.assets_var.get()))
        self.installer = inst
        share = bool(self.share_var.get())

        def work():
            try:
                res = inst.install(
                    vid, name=name, share=share,
                    on_progress=lambda p: self.q.put(("progress", p)),
                    on_log=lambda m: self.q.put(("log", m)),
                )
                self.q.put(("done", res))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", str(e)))
            finally:
                self.q.put(("install_end", None))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _cancel(self) -> None:
        if self.installer:
            self.installer.cancel.set()
            self.btn_cancel.configure(state="disabled")
            self._log("正在取消…")

    def _open_last(self) -> None:
        if self.last_install_dir and Path(self.last_install_dir).exists():
            os.startfile(str(self.last_install_dir))

    # ------------------------------------------------------------ 实例页
    def _reload_instances(self) -> None:
        def work():
            items = inst_mod.list_instances(self.cfg)
            usage = inst_mod.store_usage(self.cfg)
            self.q.put(("instances", (items, usage)))

        threading.Thread(target=work, daemon=True).start()

    def _inst_selected(self) -> str:
        sel = self.itree.selection()
        return sel[0] if sel else ""

    def _inst_rows(self) -> dict:
        return getattr(self, "_rows", {})

    def _inst_open(self) -> None:
        name = self._inst_selected()
        rows = self._inst_rows()
        if name and name in rows:
            p = rows[name].mc_dir
            if p.exists():
                os.startfile(str(p))

    def _inst_copy(self) -> None:
        name = self._inst_selected()
        rows = self._inst_rows()
        if name and name in rows:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(rows[name].mc_dir))
            self._ilog(f"已复制路径：{rows[name].mc_dir}")

    def _inst_selected_detail(self, _evt=None) -> None:
        name = self._inst_selected()
        rows = self._inst_rows()
        info = rows.get(name) if name else None
        if not info:
            return
        if info.loaders:
            parts = "、".join(
                f"{inst_mod.loader_label(x.get('kind', ''))} {x.get('loader_version', '')}"
                for x in info.loaders)
            self._ilog(f"{info.name}：原版 {info.version}；已装 {parts}")
        else:
            self._ilog(f"{info.name}：原版 {info.version}；还没装模组加载器")

    def _inst_verify(self) -> None:
        name = self._inst_selected()
        if not name:
            return
        self._ilog(f"开始校验 {name} …")

        def work():
            try:
                r = inst_mod.verify_instance(self.cfg, name)
                self.q.put(("verify", r))
            except Exception as e:  # noqa: BLE001
                self.q.put(("ilog", f"校验失败：{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _inst_delete(self) -> None:
        name = self._inst_selected()
        if not name:
            return
        if not messagebox.askyesno(
                "删除实例",
                f"确定删除实例「{name}」？\n\n"
                "只会删除这个实例自己的存档、模组和设置；\n"
                "共享的 assets / libraries 不会被删除。"):
            return
        try:
            inst_mod.delete_instance(self.cfg, name)
            self._ilog(f"已删除实例：{name}")
        except Exception as e:  # noqa: BLE001
            self._ilog(f"删除失败：{e}")
        self._reload_instances()

    # ------------------------------------------------------------ 导入 PCL 目录
    def _inst_import(self) -> None:
        d = filedialog.askdirectory(
            title="选择 PCL 游戏目录（含 .minecraft 的那一层，或直接选 .minecraft）",
            initialdir=str(self.cfg.root))
        if not d:
            return
        pcl = Path(d)
        mc = (pcl / ".minecraft") if (pcl / ".minecraft").is_dir() else pcl
        if not (mc / "versions").is_dir():
            messagebox.showerror(
                "CTMD",
                f"这里没有 versions 目录：\n{mc}\n\n"
                "请选含 .minecraft 的那一层（比如 …\\PCL），或直接选 .minecraft。")
            return
        vids = inst_mod.list_versions(mc)
        if not vids:
            messagebox.showerror("CTMD", "这个目录里没有可用版本（versions\\*\\<id>.json 都没有）。")
            return
        _ImportDialog(self, mc, vids, self._run_import)

    def _run_import(self, mc: Path, vids: list, mode: str, share: str) -> None:
        self._ilog(f"开始导入 {len(vids)} 个版本：{mc}")
        self._ilog(f"数据方式 {mode}｜资源 {share}"
                   + ("（同盘硬链接，瞬间完成）" if mode == "link" else ""))
        self.btn_import.configure(state="disabled")

        def work():
            try:
                def log(msg: str) -> None:
                    self.q.put(("ilog", str(msg)))

                res = inst_mod.import_pcl(self.cfg, mc, versions=vids,
                                          mode=mode, share=share, log=log)
                self.q.put(("import_done", (res, mode, share)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", f"导入失败：{e}"))
            finally:
                self.q.put(("import_busy", False))

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ 队列泵
    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "ilog":
                    self._ilog(str(payload))
                elif kind == "status":
                    self.status.configure(text=str(payload))
                elif kind == "progress":
                    self._on_progress(payload)
                elif kind == "versions":
                    self.versions = payload
                    self._apply_filter()
                elif kind == "detail":
                    v, txt, jvm, seq = payload
                    if seq == self._detail_seq and v == self._selected():
                        self.detail_cache[v] = txt
                        self.detail.configure(text=txt)
                        own = java.find_for(jvm) if jvm else None
                        if jvm and not own:
                            self.java_label.configure(
                                text=f"⚠ 没找到 Java {jvm}，启动前需要先安装", foreground="#b45309")
                        else:
                            self.java_label.configure(
                                text=java.summary_line(), foreground=MUTED)
                elif kind == "refresh_done":
                    self.btn_refresh.configure(state="normal")
                elif kind == "install_end":
                    self.btn_start.configure(state="normal")
                    self.btn_cancel.configure(state="disabled")
                elif kind == "done":
                    self._on_done(payload)
                elif kind == "instances":
                    items, usage = payload
                    self._fill_instances(items, usage)
                elif kind == "verify":
                    self._on_verify(payload)
                elif kind == "li_versions":
                    _lk, items = payload
                    labels = [f"{x['version']}  {x['tag']}".strip() for x in items]
                    self.li_version.configure(values=labels)
                    if labels:
                        # 默认挑稳定/推荐的那个，而不是盲目选最新
                        pick = next((l for l in labels if ("推荐" in l or "稳定" in l)), labels[0])
                        self.li_version_var.set(pick)
                    else:
                        self._li_note(f"这个原版版本没有可用的 {_lk} 版本")
                elif kind == "li_log":
                    self._li_note(str(payload))
                elif kind == "li_done":
                    res = payload
                    self._li_note(f"完成：{res.version_id}（{res.seconds:.0f}s）")
                    self._reload_instances()
                    messagebox.showinfo(
                        "装好了",
                        f"{res.kind.title()} {res.loader_version} 已装进实例。\n\n"
                        f"版本名：{res.version_id}\n"
                        "在启动器里刷新一下版本列表就能看到它。")
                elif kind == "li_error":
                    self._li_note(f"失败：{payload}")
                    messagebox.showerror("安装失败", str(payload))
                elif kind == "li_busy":
                    self._li_busy = bool(payload)
                    self.li_install.configure(state="disabled" if payload else "normal")
                    self.li_refresh.configure(state="disabled" if payload else "normal")
                elif kind == "import_done":
                    res, _mode, _share = payload
                    ok = [r for r in res if r.ok]
                    bad = [r for r in res if not r.ok]
                    self._ilog(f"导入完成：{len(ok)}/{len(res)} 个实例")
                    self._reload_instances()
                    msg = f"成功导入 {len(ok)} 个实例。\n\n在列表里双击可以打开游戏目录。"
                    if bad:
                        detail = "\n".join(f"· {r.version}：{(r.notes or ['未知原因'])[0]}"
                                           for r in bad[:6])
                        msg += f"\n\n{len(bad)} 个失败：\n{detail}"
                    (messagebox.showinfo if not bad else messagebox.showwarning)(
                        "导入完成", msg)
                elif kind == "import_busy":
                    self.btn_import.configure(state="disabled" if payload else "normal")
                elif kind == "error":
                    self._log(f"错误：{payload}")
                    messagebox.showerror("CTMD", str(payload))
        except queue.Empty:
            pass
        self.root.after(80, self._pump)

    def _on_progress(self, p: Progress) -> None:
        self.pbar["value"] = p.percent
        self.prog_label.configure(
            text=f"{p.percent:5.1f}%   {human_size(p.done)} / {human_size(p.total)}   "
                 f"{p.files_done}/{p.files_total} 个文件")
        self.prog_right.configure(
            text=f"{human_size(p.speed)}/s   剩余 {_fmt_eta(p.eta)}"
                 + (f"   {p.current[:36]}" if p.current else ""))

    def _on_done(self, res) -> None:
        if res.cancelled:
            self.prog_label.configure(text="已取消")
            self._log("已取消。")
            return
        if not res.info:
            self.prog_label.configure(text="未完成（有文件下载失败）")
            return
        info = res.info
        self.last_install_dir = info.path
        self.btn_open.configure(state="normal")
        self.pbar["value"] = 100
        self.prog_label.configure(text=f"完成：{info.name}")
        self.prog_right.configure(text=f"用时 {res.elapsed:.0f}s")
        self._reload_instances()
        self._log(f"实例已就绪：{info.mc_dir}")
        msg = (f"实例「{info.name}」已创建完成。\n\n"
               f"版本：{info.version}（需要 Java {info.java_major}）\n"
               f"游戏目录：\n{info.mc_dir}\n\n"
               "PCL2 → 设置 → 启动器 → 游戏目录 → 添加，选这个文件夹即可。\n"
               "HMCL → 设置 → 游戏目录 → 添加游戏目录。\n\n"
               "现在打开文件夹吗？")
        if messagebox.askyesno("CTMD · 完成", msg):
            os.startfile(str(info.path))

    def _on_verify(self, r) -> None:
        if r.get("ok"):
            self._ilog(f"✓ {r['name']}：检查 {r['checked']} 个文件，全部正常")
            messagebox.showinfo("校验通过", f"{r['name']} 完好无损（{r['checked']} 个文件）。")
            return
        bad = r.get("missing", []) + r.get("corrupt", [])
        self._ilog(f"✗ {r['name']}：{len(bad)} 个问题")
        for b in bad[:6]:
            self._ilog(f"    {b}")
        messagebox.showwarning("发现问题",
                               f"{r['name']} 有 {len(bad)} 个文件缺失或损坏。\n"
                               "重新执行一次安装可以自动补齐（已下载的会跳过）。")

    def _fill_instances(self, items, usage) -> None:
        self._rows = {i.name: i for i in items}
        self.itree.delete(*self.itree.get_children())
        for i in items:
            mode = "共享（链接）" if "junction" in (i.link_mode or "") else "独立复制"
            created = (i.created or "").replace("T", " ")[:10]
            ld_text = "、".join(
                inst_mod.loader_label(x.get("kind", ""))
                for x in (i.loaders or [])) or "—"
            if i.loaders:
                ld_text += f"（{len(i.loaders)}）"
            self.itree.insert("", "end", iid=i.name, values=(
                i.name, i.version, i.source, i.size_text, created, mode, ld_text))
        if not items:
            self._ilog("还没有实例。切到「安装新版本」，选个版本下载就会出现在这里。")
        self.store_label.configure(
            text=f"共享仓库：资源 {human_size(usage['assets'])} · 依赖库 {human_size(usage['libraries'])}"
                 f" · 版本本体 {human_size(usage['versions'])}"
                 f"　（实例内只存自己的存档和模组）")

        # 同步「模组加载器」页的实例下拉框
        if hasattr(self, "li_instance"):
            names = [i.name for i in items]
            self.li_instance.configure(values=names)
            if names and self.li_instance_var.get() not in names:
                self.li_instance_var.set(names[0])
                self.li_mc_label.configure(text=f"原版 {items[0].version}")
                self.li_version.configure(values=[])
                self.li_version_var.set("")

    # ------------------------------------------------------------ 日志
    def _log(self, msg: str) -> None:
        self._append(self.log, msg)

    def _ilog(self, msg: str) -> None:
        self._append(self.inst_log, msg)

    def _append(self, widget, msg: str) -> None:
        widget.configure(state="normal")
        widget.insert("end", f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        widget.see("end")
        widget.configure(state="disabled")

    def run(self) -> None:
        self.root.mainloop()


def run(cfg: Config) -> None:
    enable_dpi()
    net_mod.bind_slow_hosts(cfg.store("cache", "slow_hosts.json"))
    App(cfg).run()


class _ImportDialog(tk.Toplevel):
    """导入 PCL 目录前的选项窗口。

    PCL 开版本隔离时 gameDir = versions\\<id>\\，跟 CTMD 的实例一一对应，
    所以这里只需要问「数据怎么搬」和「资源怎么接」两件事。
    """

    MODES = (
        ("copy", "复制一份（最安全，原目录完全不动）"),
        ("link", "同盘硬链接（不占空间，但和原目录共享同一份数据）"),
        ("move", "搬过来（省空间，原目录里的数据会被移走）"),
    )
    SHARES = (
        ("local", "指回原目录（0 复制，推荐）"),
        ("store", "收进 CTMD 共享仓库"),
        ("copy", "复制进每个实例（最占空间）"),
    )

    def __init__(self, app, mc: Path, vids: list, on_start) -> None:
        super().__init__(app.root)
        self.title("导入 PCL 目录")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.app = app
        self.mc = mc
        self.vids = vids
        self.on_start = on_start

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="把已经玩很久的 PCL 目录搬进 CTMD",
                  style="Section.TLabel").pack(anchor="w")
        ttk.Label(body, text=str(mc), style="Muted.TLabel",
                  wraplength=580, justify="left").pack(anchor="w", pady=(4, 6))
        ttk.Label(body, text=f"找到 {len(vids)} 个版本",
                  style="Muted.TLabel").pack(anchor="w")
        preview = " / ".join(vids[:4])
        if len(vids) > 4:
            preview += f" 等 {len(vids)} 个"
        ttk.Label(body, text=preview, style="Muted.TLabel", wraplength=580,
                  justify="left").pack(anchor="w", pady=(2, 12))

        # 默认：同盘就用硬链接（快、不占地方），跨盘只能复制
        same_drive = (str(mc.drive).upper() == str(self.app.cfg.instances_dir.drive).upper())
        self.mode = tk.StringVar(value="link" if same_drive else "copy")
        box = ttk.LabelFrame(body, text="存档 / 模组怎么搬", padding=10)
        box.pack(fill="x")
        for val, label in self.MODES:
            ttk.Radiobutton(box, text=label, value=val,
                            variable=self.mode).pack(anchor="w")
        if same_drive:
            ttk.Label(box, text=f"检测到同一块磁盘（{mc.drive}），默认硬链接，几乎瞬间完成。",
                      style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        else:
            ttk.Label(box, text=f"源目录在 {mc.drive} 盘，跨盘不能硬链接，默认复制。",
                      style="Muted.TLabel").pack(anchor="w", pady=(4, 0))

        self.share = tk.StringVar(value="local")
        box2 = ttk.LabelFrame(body, text="assets / libraries（体积大头）", padding=10)
        box2.pack(fill="x", pady=(10, 0))
        for val, label in self.SHARES:
            ttk.Radiobutton(box2, text=label, value=val,
                            variable=self.share).pack(anchor="w")

        ttk.Label(body,
                  text="导入后可以在列表里「校验文件」，缺的用 ctmd.py repair 补。",
                  style="Muted.TLabel", wraplength=600,
                  justify="left").pack(anchor="w", pady=(12, 0))

        row = ttk.Frame(body)
        row.pack(fill="x", pady=(14, 0))
        ttk.Button(row, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(row, text="开始导入", style="Accent.TButton",
                   command=self._ok).pack(side="right", padx=8)

        self.update_idletasks()
        self._center(app.root)
        self.grab_set()
        self.focus_set()

    def _center(self, parent) -> None:
        try:
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            w, h = self.winfo_width(), self.winfo_height()
            self.geometry(f"+{px + max(0, (pw - w) // 2)}+{py + max(0, (ph - h) // 3)}")
        except Exception:
            pass

    def _ok(self) -> None:
        mode, share = self.mode.get(), self.share.get()
        self.destroy()
        self.on_start(self.mc, self.vids, mode, share)
