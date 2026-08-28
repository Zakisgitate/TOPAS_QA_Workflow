# PLAN1699 项目优化报告

> 生成时间：2026-08-21，最近更新：2026-08-23（Asia/Shanghai）
> 分析对象：`/Users/jiangzhenmin/Desktop/PLAN1699_副本`
> 用途：逐项对照修复的工作清单。每项含「证据 / 位置 / 修复方案 / 验收标准 / 工作量」。
> 性质说明：本报告只涉及**软件工程与计算效率**，不构成任何物理 commissioning 或临床验收结论。
>
> 2026-08-22 变更：新增 §1.4（2026-08-21 的运行是逐字节重复计算）与 §1.5（工作目录说明）；
> §1.3 的文档漂移已修复并标注；优化项 11 更新为「内容已同步、根因未解决」；
> **优化项 1（线程上限）已实施并勾选**，测试数由 13 增至 19。
>
> 2026-08-23 变更：网页刷新不再影响运行中的计算（自动重新挂接 + 运行期冻结输运参数）；
> 优化项 2 中「total_histories 必须 ≥ spot 数」的 BLOCK 已改为**警告 + 稀疏试跑**；
> 批处理队列的失败原因不再只显示 exit code。测试数增至 **39**。
>
> 2026-08-23 追加：修复 case 目录下空 `gui/` 目录遮蔽真实 `gui` 包，导致
> `14_calibrate_mc_dose.py` 在输运完成后才 `ModuleNotFoundError` 的缺陷（属优化项 3 的一个实例）；
> 批处理队列现在在输运后继续执行 profile 导出与 Gamma。测试数增至 **49**。
>
> 2026-08-23 再追加：**优化项 4（case 目录守卫）已完成并勾选**，但判据与原方案不同——
> 原文规则会拒绝当前全部 5 个在用 case，改为按「能否安全充当 case root」判断，详见正文。
> `case_identity()` 的 anonymous 回落现在区分读/写。测试数增至 **63**。

---

## 0. 如何使用本报告

- 每个条目前的 `- [ ]` 是进度勾选框，修完改成 `- [x]`。
- 优先级：`P0` = 收益最大或有正确性风险，`P1` = 明确缺陷，`P2` = 效率/可维护性，`P3` = 卫生与流程。
- 「证据」一栏全部来自本机磁盘实测，可以自行复现。
- 建议先做第 8 节的「三件事」，再按编号推进。

---

## 1. 项目现状核对（截至 2026-08-21）

### 1.1 项目做什么

把 TPS 导出的真实碳离子 PBS DICOM 计划在 TOPAS 中重建，用机器绑定的 `N_plan/N_sim × C_machine` 独立粒子数标定得到物理剂量，再与 TPS 物理剂量对比。**TPS 剂量不参与 MC 输出的拟合**，这是本项目相对早期版本最关键的方法学改进。

活动病例：

| 项目 | 值 |
|---|---|
| PatientID | `20240813005` |
| RTPlanLabel | `hzroom1-h-rf4-COM-250916` |
| 机器 | `hzRoom1_90_RF4_250701` |
| 粒子 / 模式 | Carbon-12 / PBS `MODULATED` |
| 能量层 / spot | 48 / 43,919 |
| 能量范围 | 203.67 – 379.73 MeV/u |
| CT | 512×512×199，约 1.171875×1.171875×1.5 mm |
| 剂量网格 [Z,Y,X] | 152×154×185，2 mm 各向同性，4,330,480 个 float64 |
| MC binary 大小 | 34,643,840 bytes |

### 1.2 已完成的闭环

```
DICOM 身份/引用/几何安全门
  → RT Ion Plan 解析（能量层 / spot / FWHM / meterset）
  → CT 几何 + TPS 同网格 DoseToMedium scorer
  → commissioned 束流模型（实测 IDD NNLS 离散谱 + Fermi-Eyges 相空间 + VSAD 投影 + NF(E)）
  → TOPAS 全计划输运
  → N_plan/N_sim 粒子数标定审计
  → 三方向 profile / 任意 line dose / 3D Gamma / MC DICOM RTDOSE
  → 每患者/计划/运行的 manifest 缓存
```

外加：Web GUI、持久批处理队列、机器模型包 registry（不可变导入 + SHA-256 门禁）、SSH 远程 bundle 接口。

### 1.3 与文档不一致的实际状态（重要）

| 原文档说法 | 磁盘实际 |
|---|---|
| `CLAUDE_PROJECT_HANDOFF.md` §0：「仍有 TOPAS 在运行，不要重启 GUI」 | 队列 job `418087a0f0ad` 状态 **completed**（13:19:55 → 16:32:44，11,569 s），**当前无 TOPAS 进程**，GUI 也未运行 |
| `PROJECT_STATUS.md:17`：`Threads: 12` | 实际完成的 job 记录为 `threads: 64` |
| `PROJECT_STATUS.md:19`：当前 production 是那次 8,166.35 s 的运行 | 已被 11,541 s 的新 commissioned 运行取代 |
| `CLAUDE_PROJECT_HANDOFF.md` §2：「结束后需重跑 profiles / Gamma / MC RTDOSE」 | **不需要**，见 §1.4 |
| `WORKFLOW.md:116`：估时基准为水模 150,000 histories | 当前病例已有实测，Quick 档实测 2.27 h 而非表中的 1.3 h |

> **已于 2026-08-22 修复。** `PROJECT_STATUS.md`、`CLAUDE_PROJECT_HANDOFF.md`、`README.md`、
> `WORKFLOW.md` 均已按磁盘实际状态同步。上表保留作为记录。
> 但**文档需要手工同步这一根因仍未解决** —— 见优化项 11。

