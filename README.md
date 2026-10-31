# CTMD · Creative Trinity Minecraft Downloader

> 给 PCL2 / HMCL / 官方启动器用的 **Minecraft Java 版原版下载器**。
> 下载游戏本体 → 自动组装成标准 `.minecraft` → 每个实例天然隔离。

名字来自你的服务器 **Creative Trinity（三联创合）**。

---

## 为什么要有它

2026 年各家第三方启动器陆续下架了「下载原版」的功能。
启动器只负责**启动**，CTMD 负责**把游戏文件准备好**，
而且准备出来的结构就是启动器认识的标准结构，拖进去就能用。

---

## 三件事

### ① 版本隔离：每个实例一个 `.minecraft`

```
instances/
├─ 生存服/
│  ├─ ctmd.json                 ← 实例信息
│  ├─ 如何启动.txt
│  └─ .minecraft/
│     ├─ versions/26.3/26.3.json + 26.3.jar   ← 只有这一个版本
│     ├─ saves/  mods/  config/  resourcepacks/   ← 自己独享
│     ├─ assets      → 链接到共享仓库
│     └─ libraries   → 链接到共享仓库
└─ 生电服/
   └─ .minecraft/ …（另一套完全独立的存档和模组）
```

* 存档、模组、按键设置、光影、截图 **完全不串**；
* 体积巨大的 `assets`（约 450–500 MB/版本）和 `libraries` 通过 **目录联接（junction）** 共享，全盘只存一份；
* 版本主文件 `26.3.jar` 用 **硬链接**，实例里看到的是真文件，但磁盘上只有一份；
* 实例可以整个搬走（用「复制模式」安装，或直接 copy 后把两个链接目录换成实体目录）。

### ② 双下载源 + 自动测速 + 失败换源

| 源 | 地址 | 说明 |
|---|---|---|
| Mojang 官方源 | `piston-meta.mojang.com` 等 | 原汁原味，海外线路快 |
| BMCLAPI 镜像 | `bmclapi2.bangbang93.com` | 国内镜像，自动改写 maven / assets 路径 |

* **自动**：启动时测速，快的排前面；
* 单个文件失败会自动换下一个源重试（最多 3 次），全部失败才报错；
* 断点续传友好：重跑会跳过已存在且 SHA1 正确的文件，只补缺的。

> 实测（本机，2026-10）：官方源 4 MB/s 左右，BMCLAPI 在这条线路上只有 0.1 MB/s，
> 所以默认「自动」会把官方排前面。你在国内网络下如果官方慢，手动切镜像即可。

### ③ 兼容 PCL2 / HMCL / 官方启动器

产物是**标准 `.minecraft` 目录**：

* PCL2：设置 → 启动器 → 游戏目录 → 添加 → 选中实例的 `.minecraft`
* HMCL：设置 → 游戏目录 → 添加游戏目录 → 同上
* 官方启动器：已生成 `launcher_profiles.json`，登录正版账号后可在「安装」里看到 `CTMD · 实例名`
* 启动器需要的 `versions/<id>/<id>.json`、`<id>.jar`、`assets/indexes`、`libraries` 一应俱全

另外会顺手生成 `如何启动.txt`，把路径和步骤写在实例里。

### ④ 接手已经玩很久的 PCL 目录

PCL2 开「版本隔离」时，每个版本的 `gameDir` 就是 `.minecraft\versions\<id>\``——
这跟 CTMD 的实例 `.minecraft\` 是**同一个东西**，所以能一一对应地搬过来：

```bat
python ctmd.py import E:/Minecraft/PCL --dry-run      # 先看会导哪些
python ctmd.py import E:/Minecraft/PCL --mode copy    # 正式导（copy 最安全）
```

导入做的事：

1. `versions\<id>\` 里的存档、模组、配置、`options.txt` **上提一层**到实例根；
2. `<id>.json` / `<id>.jar` 归位到 `.minecraft\versions\<id>\`；
3. `assets` / `libraries` 默认用 junction 指回原目录（**0 复制、0 重下**），
   也可用 `--share store` 收编进共享仓库；
4. 写 `ctmd.json`（`source: PCL 导入`）和启动器档案。

`--mode` 三档：`copy`（复制一份，安全）/ `link`（同盘硬链接，0 空间但与源共享数据）/
`move`（搬过来，源目录的数据会被移走）。

导入后可以 `ctmd.py repair` 补齐老目录常见的缺口（`log_configs`、旧版音效等）。

---

## 模组加载器（Fabric / Quilt / Forge / NeoForge）

装好原版之后，可以在同一个实例里再装加载器——**不会新建实例**，
而是在实例的 `.minecraft/versions/` 里多出一个版本，启动器里会并排出现：

```
26.3                          原版
26.3-forge-66.0.13            Forge
neoforge-26.3.0.95            NeoForge
fabric-loader-0.19.5-26.3     Fabric
quilt-loader-0.30.1-26.3      Quilt
```

模组放实例的 `mods/` 里就行，隔离照样成立。

| | Fabric / Quilt | Forge / NeoForge |
|---|---|---|
| 原理 | 走 meta API 拿 profile JSON（`inheritsFrom` 继承原版） | 跑官方安装器，给客户端打二进制补丁 |
| 耗时 | 几秒（7–12 个依赖库） | 2–5 分钟首次，之后靠缓存 1 分钟内 |
| 源 | maven.fabricmc.net / maven.quiltmc.org | 官方 maven，国内走 BMCLAPI（安装器 `--mirror`） |
| 产物 | `<loader>-loader-<版本>-<mc>` | `<mc>-forge-<版本>` / `neoforge-<版本>` |
| 支持范围 | Fabric/Quilt 基本每个版本都有 | Forge 老到 1.1；NeoForge 从 1.20.2 开始 |

```bat
:: 看有哪些版本可选
python ctmd.py loader list fabric   --mc 26.3
python ctmd.py loader list quilt    --mc 26.3
python ctmd.py loader list forge    --mc 1.20.1
python ctmd.py loader list neoforge --mc 26.3

