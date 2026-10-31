# CTMD × 老 PCL 目录 兼容性评估

> 评估对象：`D:\Minecraft\PCL\.minecraft`（19 GB，13 个版本，55 个存档世界）
> 评估时间：2026-10-31　评估人：玛莉嘉
> 实测环境：`E:\Laboratory\PCL`（完整副本，9.1T 盘）+ 沙盒 `E:\Laboratory\ctmd-sandbox`
>
> **结论：已经做到「完美兼容」。** CTMD 新增 `import` 命令，把 PCL 版本隔离目录
> 一键转成实例，13/13 全部导入成功、加载器识别全对、存档一个不丢。
> 下面是为什么「直接指过去」不行，以及改造后的对应关系（历史分析，仍有参考价值）。

---

## 0. 先纠正一个误会

`D:\ctmd\PCL` 里**没有游戏数据**。那是 10 月 20 日做 PCL2 兼容性测试时留下的启动器壳子：

```
D:\ctmd\PCL\
  ├─ libwebp.dll
  ├─ Log1..Log5.txt      ← Log1-3 是 2026-10-31 16:46 新生成的
  ├─ Musics\  Pictures\   （空）
  └─ Setup.ini           LaunchFolderSelect:C:\Users\<用户名>\AppData\Roaming\.minecraft\
```

真正的老巢在原地：

```
D:\Minecraft\PCL\
  ├─ plain craft launcher 2.exe
  ├─ PCL\Setup.ini        LaunchFolderSelect:$.minecraft\   ← "$" = 跟着启动器走
  └─ .minecraft\          19 GB  ← 全部在这
```

全盘扫描确认，没有第二份 19 GB 的副本（`du` 结果：`/d/ctmd` 共 714 MB）。

---

## 1. 你的家底（只读盘点）

| 版本目录 | 类型 | Java | Mods | 世界 | 体积 |
|---|---|---|---|---|---|
| `1.1` | 原版 | 8 | 0 | 1 | 72 MB |
| `1.12.2-Forge_14.23.5.2859-OptiFine_G5` | Forge+OptiFine | 8 | 1 | 1 | 94 MB |
| `1.18.2-Forge_40.2.1-OptiFine_H9_pre3` | Forge+OptiFine | 17 | 6 | 7 | 2.8 GB |
| `1.19.2-Fabric 0.14.9-OptiFine_H9` | Fabric+OptiFine | 17 | 13 | 15 | 2.1 GB |
| `1.19.3-OptiFine_I2_pre5` | 原版+OptiFine | 17 | 0 | 1 | 353 MB |
| `1.19.4-Fabric 0.14.19-OptiFine_I4` | Fabric+OptiFine | 17 | 7 | 5 | 477 MB |
| `1.20.1-Fabric 0.14.23-OptiFine_I5` | Fabric+OptiFine | 17 | 5 | 8 | 1.5 GB |
| `1.20.1-Forge_47.1.0-OptiFine_I5` | Forge+OptiFine | 17 | 15 | 1 | 190 MB |
| `1.20.1-Forge_47.3.0-OptiFine_I6` | Forge+OptiFine | 17 | 12 | 0 | 74 MB |
| `1.20.4-Fabric 0.15.10-OptiFine_I7` | Fabric+OptiFine | 17 | 11 | 12 | 4.0 GB |
| `1.21.4-Fabric 0.16.14` | Fabric | 21 | 6 | 0 | 82 MB |
| `1.21.6-Fabric 0.16.14` | Fabric | 21 | 1 | 0 | 30 MB |
| `26.2回坑首端` | Fabric | 25 | 12 | 4 | 4.6 GB |

**合计 13 个版本 / 55 个世界 / 16.0 GB**（另 1.9 GB assets + 699 MB libraries）

（各世界的具体名称从略，涉及个人存档信息不公开。）

---

## 2. 现在为什么不行：两个模型的前提撞了