### 1.4 关键发现：2026-08-21 的运行是一次逐字节的重复计算

当前 production binary 与
`.../run-full_plan_100000_commissioned/topas_runs/archived-20260821T131348_028242/dose/RTDOSE_00003_DoseToMedium_TPSGrid.bin`
的 **SHA-256 完全相同**：

```
46cc0286045b54c9ce88ba28cd87cb7b6194a6587cf4846e0ac912c656b0b8bb
```

后者来自 2026-08-20 的运行（日志 `run_full_plan_qa_20260820_152030.log`，Real 17,547.6 s）。
两次运行 seed 都是 1699、线程数都是 64；Geant4 MT 在这两者固定时可复现，所以输出逐字节一致。

**三个推论**：

1. 缓存中的 Gamma（08-21 01:06）、profiles（01:14）、MC RTDOSE（01:07）虽然时间戳早于最后一次运行完成，
   但它们是针对逐字节相同的 binary 计算的，**对当前 production binary 有效，不需要重跑**。
   交叉验证：Gamma metrics 记录的 `MC_TOPAS_per_run_max_Gy = 4.941871885e-05` 与当前 binary 的实际最大值精确相符。
2. 2026-08-21 13:20–16:32 那 3.2 h **没有产生任何新信息**，是一次重复计算。
3. 「多随机种子统计」这条待办因此更紧迫：**重复运行相同配置不构成独立样本**，必须更换 seed。
   反过来说，这也是一次意外获得的可复现性验证。

另注：`archived-20260820T151958_596587` 与 `archived-20260821T132000_154803` 两个归档目录下的 `.bin`
均为 **0 字节**，是未完成/被取消运行的审计留痕，不是可用剂量。

### 1.5 工作目录说明

本次分析在 `/Users/jiangzhenmin/Desktop/PLAN1699_副本` 中进行，原始目录
`/Users/jiangzhenmin/Desktop/PLAN1699` 仍存在。

- **源码零硬编码路径**：`scripts/`、`gui/`、`tests/` 中没有任何 `/Desktop/PLAN1699` 字面量，
  一律用 `Path(__file__).resolve().parents[1]` 推导 root，所以副本可以直接运行。
- **83 个审计产物记录了原始绝对路径**（manifest、summary、calibration JSON）。那是溯源信息而非配置，
  **不要批量改写**。
- 副本中的标定门禁正常：`allocation.st_mtime_ns < mc_binary.st_mtime_ns`，
  `require_particle_calibration()` 不会 BLOCK。

### 1.6 测试与结果

- `python -m unittest discover -s tests` → **134 tests OK**（2026-08-25；13 → 19 线程上限 → 24 刷新安全 → 39 稀疏试跑 → 49 队列流水线与包遮蔽 → 63 case 目录与身份守卫 → 134 水模验证物理分析层）。
- Gamma（3%/3 mm，全局，10% TPS 阈值）：**99.9640%**，477,581 / 477,753 voxel。
- 高剂量区（TPS ≥ 50% max）calibrated MC/TPS 中位比：**0.9999317**。

### 1.7 真正的未完成项

不是软件按钮，而是物理 commissioning：机构 CT HU–material 标定、MRF4 几何与残余散射/碎裂、绝对输出可追溯性、多随机种子统计、独立端到端验收。项目文档对此描述准确且诚实，本报告不重复。

---

## 2. P0 级问题

### - [x] 优化项 1：线程数设置正在拖慢计算 1.4–2.1 倍（已修复 2026-08-22）

**证据（同一 43,919-spot / 100,000-history 全计划，本机 `hw.logicalcpu = 15`，24 GB）**

| 请求线程 | Real | User | **Sys** | 日志文件 |
|---|---|---|---|---|
| **4** | **8,166 s** | 15,754 s | **69 s** | `topas_output/production/run_full_plan_qa_20260818_130551.log` |
| 64 | 11,541 s | 19,999 s | 1,017 s | `run_full_plan_qa_20260821_132008.log` |
| 64 | 17,548 s | 20,505 s | 1,211 s | `run_full_plan_qa_20260820_152030.log` |

15 核机器上开 64 个 Geant4 worker 的后果：

- Sys 时间涨 **15–17 倍**（纯上下文切换）；
- wall time 反而变长；
- 同配置两次运行相差 **1.5 倍**，结果不可复现。

**位置（修复前）**

- `gui/web_app.py:1672` — 前端自定义线程校验只有 `t>0 && t<=64`，与本机核数无关。
- `gui/tps_topas_gui.py:66-68` — 预设档其实是对的（`min(6/8/12, logical_cpus)`），问题只出在自定义输入。
- `topas/run_full_plan_qa.txt` — `i:Ts/NumberOfThreads = 64`。
- 队列快照 `analysis/_batch_queue/queue.json` 原样接受 `threads: 64`。

**已实施的修复**

单一收敛点在 `gui/runtime_monitor.py`：

- `logical_cpu_count()` / `physical_cpu_count()` — 复用已有的 `_hardware()` 探测，不再各处 `os.cpu_count()`。
- `clamp_threads(requested) -> (effective, note)` — 返回真正会写进 `Ts/NumberOfThreads` 的值，以及被收敛时的人类可读说明（未收敛时 `note` 为空串）。非整数 / ≤0 输入一并归一化。

所有通往 TOPAS 的路径都经过它：