:: 装进已有实例（默认挑稳定版 / 推荐版 / 最新版）
python ctmd.py loader install fabric   --instance 26.3
python ctmd.py loader install quilt    --instance 26.3
python ctmd.py loader install forge    --instance 26.3
python ctmd.py loader install neoforge --instance 26.3

:: 指定版本、指定 Java（安装器用）
python ctmd.py loader install forge --instance 26.3 --loader 47.4.28 --java "C:\Program Files\Java\jdk-17\bin\java.exe"
```

图形界面里有第三个标签页「模组加载器」：选实例 → 选加载器 → 选版本 → 安装，
安装器的输出会实时滚在下面的日志区（打补丁的上万行会折叠成计数）。

> - 装完都会跑一遍**产物校验**（按 version JSON 逐个核对文件与 SHA1），
>   安装器"退出码 0 但补丁没打上"这种情况会被拦下来。
> - Forge/NeoForge 安装器下载的库直接落在共享仓库 `store/libraries`，同版本第二次装基本不下载；
>   安装器本体缓存在 `store/cache/installers/`。

## 用法

### 图形界面

```bat
python ctmd.py
```

或者双击 `启动 CTMD.bat`（无控制台窗口）。

界面分两页：

* **安装新版本** —— 左侧版本表（正式版 / 快照 / 远古，可搜索），右侧填实例名、勾选项，一键下载；
* **已装实例** —— 看占用、打开文件夹、复制路径、校验文件、删除实例，
  **以及「导入 PCL 目录」**（见下方 ④）。

### 打包成 exe

```bat
python build.py            # 单文件、无控制台窗口 → dist\CTMD.exe
python build.py --console  # 带控制台，能看到命令行输出（排错用）
```

产物是**单个 exe（约 11 MB）**，双击即用。第一次启动会解压到临时目录，
需要几秒，之后快一些。

几个要点：

* `config.json`、`store\`、`instances\` 都建在 **exe 所在目录**，
  所以别把 exe 放进 `Program Files`（没写权限），放 `D:\CTMD\` 这类地方就行；
* 图标 `assets\ctmd.ico` 由 `python tools/make_icon.py` 生成（纯标准库，不需要 Pillow），
  改完图标重跑这个脚本再打包；
* exe 是 GUI 版，命令行子命令（`install` / `verify` 等）在 exe 里没有输出，
  要用命令行请跑 `python ctmd.py ...`。

### 命令行

```bat
python ctmd.py list --type release --limit 20      # 列版本
python ctmd.py list --type snapshot --search 26.4  # 找快照
python ctmd.py install 26.3 --name 生存服           # 下载并建实例
python ctmd.py install 1.21.5 --source bmclapi --workers 12
python ctmd.py install 1.5.2 --no-assets            # 不要声音资源
python ctmd.py import E:/Minecraft/PCL                  # 导入整个 PCL 目录（每个版本建一个实例）
python ctmd.py import E:/Minecraft/PCL --only "1.20.1-Forge_47.3.0" --mode copy
python ctmd.py repair                               # 补齐所有实例缺的文件
python ctmd.py repair 生存服 --dry-run                # 只看要补什么
python ctmd.py instances                            # 已装实例
python ctmd.py verify 生存服                         # 校验完整性
python ctmd.py remove 生存服                         # 删实例（共享资源不动）
python ctmd.py clean                                # 清理没实例引用的版本本体
python ctmd.py doctor                               # 测速 + Java 体检
```

常用参数：

| 参数 | 作用 |
|---|---|
| `--name` | 实例名（默认用版本号） |
| `--source auto\|official\|bmclapi` | 下载源 |
| `--workers N` | 并发数，默认 8（实测 6–12 最划算） |
| `--no-assets` | 不下载资源文件（能进游戏，但没声音/没语言文件） |
| `--copy` | 复制资源而不是链接（占空间，但实例可整体搬走） |

---

## 实测记录（2026-10-21，本机）

| 版本 | 文件数 | 体积 | 结果 |
|---|---|---|---|
| 26.3（最新正式版） | 5224 | 505 MB | ✓ 校验通过 |
| 1.21.5 | 4285 | 505 MB | ✓ 校验通过 |
| 1.8.9 | 758 | 131 MB | ✓ 校验通过（GUI 全流程） |
| 1.5.2（上古版本） | 479 | 55 MB | ✓ 校验通过（含 legacy 资源铺平） |
| 26.3 + Forge 66.0.13 | 40 个库 | — | ✓ 安装器跑完 + 产物校验，重打补丁也一致 |
| 26.3 + Fabric 0.19.5 | 7 个库 | — | ✓ 秒装 |
| 26.3 + Quilt 0.30.1 | 9 个库 | — | ✓ 装完校验通过 |
| 26.3 + NeoForge | — | — | ⚠ 版本列表可用；本机网络连不上 maven.neoforged.net，BMCLAPI 又限流，未能完成安装验证 |

> Forge 实测耗时：首次 179 秒（下载 + 1388 个类打补丁），
> 删掉补丁产物重打 53 秒，命中缓存 5 秒。

PCL2（正式版 2.99.0.0）把实例的 `.minecraft` 添加为游戏目录后，直接识出版本，无需任何额外处理：
第一次用 1.8.9 验证；26.3 那台实例里同时识别出原版 `26.3`、`26.3-forge-66.0.13`、
`fabric-loader-0.19.5-26.3`（PCL.ini 里两条卡片、三条版本记录）。

卸载验证：删除实例后共享仓库 `store/assets`、`store/libraries` 完好无损
（这条路径踩过坑，已写进单元测试）。

```bat
python -m unittest discover -s tests -v     # 11 个用例
```

## 目录说明

```
D:\ctmd\
├─ ctmd.py                 入口
├─ ctmd\                   源码
│  ├─ config.py            配置（config.json）
│  ├─ sources.py           官方源 / 镜像源 / 测速 / 地址改写
│  ├─ manifest.py          版本清单、规则匹配、任务规划
│  ├─ net.py               HTTP 长连接池、流式下载、重试换源
│  ├─ downloader.py        并发调度、进度、跳过已完成
│  ├─ instance.py          实例组装、junction、校验、安全删除
│  ├─ java.py              Java 检测
│  ├─ loaders.py           Fabric / Forge 安装
│  ├─ service.py           流程编排（CLI / GUI 共用）
│  ├─ cli.py  gui.py       两个界面
├─ store\                  共享仓库（别手动删）
│  ├─ versions\<id>\<id>.jar
│  ├─ libraries\
│  ├─ assets\{indexes,objects,log_configs,virtual}
│  └─ cache\               版本清单缓存
├─ instances\              你的实例
└─ config.json
```

---

## 技术细节（给好奇的你）

* **规则匹配**：完整实现 Mojang 的 `rules`（os.name / os.arch / os.version 正则 / features），
  只下 Windows 真正需要的库；老版本（≤1.5.2）的 `natives` 分类器、纯 maven 坐标库也都照顾到了。
* **老版本资源**：`pre-1.6` 这类索引会额外铺出 `assets/virtual/legacy/…` 和 `resources/…`，
  1.5.2、1.2.5 这些上古版本也能正常出声。
* **校验**：每个文件按清单里的 `sha1` + `size` 双重校验，先写 `.part` 再原子改名，不会留下半截文件。
* **安全删除**：删实例时识别 reparse point，只摘链接、绝不顺着 junction 删进共享仓库。
* **长路径**：Windows 下自动加 `\\?\` 前缀，深目录（`net/minecraftforge/...`）不会炸。
* **零依赖**：只用 Python 标准库（tkinter + http.client），不需要 pip install。

---

## 常见问题

**Q：下载到一半断了怎么办？**
重跑同一条命令。已完成的文件会跳过，只补缺的。

**Q：提示「没找到 Java 21」？**
CTMD 只负责游戏文件，Java 由启动器管理。PCL2 / HMCL 里都能装 Java；
本机已检测到 Java 26 / 23 / 17 / 8，够用。

**Q：实例里的 assets 和 libraries 是链接，删了会怎样？**
链接本身可以删，共享仓库别删。用界面里的「删除实例」最安全。

**Q：能不能下 Forge / Fabric？**
当前专注原版。装模组加载器可以先用启动器装（它们通常还能联网），或后续版本再加。

**Q：磁盘占用？**
每个版本本体约 20–40 MB + 依赖库约 100 MB（共享）+ 资源约 450 MB（共享）。
第二个实例起，只多几十 MB。删掉实例后跑一次 `python ctmd.py clean`，
没人用的版本本体也会被回收。

---

## 版本

* v0.1.0 — 原版下载、实例隔离、双源、PCL2/HMCL 兼容、GUI + CLI、单元测试
