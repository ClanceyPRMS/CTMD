"""CTMD 核心单元测试（不联网）。

运行：
    python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ctmd import fsutil, instance as inst_mod, manifest  # noqa: E402


class RuleTests(unittest.TestCase):
    def test_no_rules_allows(self):
        self.assertTrue(manifest.rules_allow(None))
        self.assertTrue(manifest.rules_allow([]))

    def test_allow_only_other_os(self):
        rules = [{"action": "allow", "os": {"name": "osx"}}]
        self.assertFalse(manifest.rules_allow(rules))

    def test_allow_then_disallow(self):
        rules = [{"action": "allow"},
                 {"action": "disallow", "os": {"name": "windows"}}]
        self.assertFalse(manifest.rules_allow(rules))

    def test_features_default_false(self):
        rules = [{"action": "allow", "features": {"has_custom_resolution": True}}]
        self.assertFalse(manifest.rules_allow(rules))
        self.assertTrue(manifest.rules_allow(rules, {"has_custom_resolution": True}))

    def test_arch_x86_not_match(self):
        rules = [{"action": "allow", "os": {"name": "windows", "arch": "x86"}}]
        self.assertFalse(manifest.rules_allow(rules))


class MavenTests(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(manifest.library_jar_path("org.lwjgl:lwjgl:2.9.0"),
                         "org/lwjgl/lwjgl/2.9.0/lwjgl-2.9.0.jar")

    def test_classifier(self):
        self.assertEqual(
            manifest.library_jar_path("org.lwjgl:lwjgl:3.3.1:natives-windows"),
            "org/lwjgl/lwjgl/3.3.1/lwjgl-3.3.1-natives-windows.jar")


class InstanceNameTests(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(inst_mod.sanitize_name('a/b:c*?"<>|'), "a_b_c______")
        self.assertEqual(inst_mod.sanitize_name("  "), "新实例")
        self.assertEqual(inst_mod.sanitize_name("CON"), "MC-CON")


class SafeDeleteTests(unittest.TestCase):
    """最危险的那条路：删实例绝不能删到共享仓库。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ctmd-test-"))
        self.store = self.tmp / "store"
        (self.store / "assets" / "objects").mkdir(parents=True)
        (self.store / "assets" / "objects" / "abc").write_bytes(b"x" * 1024)
        (self.store / "libraries").mkdir(parents=True)
        (self.store / "libraries" / "lib.jar").write_bytes(b"y" * 512)

    def tearDown(self):
        fsutil.safe_rmtree(self.tmp)

    def test_delete_instance_keeps_shared_store(self):
        inst = self.tmp / "instances" / "demo"
        mc = inst / ".minecraft"
        (mc / "saves").mkdir(parents=True)
        (mc / "saves" / "world.dat").write_bytes(b"s")
        self.assertTrue(fsutil.make_junction(mc / "assets", self.store / "assets"))
        self.assertTrue(fsutil.make_junction(mc / "libraries", self.store / "libraries"))

        fsutil.safe_rmtree(inst)

        self.assertFalse(inst.exists(), "实例目录应该被删除")
        self.assertTrue((self.store / "assets" / "objects" / "abc").exists(),
                        "共享 assets 必须完好")
        self.assertTrue((self.store / "libraries" / "lib.jar").exists(),
                        "共享 libraries 必须完好")

    def test_dir_size_ignores_junction(self):
        inst = self.tmp / "i" / ".minecraft"
        inst.mkdir(parents=True)
        (inst / "a.bin").write_bytes(b"z" * 100)
        fsutil.make_junction(inst / "assets", self.store / "assets")
        self.assertEqual(fsutil.dir_size(inst), 100)


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ctmd-inst-"))

    def tearDown(self):
        fsutil.safe_rmtree(self.tmp)

    def test_create_instance_layout(self):
        from ctmd.config import Config
        from ctmd.manifest import Plan

        cfg = Config(self.tmp)
        cfg.ensure_dirs()
        store = cfg.store_dir
        (store / "versions" / "9.9").mkdir(parents=True)
        (store / "versions" / "9.9" / "9.9.jar").write_bytes(b"jar")
        (store / "assets" / "indexes").mkdir(parents=True, exist_ok=True)
        (store / "libraries").mkdir(parents=True, exist_ok=True)

        vjson = {"id": "9.9", "type": "release", "libraries": [],
                 "downloads": {"client": {"size": 3}}}
        plan = Plan(version_id="9.9", version_json=vjson,
                    jar_dest=store / "versions" / "9.9" / "9.9.jar")

        info = inst_mod.create_instance(cfg, "菜鸟服", plan, source_label="测试")
        mc = info.mc_dir
        self.assertTrue((mc / "versions" / "9.9" / "9.9.json").exists())
        self.assertTrue((mc / "versions" / "9.9" / "9.9.jar").exists())
        self.assertTrue((mc / "saves").is_dir())
        self.assertTrue((cfg.instances_dir / "菜鸟服" / "ctmd.json").exists())
        self.assertTrue(fsutil.is_reparse_point(mc / "assets"))
        meta = json.loads((cfg.instances_dir / "菜鸟服" / "ctmd.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["version"], "9.9")

        items = inst_mod.list_instances(cfg)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].version, "9.9")