| 位置 | 改动 |
|---|---|
| `scripts/09_prepare_topas_run.py:78-84` | 删掉硬编码的 `1 <= threads <= 64`，改为 `clamp_threads()`；写文件用收敛值；stdout 打 `WARNING:`；`topas_run_preparation_summary.txt` 新增 `Threads requested / used: R / E (logical CPUs: N)` 与 `Thread cap applied:` 两行 |
| `gui/web_app.py:589-598` | 新增 `resolve_threads(payload) -> (effective, requested, note)` |
| `gui/web_app.py:540-547`（`enqueue_case`） | 删掉 `threads must not exceed 64`；入队前把 `payload["threads"]` 就地改成收敛值，并写入 `requested_threads` / `thread_limit_note` |
| `gui/web_app.py:877`（`runtime_context_from_payload`） | 估时用收敛值；上下文里带出 `requested_threads` / `thread_limit_note` / `logical_cpus`，`/api/runtime-estimate` 直接可见 |
| `gui/web_app.py:926`（`build_commands`） | 交互式与队列执行共用的构造函数在此收敛，`prepared_run_matches` 也拿到同一个值，因此 64 线程的旧 entry 会被判为需要重建 |
| `gui/web_app.py:1243` | 收敛发生时在「Commands and live output」首行打 `WARNING:` |
| `gui/web_app.py:1823`（`/api/status`） | 下发 `logical_cpus` |
| `gui/web_app.py:1801-1807`（页面注入） | 新增 `__MAX_THREADS__` 占位符；`max="64"` 已从 HTML 中彻底消失（有测试守着） |
| 前端 JS | `const MAX_THREADS`；两处 `t<=64` 校验改为 `MAX_THREADS` 并给出解释性文案而非只报「invalid」；两个 threads 输入框 `onchange` 就地夹紧并 toast；确认框显示 `Threads requested: t (of N logical CPUs)`；modal note 写明本机核数与实测代价 |
| `gui/batch_queue.py:288-294` | job 记录同时保存 `threads`（实际使用）与 `requested_threads`（操作者原始输入） |
| `gui/batch_queue.py:410-423` | 每次 attempt 开始时再收敛一次 payload——**旧的 `threads: 64` 队列快照重试时也会被夹紧**，并写日志、回写 job 记录 |
| `gui/tps_topas_gui.py:34` | `DEFAULT_THREADS = min(12, os.cpu_count())` |
| `gui/tps_topas_gui.py:741-761` | 新增 `thread_parameter()`：超限时回写输入框、弹 warning、返回收敛值；stage 7 与整条准备流水线都改用它 |
| `gui/tps_topas_gui.py:966-979` | 自定义计划对话框的 `1–64` 校验改为 `1–logical_cpus`，错误文案给出实测理由 |
| `topas/run_full_plan_qa.txt` | `i:Ts/NumberOfThreads` 由 64 改为 12（直接 `topas run_full_plan_qa.txt` 也不会再超订阅） |
| `tests/test_thread_limit.py` | 新增 6 个回归测试 |

设计上刻意选择**收敛（clamp）而非报错**：队列里已有的历史快照、脚本命令行、旧 job 重试都不应该因为一个上限而失败，但它们也绝不该真的跑 64 个 worker。唯一会硬性拒绝的是前端提交路径——那里用户能立刻改。

**验收标准**

- [x] 自定义输入框无法提交大于本机逻辑核数的值（前端拦截 + 后端收敛，双保险）。
- [x] `python -m unittest discover -s tests` → **19 tests OK**（原 13 + 新增 6）。
- [x] `clamp_threads(64)` 在本机返回 `(15, "Requested 64 threads exceeds the 15 logical CPUs...")`。
- [x] 渲染后的页面 JS 通过 `node --check`；`__MAX_THREADS__` 占位符全部替换。
- [ ] **仍待实测**：用 4 / 8 / 12 线程各跑一次相同配置，确认 wall time 单调性合理且 Sys 时间 < 200 s。这是物理机时，代码改动本身无法替代——见附录 B 第 3 条。

**工作量**：半天（已完成）。**预期收益**：立刻回收 30–50% wall time，零风险。

---

### - [ ] 优化项 2：「每个 spot 一次 Geant4 Run」是真正的瓶颈

**证据**

`topas/beam/plan_generated.txt:958-961`：

```
d:Tf/TimelineStart = 0. s
d:Tf/TimelineEnd = 43919. s
i:Tf/NumberOfSequentialTimes = 43919
i:Tf/Verbosity = 0
```

即 TOPAS 执行 **43,919 次顺序 Run**，每次 Run 内有 48 个 source（其中 47 个 `histories = 0`），而每个 spot 只分到 1–8 个 history。

4 线程那次：`8,160 s / 43,919 = 0.186 s 每 spot`。这部分是 BeamOn 启动、worker barrier、scorer 跨线程合并的固定开销，**与 history 数量几乎无关**。

**两个推论**

1. **把 histories 从 100k 提到 1M，wall time 远远不到 10 倍。**
   文档中 P0 级的「低统计（每 spot 仅 1–8 histories，单 voxel / 单 line 噪声明显）」是当前结果说服力最大的短板，而它恰好是目前**最便宜**能修的一条。

2. **结构性改法**：把 `Tf` 时间轴的步长按 spot 的 NF·MU 权重分配，用随机时间采样让一次 Run 覆盖整层甚至整个计划。这同时消掉三个问题：
   - per-run 固定开销；
   - largest-remainder 取整偏差（当前最大 0.517 history）；
   - ~~`scripts/04_generate_topas_plan.py:291` 的「total_histories 必须 ≥ spot 数」BLOCK 约束。~~
     **已于 2026-08-23 改为警告**：低于 spot 数时按权重取前 N 个 spot 各 1 个 primary，其余 spot 从 `Tf` 时间轴删除，
     因此 wall time 随 histories 近似线性下降（10,000 histories ≈ 31 min）。产物标记为 `SPARSE TEST RUN`，
     标定/Gamma/profile 对比对其无效。这只解决了「想快速试跑」的需求，**没有**解决 per-run 固定开销本身——
     完整计划仍是 43,919 次 Run，步骤 C 的结构改造依然需要。

