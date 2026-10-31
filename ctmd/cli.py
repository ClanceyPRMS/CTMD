"""命令行界面：适合脚本、排错、批量装版本。"""

from __future__ import annotations

import argparse
import unicodedata
import os
import sys
import time
from pathlib import Path

from . import __version__, instance as inst_mod, java, net, service
from .config import Config
from .fsutil import human_size
from .manifest import version_type_label


def _fmt_eta(sec: float) -> str:
    if sec <= 0:
        return "--:--"
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class _Bar:
    def __init__(self, enabled: bool = True):
        self.enabled = enabled and sys.stdout.isatty()
        self.last = 0.0

    def __call__(self, p) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if now - self.last < 0.2 and p.done < p.total:
            return
        self.last = now
        width = 28
        filled = int(width * p.percent / 100)
        bar = "█" * filled + "░" * (width - filled)
        sys.stdout.write(
            f"\r  [{bar}] {p.percent:5.1f}%  "
            f"{human_size(p.done)}/{human_size(p.total)}  "
            f"{human_size(p.speed)}/s  剩余 {_fmt_eta(p.eta)}   "
        )
        sys.stdout.flush()
        if p.percent >= 100:
            sys.stdout.write("\n")


def cmd_list(cfg: Config, args) -> int:
    inst = service.Installer(cfg)
    inst.prepare_sources(measure=False)
    man = inst.fetch_versions(force=args.refresh)
    versions = man.get("versions", [])
    if args.type != "all":
        versions = [v for v in versions if v.get("type") == args.type]
    if args.search:
        q = args.search.lower()
        versions = [v for v in versions if q in v["id"].lower()]
    for v in versions[: args.limit]:
        print(f"{v['id']:<22} {version_type_label(v.get('type')):<9} {v.get('releaseTime', '')[:10]}")
    print(f"\n共 {len(versions)} 个版本"
          + ("" if len(versions) <= args.limit else f"，已显示前 {args.limit} 个"))
    return 0


def cmd_install(cfg: Config, args) -> int:
    cfg.update(source=args.source, workers=args.workers,
               share_assets=not args.copy)
    inst = service.Installer(cfg, source=args.source, workers=args.workers,
                             include_assets=not args.no_assets)
    bar = _Bar()

    def log(msg: str) -> None:
        if bar.enabled:
            sys.stdout.write("\r" + " " * 100 + "\r")
        print(msg)

    def on_file(task, status, detail) -> None:
        if args.verbose and status != "skip":
            log(f"  [{status}] {task.name} {detail}")

    res = inst.install(args.version, name=args.name or "",
                       share=not args.copy, on_progress=bar,
                       on_log=log, on_file=on_file)
    if res.cancelled:
        print("已取消。")
        return 130
    if not res.info:
        print("安装未完成。", file=sys.stderr)
        return 1
    print(f"\n实例已就绪：{res.info.path}")
    print(f"游戏目录：{res.info.mc_dir}")
    print(f"版本：{res.info.version}    需要 Java {res.info.java_major}")
    print("用 PCL2 / HMCL 把这个 .minecraft 文件夹添加为游戏目录即可启动。")
    return 0