class PclImportTests(unittest.TestCase):
    """PCL2 版本隔离目录 → CTMD 实例的导入回归。

    PCL 开隔离时 gameDir = versions/<id>/，CTMD 的实例 gameDir = .minecraft/，
    导入就是把游戏数据上提一层、版本本体归位。合并版 JSON 没有 inheritsFrom，
    加载器只能靠 mainClass / 库坐标认。
    """

    def setUp(self):
        from ctmd.config import Config
        self.tmp = Path(tempfile.mkdtemp(prefix="ctmd-pcl-"))
        self.cfg = Config(self.tmp / "ctmd")
        self.cfg.ensure_dirs()
        self.pcl = self.tmp / "PCL" / ".minecraft"
        (self.pcl / "versions").mkdir(parents=True)
        (self.pcl / "libraries").mkdir()
        (self.pcl / "assets" / "indexes").mkdir(parents=True)

    def tearDown(self):
        fsutil.safe_rmtree(self.tmp)

    def _make(self, vid, main_class, libs, world="世界一"):
        vdir = self.pcl / "versions" / vid
        vdir.mkdir(parents=True)
        all_libs = list(libs)
        all_libs.append({"name": "org.lwjgl:lwjgl:3.3.1", "downloads": {"artifact": {
            "path": "org/lwjgl/lwjgl/3.3.1/lwjgl-3.3.1.jar", "url": "https://x/",
            "size": 3, "sha1": ""}}})
        vjson = {"id": vid, "type": "release", "mainClass": main_class,
                 "libraries": all_libs,
                 "downloads": {"client": {"url": "https://x/c.jar", "size": 3, "sha1": ""}},
                 "javaVersion": {"majorVersion": 17}}
        (vdir / f"{vid}.json").write_text(json.dumps(vjson), encoding="utf-8")
        (vdir / f"{vid}.jar").write_bytes(b"jar")
        (vdir / "saves" / world).mkdir(parents=True)
        (vdir / "saves" / world / "level.dat").write_bytes(b"lv")
        (vdir / "mods").mkdir()
        (vdir / "mods" / "a.jar").write_bytes(b"mod")
        (vdir / "options.txt").write_text("fov:70", encoding="utf-8")
        (vdir / "PCL").mkdir()          # PCL 的版本级设置，应留在原地
        for lib in all_libs:
            rel = ((lib.get("downloads") or {}).get("artifact") or {}).get("path")
            if not rel:
                from ctmd.manifest import library_jar_path
                try:
                    rel = library_jar_path(lib["name"])
                except Exception:
                    continue
            f = self.pcl / "libraries" / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"lib")
        return vid

    def test_game_data_moves_up_and_version_files_stay(self):
        vid = self._make("1.20.1-Fabric 0.14.9", "net.fabricmc.loader.impl.launch.knot.KnotClient",
                         [{"name": "net.fabricmc:fabric-loader:0.14.9"}])
        r = inst_mod.import_pcl_version(self.cfg, self.pcl, vid, mode="copy", share="local")
        self.assertTrue(r.ok)
        mc = self.cfg.instances_dir / r.name / ".minecraft"
        self.assertTrue((mc / "versions" / vid / f"{vid}.json").exists())
        self.assertTrue((mc / "versions" / vid / f"{vid}.jar").exists())
        self.assertTrue((mc / "saves" / "世界一" / "level.dat").exists())
        self.assertTrue((mc / "mods" / "a.jar").exists())
        self.assertTrue((mc / "options.txt").exists())
        self.assertFalse((mc / "versions" / vid / "saves").exists())
        # share=local：assets/libraries 指向原 PCL 目录
        self.assertTrue(fsutil.is_reparse_point(mc / "libraries"))
        # PCL 的版本级设置不搬
        self.assertTrue((self.pcl / "versions" / vid / "PCL").is_dir())

    def test_merged_version_loader_detected(self):
        fab = self._make("1.20.1-Fabric 0.14.9",
                         "net.fabricmc.loader.impl.launch.knot.KnotClient",
                         [{"name": "net.fabricmc:fabric-loader:0.14.9"},
                          {"name": "optifine:OptiFine:1.20.1_HD_U_I6"}])
        forge = self._make("1.18.2-Forge_40.2.1", "cpw.mods.bootstraplauncher.BootstrapLauncher",
                           [{"name": "net.minecraftforge:accesstransformers:8.0.4"},
                            {"name": "net.minecraftforge:fmlloader:1.18.2-40.2.1"}])
        vanilla = self._make("1.1", "net.minecraft.launchwrapper.Launch", [])
        got = {x["version_id"]: x for x in inst_mod.detect_loaders(self.pcl)}
        self.assertEqual(got[fab]["kind"], "Fabric")
        self.assertEqual(got[fab]["loader_version"], "0.14.9")
        self.assertEqual(got[forge]["kind"], "Forge")
        # 别被 accesstransformers:8.0.4 骗了
        self.assertEqual(got[forge]["loader_version"], "40.2.1")
        self.assertNotIn(vanilla, got)          # 原版 launchwrapper ≠ OptiFine

    def test_verify_uses_local_paths_not_store(self):
        vid = self._make("1.20.1-Fabric 0.14.9", "net.fabricmc.loader.impl.launch.knot.KnotClient",
                         [{"name": "net.fabricmc:fabric-loader:0.14.9"}])
        r = inst_mod.import_pcl_version(self.cfg, self.pcl, vid, mode="copy", share="local")
        res = inst_mod.verify_instance(self.cfg, r.name)
        # store 里一个文件都没有，但实例通过 junction 能看见全部 → 不该报缺失
        self.assertTrue(res["ok"], f"missing={res['missing'][:5]} corrupt={res['corrupt'][:5]}")
        self.assertGreater(res["checked"], 2)