**当前缺口**：没有可用的 histories–wall time 标定曲线。`run-full_plan_10000` 那次在 1,015 个 Run 时被取消，只有 100k 一个点。

**修复方案（分三步，先只读后改写）**

- **步骤 A（先做，不改代码）**：4 线程分别跑 `100k` / `300k` / `1M` histories，记录 Real/User/Sys，拟合 `T = a·N_spots + b·N_histories/cores`。这条曲线是后续所有决策的依据。**每次用不同的 seed**——见 §1.4，相同 seed + 相同线程数会逐字节重现，既浪费机时也拿不到独立样本。
- **步骤 B**：按曲线选定一个「高统计」正式运行（预计 1M histories 的边际成本很低），重跑 profiles + Gamma + MC RTDOSE，直接改善统计噪声。步骤 A 的三次运行如果 seed 各不相同，其结果本身就构成多种子样本，可顺带给出不确定度估计。
- **步骤 C（可选，风险较高）**：改 `04_generate_topas_plan.py` 的时间轴生成逻辑，按能量层聚合成 48 次 Run，或用权重化时间轴 + 随机时间采样合并为单次 Run。

**步骤 C 的前置条件**：必须先对照 TOPAS 4.2 官方文档确认 `Tf/RandomizeTimeDistribution` 的确切语义（本次分析**未在本机验证**该参数行为）。这是方案而非结论。改动后必须验证：总 history 数、每层注量比例、`N_plan` 计算三者与旧实现一致。

**验收标准**

- 步骤 A：得到两参数模型，对已有 100k 数据点的预测误差 < 15%。
- 步骤 B：line dose 的 MC 曲线目视噪声显著下降；Gamma 结果与 100k 版本在协议一致时不出现方向性偏移。
- 步骤 C：与 100k 旧实现在相同种子下的剂量分布差异在统计涨落范围内。

**工作量**：A 约 1 天（主要是等运行）；B 约 1 天；C 约 3–5 天含验证。

---

## 3. P1 级问题

### - [ ] 优化项 3：case 初始化会复制脚本，代码已经分叉（正确性风险）

**证据**

```
$ diff -rq scripts dicom/Dicom/hzRoom1_90_RF4_250701/scripts
Files ... 10_initialize_case.py ... differ
Files ... utils/commissioned_beam.py ... differ      ← 物理代码已经不一致
Only in scripts: 15_prepare_remote_bundle.py         ← 新脚本完全缺失
```

**位置**：`scripts/10_initialize_case.py:72-77`，使用 `copy_if_missing()` —— 目标存在就跳过，**永不更新**。

**后果**：一个早期建立的 case 会静默地用**旧版 commissioned 束流加载器**跑物理，没有任何提示。对一个把「每个 profile 都做 SHA-256 门禁」当作核心设计的项目来说，脚本本身反而没有指纹校验，是明显的不对称。

**已发生的第二种故障：空目录遮蔽 Python 包**（2026-08-23 修复）

`DIRECTORIES` 里的 `"gui"` 会在每个 case root 下建一个**空** `gui/` 目录。Python 3 的隐式命名空间包让这个空目录也能被 `import gui` 命中，于是：

```
scripts/14_calibrate_mc_dose.py  (旧代码)
    sys.path.insert(0, str(root))          # root = case root，插到最前面
    from gui.case_results import ...       # -> ModuleNotFoundError
```

`gui` 解析到 case 里那个空目录（`gui.__file__ is None`，`__path__` 指向 case），真正的 `gui/case_results.py` 永远找不到。**故障点在 TOPAS 输运完成之后**——几分钟到几小时的计算跑完，才在标定步骤崩掉，剂量文件其实是好的。

修复：脚本改用 `APP_ROOT = Path(__file__).resolve().parents[1]` 并在模块顶层导入（与 `09` / `15` 一致，它们本来就是对的），`DIRECTORIES` 去掉 `"gui"`，5 个已存在的空目录用 `rmdir` 删除（只对空目录成功，不可能误删数据）。`tests/test_queue_pipeline.py` 加了三条守卫：scaffold 不得再含 `gui`、仓库里不得出现遮蔽目录、脚本 14 不得再改 `sys.path`。

**修复方案（二选一）**

- **方案 A（推荐）**：不再复制脚本。统一用主目录脚本 + `--case-root` 参数运行；`DIRECTORIES` 中去掉 `scripts` / `scripts/utils`（`gui` 已于 2026-08-23 去掉）。
- **方案 B**：保留复制，但把脚本树的 SHA-256 写进 `manifest.json`，每次运行前比对，不一致就 WARN/BLOCK —— 与现有 `particle_calibration.json` 的做法保持一致。

**验收标准**：修改主目录任一物理脚本后，旧 case 要么直接使用新代码（方案 A），要么在运行前报出指纹不一致（方案 B）。

**工作量**：方案 A 约 1 天（需检查所有脚本的相对路径假设）；方案 B 约半天。

---

### - [x] 优化项 4：case 目录可以建在 DICOM 树里，已造成约 550 MB 垃圾和一个伪造病例身份

**证据**