def _dwidth(s: str) -> int:
    """显示宽度：中日韩全角字符算 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in str(s))


def _pad(s, width: int) -> str:
    s = str(s)
    w = _dwidth(s)
    if w > width:
        out, acc = "", 0
        for c in s:
            cw = 2 if unicodedata.east_asian_width(c) in ("W", "F") else 1
            if acc + cw > width - 1:
                break
            out += c
            acc += cw
        s, w = out + "…", acc + 1
    return s + " " * max(0, width - w)


def cmd_instances(cfg: Config, args) -> int:
    items = inst_mod.list_instances(cfg)
    if not items:
        print("还没有实例。用 `ctmd.py install <版本号>` 创建一个吧。")
        return 0
    print(f"{_pad('实例名', 34)}{_pad('版本', 26)}{_pad('来源', 10)}"
          f"{_pad('占用', 10)}{_pad('创建时间', 17)}加载器")
    for i in items:
        ld = "、".join(f"{inst_mod.loader_label(x.get('kind',''))} {x.get('loader_version','')}".strip()
                       for x in (i.loaders or [])) or "—"
        print(f"{_pad(i.name, 34)}{_pad(i.version, 26)}{_pad(i.source, 10)}"
              f"{_pad(i.size_text, 10)}{_pad(i.created[:16], 17)}{ld}")
    usage = inst_mod.store_usage(cfg)
    print(f"\n共享仓库：assets {human_size(usage['assets'])}，"
          f"libraries {human_size(usage['libraries'])}，"
          f"版本本体 {human_size(usage['versions'])}")
    return 0


def cmd_verify(cfg: Config, args) -> int:
    r = inst_mod.verify_instance(cfg, args.name)
    print(f"检查 {r['checked']} 个文件…")
    for n in (r.get("notes") or []):
        print(f"  提示：{n}")
    if r["ok"]:
        print("✓ 完整，没有发现问题。")
        return 0
    if r["missing"]:
        print(f"✗ 缺少 {len(r['missing'])} 个文件：")
        for m in r["missing"][:15]:
            print("   ", m)
        if len(r["missing"]) > 15:
            print(f"    …… 还有 {len(r['missing']) - 15} 个")
    if r["corrupt"]:
        print(f"✗ 损坏 {len(r['corrupt'])} 个文件：")
        for m in r["corrupt"][:15]:
            print("   ", m)
    print("\n重新执行同样的 install 命令即可补齐（已存在的文件会自动跳过）。")
    return 1


def cmd_remove(cfg: Config, args) -> int:
    target = cfg.instances_dir / inst_mod.sanitize_name(args.name)
    if not target.exists():
        print(f"没有这个实例：{args.name}", file=sys.stderr)
        return 1
    if not args.yes:
        ans = input(f"删除实例「{args.name}」？存档和模组会一起删除（共享资源不受影响）[y/N] ")
        if ans.strip().lower() not in ("y", "yes"):
            print("已取消。")
            return 0
    inst_mod.delete_instance(cfg, args.name)
    print(f"已删除：{target}")
    return 0


def cmd_clean(cfg: Config, args) -> int:
    before = inst_mod.store_usage(cfg)
    removed = inst_mod.prune_store_versions(cfg)
    if not removed:
        print("没有需要清理的版本本体。")
        return 0
    for name, size in removed:
        print(f"  清理 {name}（{human_size(size)}）")
    after = inst_mod.store_usage(cfg)
    saved = before["versions"] - after["versions"]
    print(f"共释放 {human_size(saved)}；共享资源（assets/libraries）未改动。")
    return 0


def cmd_loader_list(cfg: Config, args) -> int:
    mc = args.mc
    if not mc and args.instance:
        info = service._find_instance(cfg, args.instance)
        mc = info.version if info else ""
    if not mc:
        print("需要 --mc 或 --instance", file=sys.stderr)
        return 1
    vers = service.loader_versions(args.kind, mc)
    if not vers:
        print(f"{args.kind} 没有 {mc} 的版本")
        return 1
    for v in vers[: args.limit]:
        tag = f"  [{v['tag']}]" if v["tag"] else ""
        print(f"{v['version']:<24}{tag}")
    print(f"\n{mc} 共 {len(vers)} 个 {args.kind} 版本"
          + ("" if len(vers) <= args.limit else f"，已显示前 {args.limit} 个"))
    return 0


def cmd_loader_install(cfg: Config, args) -> int:
    print(f"把 {args.kind} 装进实例「{args.instance}」…")

    def log(msg: str) -> None:
        print(msg, flush=True)

    try:
        res = service.install_loader(cfg, args.instance, args.kind,
                                     version=args.loader or "",
                                     java_exe=args.java or "",
                                     on_log=log)
    except Exception as e:  # noqa: BLE001
        print(f"失败：{e}", file=sys.stderr)
        return 1
    print(f"\n完成：{res.version_id}")
    print(f"版本目录：{res.version_dir}")
    print(f"用时 {res.seconds:.0f}s。用启动器刷新一下，版本列表里就会出现它。")
    return 0


def cmd_doctor(cfg: Config, args) -> int:
    print(service.doctor(cfg))
    return 0


def cmd_gui(cfg: Config, args) -> int:
    from . import gui
    gui.run(cfg)
    return 0


def cmd_import(cfg: Config, args) -> int:
    pcl = Path(args.pcl_dir).expanduser()
    mc = (pcl / ".minecraft") if (pcl / ".minecraft").is_dir() else pcl
    if not (mc / "versions").is_dir():
        print(f"找不到 {mc}{os.sep}versions —— 这不是一个 PCL / 官方启动器的游戏目录。",
              file=sys.stderr)
        return 1
    vids = inst_mod.list_versions(mc)
    if not vids:
        print("目录里没有可用版本（versions/*/ *.json 都没有）。", file=sys.stderr)
        return 1
    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        vids = [v for v in vids if v in want]
        if not vids:
            print("--only 里的版本一个都没匹配上。", file=sys.stderr)
            return 1

    print(f"游戏目录：{mc}")
    print(f"找到 {len(vids)} 个版本｜数据方式：{args.mode}｜资源：{args.share}")
    if args.dry_run:
        for v in vids:
            print("  -", v)
        print("\n（--dry-run，什么都没做）")
        return 0

    results = inst_mod.import_pcl(cfg, mc, versions=vids,
                                  mode=args.mode, share=args.share, log=print)
    ok = [r for r in results if r.ok]
    total = sum(r.size for r in ok)
    print(f"\n完成 {len(ok)}/{len(results)} 个实例，"
          f"共 {human_size(total)}")
    for r in ok:
        st = "，".join(f"{k} {v}" for k, v in (r.stats or {}).items())
        print(f"  ✓ {r.name:<40} {st}")
    bad = [r for r in results if not r.ok]
    if bad:
        print("\n失败：")
        for r in bad:
            print(f"  ✗ {r.version}：{r.notes}")
        return 1
    return 0


def cmd_repair(cfg: Config, args) -> int:
    names = [args.name] if args.name else [i.name for i in inst_mod.list_instances(cfg)]
    if not names:
        print("没有实例可修。")
        return 0
    rc = 0
    for name in names:
        print(f"\n== {name}")
        try:
            dry = inst_mod.repair_instance(cfg, name, workers=args.workers,
                                           include_assets=not args.no_assets, dry_run=True)
        except FileNotFoundError as e:
            print(f"  ✗ {e}")
            rc = 1
            continue
        if not dry["count"]:
            print("  ✓ 没有缺失，不用补")
            continue
        if args.dry_run:
            print(f"  需要补 {dry['count']} 个文件：")
            for t in dry["tasks"][:20]:
                print(f"    - {t.kind:8s} {t.name}")
            if dry["count"] > 20:
                print(f"    …… 还有 {dry['count'] - 20} 个")
            continue
        bar = _Bar()
        res = inst_mod.repair_instance(cfg, name, workers=args.workers,
                                       include_assets=not args.no_assets,
                                       log=print if not bar.enabled else None,
                                       on_progress=bar)
        s = res["stats"]
        print(f"  补齐 {res['count']} 个文件，下载 {s.downloaded} 个"
              + (f"，失败 {len(s.failed)}" if s.failed else ""))
        if s.failed:
            rc = 1
            for t, why in s.failed[:5]:
                print(f"    ✗ {t.name}：{why}")
        chk = inst_mod.verify_instance(cfg, name)
        print("  ✓ 复检通过" if chk["ok"]
              else f"  ✗ 复检还差 {len(chk['missing'])} 个文件")
        if not chk["ok"]:
            rc = 1
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ctmd",
        description="CTMD · Creative Trinity Minecraft Downloader —— 原版 Minecraft 下载器",
    )
    p.add_argument("--version", action="version", version=f"CTMD {__version__}")
    p.add_argument("--root", help="程序根目录（默认就是 ctmd.py 所在目录）")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("list", help="列出版本")
    sp.add_argument("--type", default="release",
                    choices=["release", "snapshot", "old_beta", "old_alpha", "all"])
    sp.add_argument("--search", default="")
    sp.add_argument("--limit", type=int, default=30)
    sp.add_argument("--refresh", action="store_true", help="强制刷新版本清单")
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("install", help="下载并创建实例")
    sp.add_argument("version")
    sp.add_argument("--name", default="", help="实例名（默认用版本号）")
    sp.add_argument("--source", default="auto", choices=["auto", "official", "bmclapi"])
    sp.add_argument("--workers", type=int, default=8)
    sp.add_argument("--no-assets", action="store_true", help="不下载资源文件（没声音）")
    sp.add_argument("--copy", action="store_true", help="复制资源而不共享（占空间，但可整体搬走）")
    sp.add_argument("-v", "--verbose", action="store_true")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("import", help="把已有 PCL / 官方启动器目录导入为实例")
    sp.add_argument("pcl_dir", help="游戏目录（含 .minecraft 的那一层，或直接给 .minecraft）")
    sp.add_argument("--only", default="", help="只导这几个版本，逗号分隔")
    sp.add_argument("--mode", default="copy", choices=["copy", "link", "move"],
                    help="copy=复制一份（安全，占空间）；link=同盘硬链接（0 空间，但与源共享数据）；"
                         "move=搬过来（源目录里的数据会被移走）")
    sp.add_argument("--share", default="local", choices=["local", "store", "copy"],
                    help="assets/libraries 怎么接：local=指向原目录；store=指向共享仓库；copy=复制进实例")
    sp.add_argument("--dry-run", action="store_true", help="只列出会导入哪些版本")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("instances", help="查看已安装实例")
    sp.set_defaults(func=cmd_instances)

    sp = sub.add_parser("verify", help="校验实例文件完整性")
    sp.add_argument("name")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("repair", help="按校验结果补齐缺失文件（PCL 目录常缺）")
    sp.add_argument("name", nargs="?", default="", help="实例名（不给=全部）")
    sp.add_argument("--workers", type=int, default=8)
    sp.add_argument("--no-assets", action="store_true", help="不补资源文件")
    sp.add_argument("--dry-run", action="store_true", help="只看要补什么")
    sp.set_defaults(func=cmd_repair)

    sp = sub.add_parser("remove", help="删除实例（共享资源不受影响）")
    sp.add_argument("name")
    sp.add_argument("-y", "--yes", action="store_true", help="不再确认")
    sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser("doctor", help="网络与 Java 环境体检")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("clean", help="清理没有实例引用的版本本体")
    sp.set_defaults(func=cmd_clean)

    lp = sub.add_parser("loader", help="装 Fabric / Forge")
    lsub = lp.add_subparsers(dest="loader_cmd")

    sp = lsub.add_parser("list", help="列出某个 MC 版本可用的加载器版本")
    sp.add_argument("kind", choices=["fabric", "quilt", "forge", "neoforge"])
    sp.add_argument("--mc", default="")
    sp.add_argument("--instance", default="")
    sp.add_argument("--limit", type=int, default=25)
    sp.set_defaults(func=cmd_loader_list)

    sp = lsub.add_parser("install", help="装进已有实例")
    sp.add_argument("kind", choices=["fabric", "quilt", "forge", "neoforge"])
    sp.add_argument("--instance", required=True)
    sp.add_argument("--loader", default="", help="加载器版本（默认最新稳定）")
    sp.add_argument("--java", default="", help="指定 Java 可执行文件（装 Forge 用）")
    sp.set_defaults(func=cmd_loader_install)

    sp = sub.add_parser("gui", help="打开图形界面")
    sp.set_defaults(func=cmd_gui)
    return p


def main(root: Path) -> int:
    parser = build_parser()
    args = parser.parse_args()
    root = Path(args.root).resolve() if args.root else Path(root).resolve()
    cfg = Config(root)
    if not cfg.path.exists():
        cfg.save()          # 首次运行就把配置写出来，用户知道文件在哪
    cfg.ensure_dirs()
    net.bind_slow_hosts(cfg.store("cache", "slow_hosts.json"))
    if not getattr(args, "func", None):
        return cmd_gui(cfg, args)
    try:
        return args.func(cfg, args)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"出错：{e}", file=sys.stderr)
        return 1