class LoaderUrlTests(unittest.TestCase):
    def test_forge_installer_url(self):
        from ctmd import loaders
        self.assertEqual(
            loaders.forge_installer_url("26.3", "66.0.13"),
            "https://maven.minecraftforge.net/net/minecraftforge/forge/"
            "26.3-66.0.13/forge-26.3-66.0.13-installer.jar")

    def test_forge_mirror_url(self):
        from ctmd import loaders
        url = loaders.forge_installer_url("1.20.1", "47.4.28", mirror=True)
        self.assertTrue(url.startswith(
            "https://bmclapi2.bangbang93.com/maven/net/minecraftforge/forge/1.20.1-47.4.28/"))

    def test_forge_url_has_no_doubled_group_path(self):
        from ctmd import loaders
        url = loaders.forge_installer_url("26.3", "66.0.13")
        self.assertEqual(url.count("/net/minecraftforge/forge/"), 1)


class InheritVersionTests(unittest.TestCase):
    """加载器版本靠 inheritsFrom 继承原版，自己没有 jar —— verify 不能误报。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ctmd-inherit-"))

    def tearDown(self):
        fsutil.safe_rmtree(self.tmp)

    def _make(self, vid, vjson, jar=True):
        mc = self.tmp / "instances" / "demo" / ".minecraft"
        d = mc / "versions" / vid
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{vid}.json").write_text(json.dumps(vjson), encoding="utf-8")
        if jar:
            (d / f"{vid}.jar").write_bytes(b"jar")

    def test_inherits_from_is_not_missing(self):
        from ctmd.config import Config
        cfg = Config(self.tmp)
        cfg.ensure_dirs()
        self._make("9.9", {"id": "9.9", "type": "release", "libraries": []})
        self._make("9.9-forge-1.0", {"id": "9.9-forge-1.0", "inheritsFrom": "9.9",
                                     "libraries": []}, jar=False)
        r = inst_mod.verify_instance(cfg, "demo")
        self.assertTrue(r["ok"], r)

    def test_broken_parent_is_reported(self):
        from ctmd.config import Config
        cfg = Config(self.tmp)
        cfg.ensure_dirs()
        self._make("9.9-forge-1.0", {"id": "9.9-forge-1.0", "inheritsFrom": "9.9",
                                     "libraries": []}, jar=False)
        r = inst_mod.verify_instance(cfg, "demo")
        self.assertFalse(r["ok"])
        self.assertTrue(any("父版本" in x for x in r["missing"]))

    def test_detect_loaders_from_versions(self):
        from ctmd.config import Config
        cfg = Config(self.tmp)
        cfg.ensure_dirs()
        self._make("9.9", {"id": "9.9", "libraries": []})
        self._make("9.9-forge-47.1.0", {"id": "9.9-forge-47.1.0", "inheritsFrom": "9.9"}, jar=False)
        self._make("fabric-loader-0.16.0-9.9",
                   {"id": "fabric-loader-0.16.0-9.9", "inheritsFrom": "9.9"}, jar=False)
        found = inst_mod.detect_loaders(self.tmp / "instances" / "demo" / ".minecraft")
        kinds = {x["kind"]: x["loader_version"] for x in found}
        self.assertEqual(kinds.get("Forge"), "47.1.0")
        self.assertEqual(kinds.get("Fabric"), "0.16.0")


class LoaderVersionMathTests(unittest.TestCase):
    def test_neoforge_prefix(self):
        from ctmd import loaders
        self.assertEqual(loaders.neoforge_prefix("26.3"), "26.3.")
        self.assertEqual(loaders.neoforge_prefix("1.21.5"), "21.5.")
        self.assertEqual(loaders.neoforge_prefix("1.20.2"), "20.2.")
        self.assertEqual(loaders.neoforge_prefix("1.21.1"), "21.1.")

    def test_neoforge_sorting(self):
        from ctmd import loaders
        vs = ["26.3.0.9", "26.3.0.95", "26.3.0.10", "26.3.0.0-beta"]
        vs.sort(key=loaders._nf_sort_key, reverse=True)
        self.assertEqual(vs[0], "26.3.0.95")
        self.assertEqual(vs[-1], "26.3.0.0-beta")

    def test_quilt_sorting(self):
        from ctmd import loaders
        vs = ["0.18.1-beta.79", "0.20.0-beta.9", "0.19.0", "0.20.0"]
        vs.sort(key=loaders._quilt_sort_key, reverse=True)
        self.assertEqual(vs[0], "0.20.0")
        self.assertEqual(vs[1], "0.20.0-beta.9")
        self.assertEqual(vs[-1], "0.18.1-beta.79")


class LoaderOutputCheckTests(unittest.TestCase):
    """Forge 安装器可能"退出码 0 但补丁没打上"，装完必须自己核一遍产物。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ctmd-out-"))
        from ctmd.config import Config
        self.cfg = Config(self.tmp)
        self.cfg.ensure_dirs()
        self.vdir = self.tmp / "instances" / "demo" / ".minecraft" / "versions" / "9.9-forge-1.0"
        self.vdir.mkdir(parents=True)

    def tearDown(self):
        fsutil.safe_rmtree(self.tmp)

    def _write(self, sha1_hex: str):
        lib = self.cfg.store("libraries", "net", "minecraftforge", "forge", "x.jar")
        lib.parent.mkdir(parents=True, exist_ok=True)
        lib.write_bytes(b"patched")
        vjson = {"id": "9.9-forge-1.0", "libraries": [{
            "name": "net.minecraftforge:forge:9.9-1.0:client",
            "downloads": {"artifact": {"path": "net/minecraftforge/forge/x.jar",
                                      "sha1": sha1_hex}}}]}
        (self.vdir / "9.9-forge-1.0.json").write_text(json.dumps(vjson), encoding="utf-8")
        return lib

    def test_good_output_passes(self):
        from ctmd import loaders
        lib = self._write("0" * 40)
        good = fsutil.sha1_file(lib)
        self._write(good)
        self.assertEqual(loaders._check_version_libraries(self.cfg, self.vdir,
                                                          "9.9-forge-1.0"), [])

    def test_missing_or_wrong_output_is_reported(self):
        from ctmd import loaders
        self._write("f" * 40)
        problems = loaders._check_version_libraries(self.cfg, self.vdir, "9.9-forge-1.0")
        self.assertTrue(problems and "哈希不符" in problems[0], problems)

        self.cfg.store("libraries", "net", "minecraftforge", "forge", "x.jar").unlink()
        problems = loaders._check_version_libraries(self.cfg, self.vdir, "9.9-forge-1.0")
        self.assertTrue(problems and "缺少" in problems[0], problems)


if __name__ == "__main__":
    unittest.main()