| 路径 | 大小 | 说明 |
|---|---|---|
| `dicom/Dicom/hzRoom1_90_RF4_250701/` | 259 MB | 整个项目的嵌套副本（scripts / gui / machine_model / analysis / **还有一份 dicom**） |
| `dicom/Dicom/hzroom1-h-rf4-COM-250916/` | 130 MB | 同类副本 |
| `dicom/Dicom/hzRoom1_90_RF4_250701/CT/scripts/` | — | **case root 被选到了 CT 文件夹上** |

最能说明问题的是这条自动生成的路径：

```
dicom/Dicom/hzRoom1_90_RF4_250701/CT/analysis/
  patient-anonymous--study-ffa63583df/
    plan-plan--ffa63583df/
      run-full_plan_100000/topas_runs/settings-20260821T131136_345643.json
```

系统没有拒绝，而是给一个空病例生成了完整的 run 缓存和 settings 快照。

**位置**

- `gui/web_app.py:200` — `safe_root()` 只拒绝 `/` 和路径段数 < 3。
- `gui/case_results.py:58-70` — `case_identity()` 拿不到 RTPLAN 也拿不到 CT 时，回落成 `patient-anonymous` / `plan-plan` 而不是报错。

**已实施的修复（2026-08-23）**

原方案第 1 条「case root 不得位于本项目的 `dicom/` 之下」**未按原文实现**——实测该规则会拒绝当前全部 5 个正在使用的 case（它们都在 `dicom/` 下，共约 1.6 GB，含 6 个剂量二进制和已标定结果），其中包括刚刚成功完成输运与标定的 `hzroom1-h-rf4-COM-250916`。又因为 `APP_ROOT` 自身带 `case_config.json`，第 2 条「不得位于另一个 case root 之内」同样会拒绝这 5 个。**按原文实现会为了防一个已经不存在的问题而破坏正在工作的配置。**

实际采用的判据是「**这个目录能不能在不破坏什么的前提下当 case root**」，而不是「它在不在 `dicom/` 下」：

`gui/web_app.py` 新增 `validate_case_root()`（`safe_root()` 保持原样，仍供 27 处常规调用），拒绝三类目录：

| 拒绝 | 理由 |
|---|---|
| 直接包含 `.dcm` 文件的目录 | 就是报告里 CT 文件夹被选成 case root 的那种灾难 |
| 位于另一个 case 的 `dicom/analysis/topas_output/plan_parsed/...` 之内，或位于另一个 case 之内的任意位置 | 两个 case 的结果会混在一起 |
| 项目自身的功能目录（`gui` / `scripts` / `topas` / `analysis` / `config` / `tests` / …） | case 数据与程序文件混杂；空 `gui/` 遮蔽真实包正是这么来的（见优化项 3） |

允许：结构正常的 case（无论在不在 `dicom/` 下）、全新空目录、`APP_ROOT` 自身（它是所有 case 的模板）。

守卫挂在 `choose_case_folder()` / `choose_case_folders()` 这两个文件选择器上——**唯一的入口**，因此单选和批量选择都覆盖。

第 2 条（身份回落）按原方案实施，但**区分读和写**：`CaseIdentity` 新增 `identified` 字段，读取空 case 的身份仍然可行（DICOM 导入路径需要它拿旧身份和新文件比对），但 `analysis_run_dir(create=True)` 和 `update_run_manifest()` 会经 `require_identified_case()` 抛错。实测确认：被拦下时**一个占位目录都不会创建**。

**磁盘现状**：报告中提到的 `dicom/Dicom/`（388 MB）已不在磁盘上。5 个 case 各有一个 `patient-anonymous--study-ffa63583df/` 残留（各 8 KB，仅 manifest + settings，`mc_source` 为空、无任何结果），且**确实发生了错配**——`deep-250923` 的 anonymous 目录里装着 tag 为 `COM-250916` 的 run，`low (cc)` 和 `MID-250922` 里装着 `deep-250923`。这正是本条要防的现象。因不含数据且属用户文件，未自动删除。

**验收标准**：

- [x] 把 case folder 选到含 `.dcm` 的目录上被拒绝并给出原因。
- [x] 选到另一个 case 的数据子目录被拒绝。
- [x] 现有 5 个 case 全部仍可正常选择（`tests/test_case_root_guard.py` 遍历实际磁盘断言）。
- [x] 空 case 无法生成 `patient-anonymous` 缓存目录。
- [x] 14 个新测试；全套 **63 tests OK**。

**工作量**：半天（已完成；残留 anonymous 目录清理另计）。

---

### - [ ] 优化项 5：运行时估计器是一组无依据的魔数

**证据**：`gui/runtime_monitor.py:144-223` 中出现 `0.15`、`0.82`、`0.16`、`8.0`、`0.35 / 0.25`、`0.003` 等常数，没有物理含义，无法验证。交接文档 §12 也承认「最初 ETA 偏差很大」。

**修复方案**

按优化项 2 的结论，真实模型只有两项：

```
T ≈ a · N_spots  +  b · N_histories / min(threads, logical_cpus)
```

对 `discover_runtime_benchmarks()` 已经在收集的完成日志做二参数最小二乘拟合。两个参数、可解释、可写回归测试。

**依赖**：需要优化项 2 步骤 A 的数据点。

**验收标准**：对已有的每条 benchmark 记录做留一交叉验证，误差 < 25%。

**工作量**：半天（数据齐备后）。

---

## 4. P2 级问题

### - [ ] 优化项 6：Gamma 计算可以快 5–10 倍

**证据**