|  | PCL（你这个目录） | CTMD |
|---|---|---|
| 「世界单元」 | 1 个 `.minecraft` + **13 个版本** | 1 个实例 = **恰 1 个** `.minecraft` + 1 个版本 |
| 隔离方式 | **版本隔离**：mods/saves/config 在 `versions\<id>\` 里 | **实例隔离**：mods/saves 在 `.minecraft\` 根 |
| assets / libraries | 实体共享在根下（13882 + 629 个文件） | junction → `store\`（5149 + 141 个文件） |
| 版本 JSON | **合并版**，继承链已展开，无 `inheritsFrom` | 靠 `inheritsFrom` 认父版本 / 加载器 |
| 资源索引 id | `2, 3, 5, 12, 19, 26, 32`（PCL 自己生成） | 按 Mojang 清单 id 取 |

### 六个具体卡点（实测，不是推测）

**① 存档会「集体消失」**
`.minecraft\saves` 实测 **0 字节**。55 个世界全在 `versions\<id>\saves\`（≈13.3 GB）。
CTMD 只看 `.minecraft\saves` → 会告诉你「这个实例没有存档」。

**② `ctmd verify` 会误报几百条缺失**
校验目标一律算在 `store\` 里。拿 `26.2回坑首端` 试算：计划的 98 个文件里，store 只有 36 个存在 → **62 条「缺失」**。13 个版本加起来数百条。文件其实一个没少，全在你目录里。

```
libraries\com\mojang\authlib\9.0.75\authlib-9.0.75.jar   store=False  pcl=True
```

**③ 认不出任何加载器**
`detect_loaders(D:\Minecraft\PCL\.minecraft)` → `[]`
原因：PCL 把 Forge/Fabric/OptiFine 合并进单个 JSON，**13 个版本全部没有 `inheritsFrom`**，而 CTMD 正是靠这个字段识别的。你家 12 个模组版本会全部显示「无加载器」。

**④ 一个版本号都说不清**
GUI 的「版本」列取 `list_versions()[0]` → 显示 `1.1`（2022 年那个远古测试档）。实例面板的「版本数」会是 13，但 UI 是按 1 个设计的。

**⑤ 「删除实例」会连锅端**
`delete_instance` = 整个实例目录 `safe_rmtree`。在这个目录上按一下 → 19 GB + 55 个世界一起没，而它以为那只是「一个版本」。

**⑥ 同名覆盖风险**
CTMD 往同名实例装版本时，会重写 `versions\<id>\<id>.json` 和 jar —— 正是你玩了好久的那些文件。

> 附带发现（无害但要知道）：`.minecraft\.minecraft\` 有个 2023-01-20 的残留（15 KB，只有 options/saves/stats），还有 `.fabric`(25 MB)、`.optifine`、`kingsoft` 这些历史遗留目录。

---

## 3. 为什么修得好：两个模型是**天生一对**

PCL 开版本隔离时，`gameDir` 就是 `versions\<id>\`。
CTMD 的实例，`gameDir` 就是 `instances\<名>\.minecraft\`。

```
PCL:   ...\versions\26.2回坑首端\        ← 这就是一个 gameDir
CTMD:  ...\instances\26.2回坑首端\.minecraft\   ← 这也是一个 gameDir
```

**一一对应，1:1，一个不多一个不少。** 所以问题不是「兼容性」，是「缺一个导入器」。

「完美兼容」分三档，可以分开要：

| 档位 | 含义 | 现状 |
|---|---|---|
| L1 能启动 | 启动器认得这些版本 | ✅ 已经满足（PCL 本来就认得，跟 CTMD 无关） |
| L2 能管理 | CTMD 能正确列出、校验、装加载器、**不误删** | ❌ 需要改造 |
| L3 能共享 | 资源去重、各版本存档不串 | ❌ 需要改造 |

---

## 4. 三条路（等你定）

### 方案 A：无损搬迁 —— 13 个版本各变一个 CTMD 实例

```
D:\ctmd\instances\26.2回坑首端\.minecraft\
  ├─ versions\26.2回坑首端\{26.2回坑首端.json, .jar}   ← 从 PCL\versions 移过来
  ├─ mods\ saves\ config\ resourcepacks\ …            ← 从 PCL\versions\26.2回坑首端\ 移过来（同盘 move，秒级）
  └─ assets\ libraries\                               ← junction 指回 D:\Minecraft\PCL\.minecraft\ 的原件（0 字节）