- `scripts/11_gamma_analysis.py:118` 对全部 **1,791** 个候选偏移做完整扫描；
- 1,791 × 477,753 ≈ **8.56 亿次**三线性插值；
- 实测 `Calculation_seconds`：21.2 s / 23.5 s（两次 Gamma 运行的 metrics CSV）。

**关键点**：`candidate_offsets()`（`11_gamma_analysis.py:91-101`）**已经按距离升序排序**。既然距离单调不减，一旦 `(d_k / DTA)² ≥ γ²_min[i]`，voxel *i* 的结果就不可能再改善。

**修复方案**

1. 每轮收缩活跃索引集：仅对 `gamma_squared > (distance_mm/dta_mm)**2` 的 voxel 继续采样。通过率 99.96%，绝大多数 voxel 在最初几个壳层就定下来。
2. `gamma_squared` 与坐标数组降到 `float32`。

**注意**：这是纯性能改动，**不改变任何数值结果**。修改后必须用同一 MC binary 复算，确认 pass rate 与 `gamma_map.npy` 逐元素一致。

**验收标准**：`Gamma_pass_rate_percent` 与现有 99.9640% 完全一致；`Calculation_seconds` < 5 s。

**工作量**：约 10 行代码，半天含验证。

---

### - [ ] 优化项 7：空闲时前端轮询仍在持续消耗 CPU

**证据**

- `gui/web_app.py:1726` — `poll()` 每 900 ms 请求 `/api/log`；
- `/api/log` 无条件调用 `collect_process_status()`；
- `gui/runtime_monitor.py:344` — 该函数 fork 一个 `ps -ww -axo`（**全系统进程表**）；`:307` 再 fork 一个 `vm_stat`；
- 缓存只有 1.8 s（`runtime_monitor.py:336`），且 `process_group_id is None`（什么都没跑）时照跑不误；
- `gui/web_app.py:1733` — `setInterval(refreshQueue, 1800)` 不判断当前 tab 或页面可见性。

**修复方案**

1. `collect_process_status()`：`process_group_id is None` 且队列无活动 job 时，跳过 `ps` 全表扫描，只返回硬件与内存信息。
2. 空闲时把 `poll()` 间隔退避到 3–5 s，有任务时恢复 900 ms。
3. `refreshQueue` 增加 `document.hidden` 与当前 tab 判断。

**验收标准**：GUI 打开且空闲 5 分钟，进程树中不再出现周期性 `ps` / `vm_stat` fork。

**工作量**：半天。

---

### - [ ] 优化项 8：`gui/web_app.py` 的前端是塞在 Python 字符串里的压缩单行 JS

**证据**

- 文件总计 **2,535 行**；
- `web_app.py:1486-1734` — 约 250 行 HTML / CSS / JS，且是一行一个函数的压缩风格（单行长度常超过 2,000 字符）；
- `Handler.do_GET` / `do_POST` 是一条极长的 `if parsed.path == ...` 链（`:1792` 起）。

**后果**：这段代码无法 lint、无法有意义地 diff、浏览器里无法断点。后续任何 GUI 功能（远程队列、PDF 报告、不确定度展示）都要先趟过这 250 行。

**修复方案**

1. 拆成 `gui/static/{index.html, app.js, app.css}`，用文件服务返回。
2. 现有 CSP 已经是 `default-src 'self'`，拆完可以顺手去掉 `script-src 'unsafe-inline'` 和 `style-src 'unsafe-inline'`，安全性同步提升。
3. 路由改成 dict 分发（`{path: handler}`），拆掉 if 链。

**验收标准**：GUI 全部页面功能不变；`app.js` 可通过 eslint/prettier；`web_app.py` 降到 1,200 行以内。

**工作量**：2–3 天（主要是回归测试所有页面）。

---

### - [ ] 优化项 9：`/file` 端点的读取范围没有真正锁死

**证据**

- `gui/web_app.py:2000` — `root` 来自查询参数，只经过 `safe_root()`（仅拒绝 `/` 和路径段数 < 3）；
- 因此 `?root=/Users/<user>&path=/Users/<user>/.ssh/id_ed25519` 在允许范围内；
- `require_local_origin()`（`:1754`）只用在 POST 上，**GET 完全没有 Origin 校验**。

**严重性评估**：服务只监听 `127.0.0.1`，跨源读取需要 CORS 才能拿到响应体，所以直接利用门槛较高。但对一个处理患者 DICOM 的本地服务，**DNS rebinding 是现实威胁模型**，值得加固。

**修复方案**

1. 把读取范围钉死到 `STATE.root` 和已注册的 case root 集合，不接受任意 `root` 查询参数。
2. 把 Origin/Host 校验也加到 GET 上（`/` 首页除外）。
3. 可选：加一个进程启动时生成的随机 token，前端注入，所有 API 校验。

**验收标准**：构造 `?root=$HOME&path=$HOME/.ssh/...` 的请求被拒绝。

**工作量**：半天。

---

## 5. P3 级问题

### - [ ] 优化项 10：仓库卫生 —— 1.6 GB、没有 Git

**证据**

- 交接文档 §16 P2.6 自述：「项目目录当前没有可用 Git 工作树状态，不要假定可用 `git diff`/rollback」。
- 磁盘占用：

| 目录 | 大小 |
|---|---|
| `dicom/` | 808 MB（其中 `dicom/Dicom/` 389 MB 为意外副本） |
| `analysis/` | 359 MB |
| `.venv/` | 224 MB |
| `topas_output/` | 115 MB（`production/` 内运行日志 51 MB，单个最大 16 MB） |
| `archive/` | 77 MB |
| `plan_parsed/` | 21 MB |
| **合计** | **1.6 GB** |

- 代码部分（`scripts/` 360 KB + `gui/` 480 KB + `tests/` 32 KB + `config/` 12 KB + 文档）约 **1.2 MB**。

**核心观察**：项目现有的「非破坏性归档 / 快照 / `_trash` / 不可变导入」机制，本质上是在**手工重新实现版本控制**。

**修复方案**

```
git init
```

`.gitignore` 建议内容：

```
.venv/
dicom/
analysis/
topas_output/
archive/
plan_parsed/*.png
*.bin
*.binheader
*.npy
*.log
.DS_Store
__pycache__/
```

纳入版本控制的成本几乎为零（约 1.2 MB），收益是优化项 3 那类代码分叉从此不可能发生。

**顺带清理**：`topas_output/production/` 的 51 MB 运行日志（保留最近 2 个即可，其余已在 `analysis/.../topas_runs/archived-*/` 中有归档副本）。

**验收标准**：`git status` 干净；`du -sh .git` < 5 MB。

**工作量**：1 小时。

---

### - [ ] 优化项 11：四份状态文档互相打架（内容已同步，根因未解决）

> **2026-08-22 部分完成**：四份文档已按磁盘实际状态手工同步一次（详见 §1.3）。
> 但这次同步本身就证明了问题——需要一个人逐条比对 `queue.json`、日志、SHA-256 才能发现漂移。
> 下面的「修复方案」针对的是**根因**，仍未实施，因此本项保持未勾选。

**证据（同步前）**

| 文件 | 大小 | 问题 |
|---|---|---|
| `README.md` | 3.4 KB | 「As of 2026-08-21」的结果描述未反映最新运行 |
| `PROJECT_STATUS.md` | 7.9 KB | `:17` 写 `Threads: 12`，实际 job 为 64；`:19` 说 production 是 8,166 s 那次，实际已被取代 |
| `WORKFLOW.md` | 34 KB | `:116` 估时基准仍为水模；`:121` 写「Threads 默认 12」；`:350` CLI 示例用 `--threads 12` |
| `CLAUDE_PROJECT_HANDOFF.md` | 34 KB | §0.1 说「仍有 TOPAS 在运行」，实际已完成；§0.4 自己就在说 `PROJECT_STATUS.md` 已被取代 |

合计 78 KB，全靠手工同步。更严重的是：光看时间戳会**误判**缓存的 Gamma/profiles 已过期
（见 §1.4），只有比对 SHA-256 才能确认它们有效——这类判断不该依赖人工。

**修复方案（根因）**

1. 把「当前病例 / 运行参数 / 最新结果 / binary 指纹」这部分从 `analysis/_batch_queue/queue.json` +
   `analysis/**/manifest.json` **自动生成**（GUI 里一个 Status 页，或一条 `scripts/16_report_status.py`）。
2. 在 manifest 中记录每个下游产物（Gamma / profiles / RTDOSE）所依据的 **MC binary SHA-256**，
   而不只是路径和时间戳。这样"缓存是否对当前 binary 有效"就是一次哈希比对，而不是人工推理。
3. 散文文档只保留**协议、限制、操作规程** —— 这部分写得很好，值得完整保留。
4. 合并 `CLAUDE_PROJECT_HANDOFF.md` 与 `PROJECT_STATUS.md` 的重叠内容，避免两份「当前状态」。

**验收标准**：不存在两份需要手工同步的「当前状态」描述；缓存有效性可由程序判定并在 GUI 显示。

**工作量**：1 天（第 2 条约半天，收益最高，可单独先做）。

---

### - [ ] 优化项 12：13 个测试全在 GUI 管道上，物理路径零覆盖

**证据**

现有 `tests/`：

| 文件 | 覆盖 |
|---|---|
| `test_batch_queue.py` | 队列调度与持久状态 |
| `test_cache_deletion.py` | 可恢复缓存删除 |
| `test_machine_models.py` | 机器包 inspect/import/lifecycle |
| `test_ssh_server.py` | SSH 配置、无 secret 保存、host-key pin |

**完全没有覆盖**：`allocate_histories` 的取整不变量、`N_plan` 计算、Gamma 数值、几何反投影、相空间反传播。

**观察**：这些恰恰是最该被保护的逻辑，现在却是唯一没有自动化验证的部分。

**修复方案** —— 至少补三个数值回归测试：

1. **Gamma 数值**：合成小网格（如 10×10×10），构造已知位移/剂量差，验证 γ 值解析可算的情形。
2. **allocation 不变量**：`sum(histories) == total_histories`；所有正权重 spot 的 `histories >= 1`；`total < n_spots` 时必须抛错。
3. **`N_plan` 精确值**：固定 spot 表 + 固定 NF(E) 表，断言 `N_plan` 到小数点后若干位。

**验收标准**：测试数从 13 增至 16+，且物理脚本改动会触发失败。

**工作量**：1–2 天。

---

### - [ ] 优化项 13：脚本编号有缺口

**证据**：`scripts/` 下有两个 `03_` 前缀（`03_build_topas_dose_scoring.py` 与 `03_validate_topas_dose_scoring.py`），且缺失 `05_`。

**影响**：低，仅影响可读性。

**修复方案**：要么补一个 README 说明编号语义（stage 号 vs 文件序号），要么重编号。**注意**：重编号会波及 `gui/web_app.py` 的 `script()` 调用和 `WORKFLOW.md` 的命令行等价流程，收益小于风险，建议只补说明。

**工作量**：1 小时。

---

## 6. 优化项总览