```

- **成本**：同盘 rename，几乎零耗时、零新空间
- **风险**：动文件；所以要先把目录清单导出成快照（已完成：`docs\pcl_inventory.json`）
- **代价**：PCL 之后要在「游戏目录」里添加 `instances\<名>\.minecraft` 才看得到这些版本（或者干脆不用 PCL）
- **收益**：L2 + L3 全满足，19 GB 一份不重复

### 方案 B：零搬迁挂载 —— CTMD 只登记，不碰文件

- CTMD 增加「挂载外部 .minecraft」：只记路径；校验改成查本地文件；删除只摘登记
- **成本**：改 `instance.py`（list/verify/delete）+ GUI 加「外部」标签
- **风险**：几乎为零
- **代价**：13 个版本仍挤在一个实例里，CTMD 只能当「体检工具」；装加载器、删除必须禁用
- **收益**：L2（部分）

### 方案 C：只收编资源 —— 省下载

- 把 PCL 的 `libraries` / `assets` 硬链接进 `store\`（0 字节），以后 CTMD 新建实例不再重复下
- **成本**：改 1 个函数；一次性跑 2 万个硬链接，几十秒到几分钟
- **风险**：低（硬链接 = 同一份数据多个名字，删任何一边都不影响另一边）
- **代价**：老目录完全不接管
- **收益**：L3（部分）

> 推荐组合：**C 打底 + A 接管**。先硬链接收编资源（安全、可验证），再搬迁实例（有了 store 兜底，assets/libraries 直接 junction 到 store，连指回 PCL 都不用了）。

---

## 5. 开工前会做的事（预演，现在一步没执行）

1. 导出目录清单快照 → 已有 `docs\pcl_inventory.json`（13 版本 / 55 世界 / 每个的大小）
2. 逐版本 `os.rename` 搬迁（同盘，非复制；失败可原路退回）
3. 搬完立即 `ctmd verify`，要求每个实例「✓ 完整」
4. 抽 2 个版本用 PCL2 实际启动一次，确认进得去、存档在
5. 全部通过之前，`D:\Minecraft\PCL\.minecraft` 的原始结构保留不动

---

## 6. 若动手，CTMD 要改的地方

| 文件 | 改动 |
|---|---|
| `ctmd/instance.py` | `list_versions` 兼容目录名≠json id；`detect_loaders` 增加「合并版」识别（按 mainClass / 库名反推 Forge/Fabric）；新增 `import_pcl()` |
| `ctmd/instance.py` | `verify_instance` 支持「本地实体」模式（assets/libraries 不在 store 时按实例内路径校验） |
| `ctmd/config.py` | 可选：`external_instances` 注册表（方案 B 用） |
| `ctmd/gui.py` | 实例表加「来源」列（CTMD / 外部 / PCL）；删除外部实例时二次确认且只摘登记 |
| `tests/test_core.py` | 加回归：合并版 JSON 的加载器识别、外部实例删除不碰文件 |

---

*这份文档是只读体检的产物。等你决定方向，我随时开工。*

---

## 7. 实测结果（2026-10-31，E 盘沙盒）

**环境**：`E:\Laboratory\PCL`（完整副本）+ 沙盒 `E:\Laboratory\ctmd-sandbox`
（D 盘原件 `D:\Minecraft\PCL` 全程只读，未动一个字节）

### 7.1 导入

```bat
python ctmd.py --root E:/Laboratory/ctmd-sandbox import E:/Laboratory/PCL --mode link --share local
```

| 项目 | 结果 |
|---|---|
| 版本 | **13/13 全部导入** |
| 体积 | 16.0 GB |
| 耗时 | 1 分 55 秒（硬链接，0 额外空间） |
| 存档 | 55 个世界全部到位，`options.txt` / `servers.dat` / 配置一并上提 |
| 额外空间占用 | **0 GB**（`--share local` 让 assets/libraries 指回原目录） |

### 7.2 加载器识别（改造前全部是「无加载器」）

| 版本 | CTMD 识别 |
|---|---|
| `1.1` | 原版 |
| `1.12.2-Forge_14.23.5.2859-OptiFine_G5` | Forge 14.23.5.2859 |
| `1.18.2-Forge_40.2.1-OptiFine_H9_pre3` | Forge 40.2.1 |
| `1.19.2-Fabric 0.14.9-OptiFine_H9` | Fabric 0.14.9 |
| `1.19.3-OptiFine_I2_pre5` | OptiFine HD_U_I2_pre5 |
| `1.19.4-Fabric 0.14.19-OptiFine_I4` | Fabric 0.14.19 |
| `1.20.1-Fabric 0.14.23-OptiFine_I5` | Fabric 0.14.23 |
| `1.20.1-Forge_47.1.0-OptiFine_I5` | Forge 47.1.0 |
| `1.20.1-Forge_47.3.0-OptiFine_I6` | Forge 47.3.0 |
| `1.20.4-Fabric 0.15.10-OptiFine_I7` | Fabric 0.15.10 |
| `1.21.4-Fabric 0.16.14` | Fabric 0.16.14 |
| `1.21.6-Fabric 0.16.14` | Fabric 0.16.14 |
| `26.2回坑首端` | Fabric 0.19.5 |

### 7.3 校验与修补

首次校验 13 个实例**全部不完整**，但根因高度集中：

- 12 个版本各缺 1 个 `assets/log_configs/*.xml`（PCL 目录里根本没有这个目录）
- `1.1` 缺 48 个 `newsound` 旧音效
- `1.12.2` 缺 1 个 `lwjgl-platform` natives
- `1.20.1-Forge_47.3.0`、`1.21.4` 各缺 1 个日志配置

`ctmd.py repair` 补齐 **51 个文件（几百 KB）**，复检 **13/13 全部 ✓ 完整**。

### 7.4 PCL2 实际识别（铁证）

把 `E:\Laboratory\PCL\PCL\Setup.ini` 的 `LaunchFolderSelect` 指向
`instances/26.2回坑首端/.minecraft`（正斜杠）后启动 PCL2，日志原文：

```
17:59:08.788 当前选择的 Minecraft 文件夹：E:\Laboratory\ctmd-sandbox\instances\26.2回坑首端\.minecraft\
17:59:09.088 当前选择的 Minecraft 版本：...\.minecraft\versions\26.2回坑首端\
17:59:09.125 [PageLaunchLeft] 启动按钮：Minecraft 版本：...\versions\26.2回坑首端\
17:59:22.104 [ModMinecraft] 版本隔离初始化（26.2回坑首端）：State Fabric，IsRelease True，Modable True
```

PCL2 认出了目录、认出了版本、还判断出它是 Fabric 且「可装模组」。
测试后 `Setup.ini` 已还原为 `LaunchFolderSelect:$.minecraft\`。

### 7.5 回归测试

```
python -m unittest discover -s tests   →  25 tests OK
```

新增 3 个：导入结构、合并版加载器识别、校验走实例内路径。

### 7.6 落地到 D 盘原件时的建议

```bat
:: 方案 A：整个接管（推荐，先备份目录清单）
python ctmd.py import D:/Minecraft/PCL --mode copy --share store

:: 方案 B：只在 E 盘这份实验（现在这个状态）
python ctmd.py --root E:/Laboratory/ctmd-sandbox import E:/Laboratory/PCL --mode link
```

- 真要接管 D 盘原件，用 `--mode copy`（安全）或 `--mode move`（省空间但源目录数据会移走）；
- `--share store` 会先把 assets/libraries 收编进 `store\`（跨盘只能复制，约 2.6 GB），之后所有实例共享；
- 无论如何，**先跑 `--dry-run` 看清会动哪些版本**。

### 7.7 一个会被冤枉的「损坏」：被启动器重写过的资源索引

第二次全量校验时，`1.20.1-Fabric` 和 `1.20.1-Forge_47.1.0` 各报 1 个「损坏」：

```
5.json（大小不符）
```

查下去发现是 **PCL2 自己改的**：它的「更新语言文件」功能把 `assets/indexes/5.json`
从官方声明的 411581 字节重写成 411680 字节，连带换掉了 **129 个 `minecraft/lang/*.json`**
的 hash。索引指向的 3584 个对象本地**一个不缺**——游戏完全正常。

这类「索引与官方声明不同，但内容可用」的情况，`verify` 现在不会报损坏，
而是给一条提示：

```
检查 3639 个文件…
  提示：5.json 与官方声明不同（本地启动器更新过索引），内容可用
✓ 完整，没有发现问题。
```

判定逻辑在 `instance._index_usable()`：解析索引 → 逐个检查 `assets/objects/<xx>/<hash>`
是否存在。齐全就算可用。只有真的缺对象时才报损坏。