| # | 优先级 | 标题 | 工作量 | 状态 |
|---:|---|---|---|---|
| 1 | P0 | 线程数上限收敛到本机核数 | 半天 | - [x] 已完成（4/8/12 线程实测待补） |
| 2 | P0 | 每 spot 一次 Run 的瓶颈（A 标定 / B 高统计 / C 结构改造） | 1d / 1d / 3-5d | - [ ] |
| 3 | P1 | case 初始化复制脚本导致代码分叉 | 0.5–1 天 | - [ ] |
| 4 | P1 | case 目录可建在 DICOM 树里 | 半天 | - [x] 已完成（判据调整，见正文） |
| 5 | P1 | 运行时估计器改为两参数拟合 | 半天 | - [ ] |
| 6 | P2 | Gamma 早停优化 | 半天 | - [ ] |
| 7 | P2 | 空闲轮询消耗 | 半天 | - [ ] |
| 8 | P2 | 前端从 Python 字符串中拆出 | 2–3 天 | - [ ] |
| 9 | P2 | `/file` 端点读取范围锁死 | 半天 | - [ ] |
| 10 | P3 | Git + `.gitignore` + 日志清理 | 1 小时 | - [ ] |
| 11 | P3 | 状态文档自动生成 | 1 天 | 内容已同步；根因未做 |
| 12 | P3 | 物理路径数值回归测试 | 1–2 天 | - [ ] |
| 13 | P3 | 脚本编号说明 | 1 小时 | - [ ] |

---

## 7. 建议的推进顺序

**第一批（低风险、立刻见效）**
~~优化项 1（线程上限）~~ **已完成** → 优化项 10（Git）→ 优化项 6（Gamma 早停）→ 优化项 7（空闲轮询）

优化项 1 在建 Git 之前就做了；剩下三项建议先建 Git 再动，这样每一步都可回滚。

**第二批（收益最大）**
优化项 2 步骤 A（标定曲线）→ 步骤 B（高统计正式运行）→ 优化项 5（估计器）

这一批直接改善结果的物理说服力。

**第三批（防止后续踩坑）**
优化项 3（脚本分叉）→ ~~优化项 4（case 目录守卫）~~ **已完成** → 优化项 12（数值测试）

**第四批（可维护性投资）**
优化项 8（前端拆分）→ 优化项 9（端点加固）→ 优化项 11（文档）→ 优化项 13

**暂缓**
优化项 2 步骤 C（结构改造）—— 等步骤 A/B 的数据和优化项 12 的数值测试到位后再动，否则没有回归保护。

---

## 8. 如果只做三件事

1. ~~**把线程上限钉到物理核数**（优化项 1）~~ — **已完成**，见 §2 优化项 1。下一次全计划运行即可兑现 30–50% wall time。

2. **量出 histories–wall time 曲线，然后跑一次高统计运行**（优化项 2 步骤 A + B）
   一次标定 + 一次运行，直接消掉文档中 P0 级的「低统计」限制 —— 这是当前结果说服力最大的短板，而 per-spot 固定开销的结构意味着提高 histories 的边际成本远低于线性。**每次换 seed**，否则如 §1.4 所示只会重现已有结果。

3. **`git init` + 停止把脚本复制进 case 目录**（优化项 10 + 3）
   阻断已经真实发生了的物理代码静默分叉。

---

## 附录 A：本次分析使用的复现命令

```bash
cd /Users/jiangzhenmin/Desktop/PLAN1699_副本

# 硬件
sysctl -n hw.logicalcpu hw.physicalcpu hw.memsize

# 各次 TOPAS 运行的线程数与耗时
for f in topas_output/production/*.log; do
  echo "== $f"
  grep -m1 "setting number of threads" "$f"
  grep -A5 "^Elapsed times:" "$f" | sed -n '2,6p'
done

# 队列实际状态
python3 -c "import json;d=json.load(open('analysis/_batch_queue/queue.json'));print(d['jobs'])"

# 脚本分叉
diff -rq scripts dicom/Dicom/hzRoom1_90_RF4_250701/scripts

# Gamma 耗时与偏移数
.venv/bin/python -c "
import pandas as pd,glob
for f in sorted(glob.glob('analysis/**/gamma_metrics_*.csv',recursive=True)):
    d=pd.read_csv(f).iloc[0]
    print(f.split('/')[-1],'pass',round(d['Gamma_pass_rate_percent'],4),
          'offsets',d['Candidate_offsets'],'secs',round(d['Calculation_seconds'],1))
"

# 磁盘占用
du -sh * .venv 2>/dev/null | sort -h

# 测试
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests
```

---

## 附录 B：未验证事项声明

本报告中以下内容属于**方案建议**而非已验证结论，落地前需自行核实：

1. **`Tf/RandomizeTimeDistribution` 的确切语义**（优化项 2 步骤 C）—— 未在本机 TOPAS 4.2.p3 上验证，需对照官方文档。
2. **优化项 2 步骤 A 的曲线形状** —— 「提高 histories 的边际成本远低于线性」是基于「0.186 s/spot 固定开销与 history 数无关」的推断，需实测确认。
3. **优化项 1 的具体最优线程数** —— 上限已在代码里收敛到 `hw.logicalcpu = 15`，但已知 4 优于 64 之外，4 / 8 / 12 / 15 之间的最优点仍未测。当前 GUI 默认 12、预设档 4/6/8/12 均为经验值，不是实测最优。
4. **`dicom/1699` 与 `dicom/20260813165426` 是否可删** —— 未确认其是否为仍需保留的历史导入，清理前请自行核对。

本报告不涉及物理模型正确性、剂量准确性或临床适用性的任何判断。
