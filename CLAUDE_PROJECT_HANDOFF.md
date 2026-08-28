# PLAN1699 TPS–TOPAS 项目完整交接文档（供 Claude 阅读）

> 更新时间：2026-08-22（Asia/Shanghai），全部状态已对照磁盘核实
> 上一版本：2026-08-21 15:57 CST（当时有计算在运行，该版本的第 0、2 节已作废）
> 项目目录（本次核实）：`/Users/jiangzhenmin/Desktop/PLAN1699_副本`
> 原始目录 `/Users/jiangzhenmin/Desktop/PLAN1699` 仍存在
> 项目性质：碳离子 PBS TPS 计划重建、TOPAS Monte Carlo 物理剂量研究 QA
> 临床声明：当前系统不是临床验收工具，不能据此独立给出临床通过/失败结论。

---

## 0. 给接手模型的最重要说明

1. **当前没有 TOPAS 在运行，也没有 GUI 在运行。** 队列 job `418087a0f0ad` 已经 `completed`
   （2026-08-21T13:19:55 → 16:32:44）。可以自由重启 GUI。上一版本文档第 0 节写的
   「仍有计算在跑、不要重启 GUI、不要杀进程」已经**完全作废**，不要照做。
2. **上一次运行只是把一个已有结果重算了一遍。** 完成于 16:32:44 的 binary 与
   `archived-20260821T131348_028242/dose/` 里那份（来自 2026-08-20 的运行）**SHA-256 完全相同**：
   `46cc0286045b54c9ce88ba28cd87cb7b6194a6587cf4846e0ac912c656b0b8bb`。
   两次都用 seed 1699 + 64 threads，Geant4 MT 在固定种子和固定线程数下可复现，所以结果逐字节一致。
   → 若要再跑，**必须改种子或改 histories**，否则又是 3.2 h 的重复计算。
3. **因此不需要重跑 profiles / Gamma / MC RTDOSE。** 缓存里那几份的时间戳（08-21 01:06 / 01:07 / 01:14）
   看起来早于最后一次运行完成时间，容易被误判为过期，但它们是针对逐字节相同的 binary 计算的，**有效**。
   上一版本文档第 2 节的「本次运行结束后要做什么」清单第 3–5 步**不需要执行**。
4. 唯一在 16:32:44 新生成的产物是粒子标定审计
   `.../run-full_plan_100000_commissioned/calibration/mc_dose_calibration_full_plan_100000_commissioned.json`。
5. `PROJECT_STATUS.md` 已于 2026-08-22 按磁盘实际状态同步，可以信任。
   工程层面的问题清单和修复优先级在 `OPTIMIZATION_REPORT.md`，与本文件互补。
6. 不要通过修改 PatientID 等标签把 MC 剂量挂接到其他病例；Study、Frame of Reference、RTPLAN 引用和
   三维剂量网格都是病例身份的一部分。
7. 所有新修改应保持：DICOM 只读、旧结果非破坏性归档、每病例/计划/运行隔离、临床安全门失败时 BLOCK 而非猜测。
8. 源码中没有任何硬编码绝对路径（脚本一律用 `Path(__file__).resolve().parents[1]` 推导 root）。
   但 83 个审计产物（manifest、summary、calibration JSON）记录的是原始目录
   `/Users/jiangzhenmin/Desktop/PLAN1699`。**那是溯源信息，不是配置，不要批量改写。**

### 接手后建议的第一组只读检查

```bash
cd /Users/jiangzhenmin/Desktop/PLAN1699_副本

# 确认没有遗留进程
ps aux | grep -i topas | grep -v grep
lsof -nP -iTCP -sTCP:LISTEN | grep -i python

# 队列状态（应为 completed）
python3 -c "import json;print(json.load(open('analysis/_batch_queue/queue.json'))['jobs'])"

# 确认当前 production binary 的身份
shasum -a 256 topas_output/production/RTDOSE_00003_DoseToMedium_TPSGrid.bin

# 测试基线（应为 134 tests OK）
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests
```

现在没有正在运行的计算，GUI 重启、队列操作和缓存管理都是安全的。唯一需要谨慎的仍然是：
删除缓存、覆盖 production 输出、改动机器模型这三类动作。

---

## 1. 项目目标与当前完成度

项目目标是把 TPS 导出的真实碳离子 PBS DICOM 计划重建为 TOPAS 输入，并完成以下闭环：

```text
CT / RTSTRUCT / RTPLAN / one-or-more RTDOSE
        ↓
DICOM 身份、引用、方向、网格和支持范围安全门
        ↓
RT Ion Plan 能量层、spot、FWHM、MU/meterset 解析
        ↓
患者 CT 或水模几何 + TPS 同网格 DoseToMedium scorer
        ↓
RTPLAN baseline 或机器 commissioned 束流模型
        ↓
TOPAS 全计划输运
        ↓
机器绑定的 N_plan/N_sim 粒子数标定
        ↓
三方向 profile、任意 line dose、3D Gamma、MC DICOM RTDOSE
        ↓
每患者/计划/运行标准化缓存与可复查审计
```

目前已经实现完整本地工作流、GUI、批处理队列、机器模型包接口、结果缓存和初步 SSH 服务器接口。当前最主要的未完成事项不是软件按钮，而是临床/物理 commissioning：机构 CT 标定、MRF4 几何与残余影响、绝对输出测量证据、多随机种子统计和独立端到端验收。

---

## 2. 最近一次运行的最终状态

以下为 2026-08-22 对磁盘的只读核实结果，取代上一版本的实时快照。

| 项目 | 值 |
|---|---|
| GUI | 未运行 |
| Queue job ID | `418087a0f0ad` |
| 病例/计划 | `20240813005 · hzroom1-h-rf4-COM-250916` |
| Output tag | `full_plan_100000_commissioned` |
| Beam model | `commissioned` |
| Histories / Seed | `100000` / `1699` |
| 请求 threads | `64`（本机仅 15 逻辑核，属过度订阅） |
| 状态 | **completed** |
| 开始 / 结束 | `2026-08-21T13:19:55+08:00` → `2026-08-21T16:32:44+08:00` |
| TOPAS 计时 | Real `11,541.4 s`，User `19,999.4 s`，Sys `1,017.2 s` |
| Production binary | `topas_output/production/RTDOSE_00003_DoseToMedium_TPSGrid.bin`，34,643,840 bytes |
| binary SHA-256 | `46cc0286045b54c9ce88ba28cd87cb7b6194a6587cf4846e0ac912c656b0b8bb` |
| 运行日志 | `topas_output/production/run_full_plan_qa_20260821_132008.log` |
| Queue 文件 | `analysis/_batch_queue/queue.json` |

### 2.1 这次运行没有产生新信息

该 binary 与 `.../run-full_plan_100000_commissioned/topas_runs/archived-20260821T131348_028242/dose/RTDOSE_00003_DoseToMedium_TPSGrid.bin`
的 SHA-256 完全相同。后者来自 2026-08-20 的运行（日志 `run_full_plan_qa_20260820_152030.log`，Real 17,547.6 s）。
两次运行的 seed（1699）和线程数（64）都相同，Geant4 MT 在这两者固定时可复现，因此输出逐字节一致。

结论：这 3.2 h 是一次重复计算。**下次启动前必须改变种子或 histories**，否则仍然只会得到同一份数字。
换个角度看，这也是一次意外获得的可复现性验证——同种子同线程数确实能重现结果，这一点值得保留在记录里。

### 2.2 因此不需要做的事

上一版本第 2 节列了「本次运行结束后要做什么」五步。基于 2.1 的结论，第 3–5 步（重跑 profiles、
重跑 Gamma、重导出 MC RTDOSE）**不需要执行**：缓存中那几份是针对逐字节相同的 binary 算出来的，有效。

| 缓存产物 | 时间戳 | 是否对当前 binary 有效 |
|---|---|---|
| `gamma/gamma_metrics_*.csv`（99.9640%） | 08-21 01:06 | 有效 |
| `dicom/MC_RTDose_*_particle_calibrated_*.dcm` | 08-21 01:07 | 有效 |
| `profiles/*.csv`、`figures/*.png` | 08-21 01:14 | 有效 |
| `calibration/mc_dose_calibration_*.json` | 08-21 16:32 | 本次新生成 |

交叉验证：Gamma metrics 里记录的 `MC_TOPAS_per_run_max_Gy = 4.941871885e-05`，与当前 production
binary 的实际最大值精确相符。

### 2.3 线程数的实测结论

同一 43,919-spot / 100,000-history 全计划，同一台机器（15 逻辑核 / 15 物理核 / 24 GB）：

| 请求线程 | Real | User | Sys | 日志 |
|---:|---:|---:|---:|---|
| 4 | 8,166.35 s | 15,753.9 s | 69.4 s | `run_full_plan_qa_20260818_130551.log` |
| 64 | 11,541.4 s | 19,999.4 s | 1,017.2 s | `run_full_plan_qa_20260821_132008.log` |
| 64 | 17,547.6 s | 20,505.2 s | 1,211.3 s | `run_full_plan_qa_20260820_152030.log` |

15 核机器上请求 64 线程比请求 4 线程慢 1.4–2.1 倍，内核时间高约 15 倍，且相同配置两次相差 1.5 倍。
**请把请求线程数控制在物理核数以内。** GUI 预设档已经是 `min(6/8/12, logical_cpus)`；自定义输入框
自 2026-08-22 起也按本机逻辑核数收敛，前端拒绝 + 后端 clamp 双保险（见 `OPTIMIZATION_REPORT.md`
优化项 1，已完成）。4–15 之间的最优线程数仍未实测。

另外 `topas/beam/plan_generated.txt` 中 `Tf/NumberOfSequentialTimes = 43919`，即每个 spot 一次
Geant4 Run。4 线程时约 0.186 s/spot，且该开销与 histories 数量基本无关——所以提高 histories 的
边际成本远低于线性，这是目前改善「低统计」最便宜的途径（见优化项 2）。

---

## 3. 当前病例、DICOM 和几何

### 3.1 当前活动 DICOM

- CT：199 张。
- RTPLAN：1 个 RT Ion Plan。
- RTDOSE：3 个。
- RTSTRUCT：1 个。
- PatientID：`20240813005`。
- PatientName：`HZ_TOUMO-H_0915`。
- StudyInstanceUID：`1.3.12.2.1107.5.1.7.129139.30000024081219263283300000014`。
- FrameOfReferenceUID：`1.3.12.2.1107.5.1.7.129139.30000024081309454954400000093`。
- RTPlanLabel：`hzroom1-h-rf4-COM-250916`。
- RTPLAN SOP UID：`1.2.826.0.1.3680043.10.1049.20260526102931583000038400`。
- Plan approval：`UNAPPROVED`。

### 3.2 RTPLAN 束流

- Carbon-12，Z/A/charge = 6/12/+6。
- PBS/`MODULATED`。
- 单 beam。
- PatientPosition = HFS。
- Gantry = 90°，couch/pitch/roll = 0°。
- 96 个 control point，其中 48 个为有效能量层，48 个全零 CP 被过滤。
- 43,919 个有效 spot。
- 能量范围：203.67–379.73 MeV/u。
- Spot X：-56.3–56.3 mm。
- Spot Y：-56.2–57.5 mm。
- Spot FWHM X/Y：5.91495–9.8465 mm。
- Isocenter：`[-71.7, -220.0, -1134.0] mm`。
- 有 `MRF4` mini ridge filter 标记，但当前没有独立的 MRF4 物理几何/WET commissioned 模型。

### 3.3 三个 TPS RTDOSE

| 文件 | DoseType | Summation | Units | 用途 |
|---|---|---|---|---|
| `RTDOSE_00001.dcm` | EFFECTIVE | PLAN | GY | 可选择查看，诊断/研究比较 |
| `RTDOSE_00002.dcm` | PHYSICAL | BEAM | GY | 可选择查看，beam 级诊断 |
| `RTDOSE_00003.dcm` | PHYSICAL | PLAN | GY | 默认 TPS reference 和 TOPAS scoring grid |

GUI 会列出所有与当前 Patient/Study/Frame/RTPLAN 兼容的 RTDOSE，并允许选择展示哪一份；只有 `PLAN / PHYSICAL` 是默认标准物理剂量 reference。选择其他类型时，Gamma 和报告会明确标记为诊断/研究比较。

### 3.4 患者和剂量网格

- CT：规则轴向 512×512×199。
- CT voxel：约 1.171875×1.171875×1.5 mm。
- TOPAS patient：`TsDicomPatient`。
- 当前通用 Schneider 映射建立约 2,845 种材料。
- RTSTRUCT External：按 `RTROIInterpretedType=EXTERNAL` 识别 `External+1mm`，不再依赖精确 ROI 名字。
- TPS/TOPAS dose shape `[Z,Y,X] = [152,154,185]`。
- Dose voxel：2×2×2 mm。
- 数值数量：4,330,480 个 float64。
- 正式 MC binary 预期大小：34,643,840 bytes。

### 3.5 当前安全门

兼容性结果：13 PASS / 2 WARN / 0 BLOCK。

两个 WARN：

1. 仍使用通用 TOPAS Schneider HU-to-material 表，不是本机构/扫描协议 CT 标定；极低/超范围 HU 会 clamp。
2. RTPLAN 存在 MRF4，但 MRF4 物理几何/WET 尚未单独 commissioned。

零历史 preflight 已通过：TOPAS 4.2.p3 能完整解析 48 个 layer source、初始化 DICOM CT 和精确 TPS 剂量网格。该 preflight 只证明配置、患者初始化和 scorer 对齐，不证明束流输运和剂量准确性。

---

## 4. GUI 与启动方式

### 4.1 Python 环境

项目已有隔离虚拟环境：

```text
/Users/jiangzhenmin/Desktop/PLAN1699/.venv
```

固定依赖：

```text
pydicom==2.4.4
numpy==2.0.2
pandas==2.3.3
matplotlib==3.9.4
scipy==1.13.1
Pillow==11.3.0
```

### 4.2 启动

可双击：

```text
Launch TPS-TOPAS GUI.command
```

或：

```bash
cd /Users/jiangzhenmin/Desktop/PLAN1699
./.venv/bin/python launch_gui.py
```

GUI 仅监听 `127.0.0.1`。端口会动态选择，所以不要在代码中假定永远是 8765 或 63664。

### 4.3 当前 GUI 页面

- `Workflow`：病例导入、参数、阶段 1–11、状态表、左侧 Run log、实时 CPU/进程面板。
- `Batch queue`：多病例队列、独立 DICOM 导入、1–2 个并行 job、暂停/恢复/取消/重试。
- `Machine models`：机器模型包检查、不可变导入、版本选择、停用/启用。
- `SSH server`：用户配置服务器、host key 指纹核验、连接/环境检查、remote bundle。
- `Results`：缓存结果、三方向报告、Gamma、交互 line dose、MC RTDOSE 导出。
- `Scope & guide`：支持范围和操作说明。

所有输出图标题、坐标轴和图例已经统一使用英文。

---

## 5. 标准本地工作流与脚本映射

GUI 的准备/计算流程：

| Stage | GUI 名称 | 主要脚本 | 作用 |
|---:|---|---|---|
| 1 | DICOM geometry check | `scripts/02_check_dicom_geometry.py` | Patient/Study/Frame、UID 引用、CT/RTDOSE 几何 |
| 2 | Compatibility gate | `scripts/07_validate_case_compatibility.py` | 对支持范围 PASS/WARN/BLOCK |
| 3 | Parse RT Ion Plan | `scripts/01_parse_ion_plan.py` | energy layer、spot、FWHM、权重、英文图 |
| 4 | Generate case geometry | `scripts/08_generate_case_geometry.py` | 水模或 DICOM CT、等中心、source plane |
| 5 | Build TPS dose grid | `scripts/03_build_topas_dose_scoring.py` | 生成与默认 PLAN/PHYSICAL RTDOSE 完全一致 scorer |
| 6 | Generate full spot plan | `scripts/04_generate_topas_plan.py` | baseline 或 commissioned source/history allocation |
| 7 | Prepare TOPAS run | `scripts/09_prepare_topas_run.py` | histories、threads、seed、output tag、入口 |
| 8 | TOPAS preflight | TOPAS + `03_validate...` + `12_validate...` | 完整 parse 与零历史评分网格验证 |
| 9 | Run TOPAS | TOPAS `run_full_plan_qa.txt` | 正式输运；commissioned 后追加 `14_calibrate_mc_dose.py` |
| 10 | Export profiles | `scripts/06_export_three_direction_profiles.py` | depth/X/Y English PNG 和 CSV |
| 11 | Gamma analysis | `scripts/11_gamma_analysis.py` | 用户 DD/DTA 的全局 3D Gamma 和通过率 |

`Run stages 1–7` 会按顺序执行前七步。`Run TOPAS` 会检查当前 histories、threads、seed、beam model 和 layer selection；若派生配置过期，会自动重建 stage 4、6–8，再开始输运。

### 5.1 避免 production 覆盖/冲突

过去 `09_prepare_topas_run.py` 会因为 production 输出存在而报错。现在开始新运行前，现有 `.bin`、header、配置、allocation、机器 profile 快照和日志会自动移动到当前患者运行缓存的 `topas_runs/archived-.../`，而不是覆盖或阻断。

### 5.2 低 histories 检查

如果 total histories 太低，以至于正权重 spot 中至少一个会分到 0 history，`04_generate_topas_plan.py` 会 BLOCK 并给出 rough lower bound。当前 43,919 spots 的 commissioned 相对注量不允许用任意过小 histories 静默丢 spot。

---

## 6. DICOM 导入与更换病例

### 6.1 点击式导入

Workflow 和 Batch queue 都提供独立按钮导入：

- CT：选择切片文件夹。
- RTPLAN：单选文件。
- RTDOSE：允许多选。
- RTSTRUCT：单选文件。

不再要求用户手工填写路径。读取时按 DICOM `Modality` 分类，检查同一批次和活动病例的 Patient/Study/Frame 一致性。普通文件名、文件扩展名和厂商导出目录结构不是判定 modality 的依据。

### 6.2 安全替换

若目录已有同类别 DICOM，用户确认后旧文件先移动到：

```text
dicom_archive/<timestamp>/<modality>/
```

然后再提交新文件。导入审计写入：

```text
dicom/import_history/
```

审计包括原始/活动文件名、SOP/Study/Frame UID、大小、SHA-256 和旧文件归档路径。

检测到新 Study 或新 RTPLAN 时：

1. 缓存旧患者设置和 production 结果。
2. 新患者 histories/threads/seed/beam/Gamma/视图恢复默认。
3. 旧患者的结果缓存保留。
4. 新病例必须重新运行阶段 1–8 和 TOPAS，不能复用旧 MC。

`Reset defaults` 只恢复界面参数，不删除 DICOM、机器模型、日志或缓存结果。

---

## 7. Beam Energy、spot 与机器 commissioned 模型

### 7.1 用户可选的束流输入

GUI 支持：

1. `Use RTPLAN Energy + spots`：完整 RTPLAN energy layers、spot IEC X/Y 和 delivery sequence。
2. `Set Energy + one spot manually`：用户输入单能量、单 spot、位置、FWHM 和能散，用于研究测试，不代表完整 TPS 计划。

RTPLAN 模式还可以逐层勾选 energy layer；默认全选。选择子集会使 stage 6–8 过期并重新生成，且输出审计会记录具体 LayerIndex。子集结果不能直接解释为完整 TPS 计划验证。

### 7.2 Beam model 模式

- `RTPLAN baseline (uncommissioned)`：使用 RTPLAN energy/spot 和研究性参数。
- `Machine commissioned (IDD + emittance + VSAD)`：使用机器专属实测 IDD 离散谱、Fermi–Eyges 相空间、DICOM VSAD spot 轴和 energy-dependent number-per-MU。

Baseline 模式可启用研究性 override：

- energy scale；
- energy offset MeV/u；
- spot-size scale；
- energy-spread percent。

Commissioned 模式禁用这些任意缩放，因为会破坏谱、相空间和注量标定的一致性。要修改 commissioned 参数，应建立新的机器模型版本并重新审核。

### 7.3 当前活动机器模型

机器名：`hzRoom1_90_RF4_250701`。

目录：

```text
machine_model/beam_commissioning/hzRoom1_90_RF4_250701/
```

核心文件：

- `profile.json`
- `particle_calibration.json`
- `energy_spectrum.json`
- `phase_space.json`
- `number_per_mu.txt`
- `measured_pristine_bragg_peaks.csv`
- `measured_spot_sigma.csv`
- `commissioned_energy_list.txt`

模型来源：`/Users/jiangzhenmin/Desktop/TOPAS_Test` 的 Hangzhou/1685 参考数据和方法。

建模方式：

- pristine IDD 通过 NNLS 拟合每个标称能量的离散总能量谱；
- spot sigma-depth 通过 Fermi–Eyges 拟合上游相空间；
- source plane 位于等中心上游 680 mm；
- DICOM X/Y VSAD 为 `[5398.68, 6198.24] mm`，容差 25 mm；
- spot 由等中心反投到 source plane；
- energy-dependent NF(E) 同时用于相对 history allocation 和 `N_plan`；
- IDD 谱被认为已经包含上游 nozzle 能损，所以不再额外叠加 WET slab，避免双重能损。

曾发现并修正参考生成器的单位标签问题：Fermi–Eyges position sigma 的输入和拟合结果实际是 mm，但旧生成器标成 cm；本项目明确按 mm 写入，避免横向 source sigma 放大/缩小 10 倍。

相空间反传播审计覆盖 122 energies：实测 sigma 重建中位 RMSE 约 0.0018 mm，最大 RMSE 约 0.0455 mm；profile BLOCK 阈值 0.25 mm。当前 48 个 RTPLAN energy 与 spectrum/phase-space/NF 表精确匹配。

### 7.4 机器模型标准包接口

`Machine models` 页面接受 ZIP。标准说明：`machine_model/MACHINE_MODEL_PACKAGE.md`。

接口流程：

1. `Inspect package` 只在临时目录检查，不写入 registry。
2. 检查 schema、完整清单、所有 SHA-256、单位、版本、来源、审批记录。
3. 对当前 RTPLAN 检查机器名、VSAD、能量覆盖和 calibration binding。
4. 展示 PASS/WARN/BLOCK。
5. 用户明确确认后按 ID + version + content fingerprint 不可变导入。
6. 同机器多个 active version 时必须明确选择，不自动猜测。
7. 历史结果引用的模型不删除，只能停用；停用不影响历史缓存。
8. 计算 running/paused/cancelling 时禁止导入或改变模型状态。

以下资产必须独立于 beam package：

- `ct_calibration`
- `nozzle_geometry`
- `absolute_output_calibration`

目前独立资产包只完成不可变登记、哈希和审批审计；尚未自动绑定到计算。

---

## 8. 粒子数标定、剂量解释与 Gamma

### 8.1 当前默认标定协议

Commissioned 运行使用：

```text
N_plan = Σ_i [ MU_i × NF_machine(E_i) ]
N_sim  = Σ_i AllocatedHistories_i
MC scale = N_plan / N_sim × C_machine
```

当前机器：

```text
C_machine = 1.0
status = identity_no_empirical_correction
```

重要边界：

- NF(E)、profile、任何正式 output correction 属于机器模型。
- N_plan、N_sim 和二者比值属于具体 patient/run，不能写成机器常数。
- TPS dose 不参与 MC scale 拟合。
- 原始 TOPAS `.bin` 永不改写，scale 由下游读取时仅应用一次。
- `particle_calibration.json` 绑定 profile、NF 和 output-correction 证据哈希。

### 8.2 上一次已完成 commissioned 结果

上一次已完成 100,000-history 结果：

```text
N_plan = 9,566,797,062.83574
N_sim  = 100,000
scale  = 95,667.97062835739
```

缓存目录：

```text
analysis/patient-20240813005--study-5510801c46/
  plan-hzroom1-h-rf4-com-250916--18beea577c/
    run-full_plan_100000_commissioned/
```

上一次 reprocessed 3D Gamma：

- DD/DTA：3% / 3 mm。
- 全局 normalization。
- 低剂量阈值：TPS maximum 的 10%。
- 通过率：99.9640%。
- 通过/评价 voxel：477,581 / 477,753。
- TPS ≥ 50% maximum 区域的 calibrated MC/TPS median ratio：约 0.9999317。

这个高通过率不能单独证明临床准确性，原因包括全局 DD、10% threshold、当前 scale 协议、低统计波动以及缺失的 CT/MRF4/绝对输出独立验收。Gamma 是协议相关结果，不是束流 commissioning 的替代品。

### 8.3 Gamma 实现

用户在 GUI 输入：

- `Gamma DTA (mm)`，范围 `(0,20]`；
- `Gamma DD (%)`，范围 `(0,100]`。

当前算法：

- TPS 为 reference，MC 为 evaluation；
- global DD；
- 固定 10% TPS max low-dose threshold；
- MC 三线性插值；
- Gamma ≤ 1 通过；
- commissioned 运行必须能重建并验证 N_plan/N_sim allocation；
- baseline、manual、allocation 缺失或 hash 过期时不再静默回退 TPS peak fit，而是停止。

输出：summary TXT、metrics CSV、Gamma map NPY、map PNG、pass/fail PNG。Gamma map threshold 外为 NaN。当前空间搜索实现重点保证 Gamma ≤ 1 的通过判定，不应把失败区域的数值当作无限范围的完整 Gamma 分布解释。

---

## 9. Results、三方向曲线、Line Dose 和 DICOM RTDOSE

### 9.1 三方向结果

`scripts/06_export_three_direction_profiles.py` 输出：

- `depth_direction_<tag>.png`
- `transverse_x_<tag>.png`
- `transverse_y_<tag>.png`
- `depth_dose_<tag>.csv`
- `transverse_profile_x_<tag>.csv`
- `transverse_profile_y_<tag>.csv`
- `profile_export_summary_<tag>.txt`

图全部为英文。深度 CSV 包含 TPS Gy、particle-calibrated MC Gy、平滑值、各自归一化值、patient 坐标和 `Depth_from_beam_entry_mm`。X/Y CSV 同时保留 IEC 相对坐标和 DICOM patient 坐标。

### 9.2 Interactive line dose

功能位于 `gui/line_dose.py` 和 Results 页面：

- axial/coronal/sagittal；
- 默认打开最接近 RTPLAN isocenter 的 slice；
- 图上绘制金色等中心十字线；
- 用户拖动 A→B 画任意直线；
- TPS 和 MC 在三维规则网格中做三线性插值；
- 显示绝对 particle-calibrated Gy、独立归一化或明确标记的 legacy 模式；
- 采样数可调，默认 512；
- CSV 可下载，同时写入当前患者 run 的 `line_dose/` 缓存。

低 history 时 line dose 的 MC 波动明显属于预期统计噪声，因为 100,000 histories 分散在 43,919 spots，每个 spot 当前仅约 1–8 histories。单 voxel/单线尖峰不适合作为绝对输出指标；后续应采用更多 histories、多 seed、适当体积平均和 uncertainty 展示。

### 9.3 MC DICOM RTDOSE

`gui/mc_rtdose.py` 支持导出：

- particle-calibrated QA Gy（默认）；
- raw per-run；
- legacy TPS-peak-fit（诊断）。

导出使用 Explicit VR Little Endian、独立 SOP/Series UID、原 Patient/Study/Frame，并引用当前 RTPLAN。导出前后会重读并检查：

- PatientID；
- PatientName；
- StudyInstanceUID；
- FrameOfReferenceUID；
- Referenced RTPLAN SOP UID；
- dose grid 和 orientation；
- quantization round-trip error。

上一次 particle-calibrated MC RTDOSE 已成功被修正为与原 TPS DICOM 同一患者/Study/Frame，可共同导入 TPS。其路径记录在当前 run `manifest.json` 中。

`Import MC RTDOSE` 可导入与当前病例完全兼容的 MC RTDOSE，无需重新运行 TOPAS即可查看；外部 Patient/Study/Frame/grid 不一致时拒绝。

---

## 10. 结果缓存、归档和删除

标准目录：

```text
analysis/
  patient-<patient-id>--study-<hash>/
    plan-<plan-label>--<rtplan-hash>/
      run-<output-tag>/
        manifest.json
        figures/
        profiles/
        gamma/
        calibration/
        dicom/
        line_dose/
        topas_runs/
```

`manifest.json` 记录：病例/计划身份、TPS RTDOSE 选择、MC 来源、beam 设置、energy layer、机器 fingerprint、particle calibration、DICOM export 和 TOPAS archive。

GUI Results 的 `Cached result run` 会扫描这些 manifest。加载缓存时会同步恢复对应 TPS RTDOSE；不会把历史结果误认为当前 production。

缓存删除：

- `Delete cached` 不直接永久删除；
- 整个 `run-<tag>` 原子移动到 `analysis/_trash/...`；
- 写入 `deletion.json`；
- 不删除 DICOM、当前 production、机器模型；
- 病例 queued/running/paused/cancelling 时拒绝删除。

历史水模结果已经隔离到：

```text
archive/legacy_water_phantom_20260818/
```

它包含旧 56,349 spots、不同 DICOM 和 `[153,152,201]` 网格，绝不能自动选择为当前患者结果。

---

## 11. 多病例 Batch queue

已实现需求：

- 可导入任意数量完成 DICOM 准备的病例进入等待队列；
- 本机并行槽位只允许 1 或 2；
- 完成后自动启动下一个；
- 每病例独立保存、Pause、Resume、Cancel、Retry、Remove 和日志；
- 相同病例目录不会同时运行两条 job；
- 队列页可独立选择/创建 case，并分别导入 CT/RTPLAN/RTDOSE/RTSTRUCT，不改变 Workflow 当前 case；
- 每个 job 入队时固化 histories、threads、seed、beam model、output tag 等设置；
- 非当前 case 入队时默认使用其自身 RTPLAN 的全部 energy layers，避免误套当前 case 的 LayerIndex。

持久文件：

```text
analysis/_batch_queue/queue.json
analysis/_batch_queue/job-<ID>/run.log
```

调度语义：

- `Start / auto-run`：填满空槽并自动接续。
- `Stop scheduling`：只停止启动新 job，不停止活动 TOPAS。
- `Pause`：对当前 POSIX process group 发送 SIGSTOP。
- `Resume`：SIGCONT。
- `Cancel`：停止任务但保留 partial files 审计。
- `Retry`：失败/取消/中断后重新走 pipeline、preflight、run_topas。

暂停/恢复是进程内功能，不是 TOPAS checkpoint。GUI 异常退出后原 running/paused job 会标为 `interrupted`，不会自动重复；需要先检查是否存在孤儿 TOPAS 进程，再由用户决定 Retry。浏览器页面 refresh 不会中断 GUI 后端或 TOPAS——任务活在服务器进程里（daemon 线程 + `start_new_session=True` 的独立进程组），与浏览器连接无关；`tests/test_refresh_safety.py` 对此有回归测试。自 2026-08-22 起刷新后还会**自动重新挂接**：页面从 `/api/log` 的 `form_state` 恢复正在运行那次任务的 histories / threads / seed / output_tag / 束流设置 / 能量层选择，并在任务结束前冻结这些输入，避免重载后的表单与在跑的计算不一致。

同时跑两个病例会叠加 threads 和内存。当前单病例已经使用约 3–4 GB RSS 且线程扩展很差，因此默认建议本机单槽，只有经过资源验证后才用双槽。

---

## 12. Run log、实时资源和 ETA

Run log 已从独立标签移到 Workflow 左侧空白区域，不在页面底部。

`Command and live output` 内有可折叠计算状态面板，显示：

- 当前命令；
- wall/active/paused time；
- planned estimate 和 confidence；
- 根据实际 spot/history 的 ETA 和 finish time；
- task CPU percent 和有效 cores；
- system CPU/load/memory；
- task RSS；
- requested TOPAS threads、实际 OS threads；
- process PID/PPID/PGID/state/elapsed/command；
- 系统高占用进程。

最初 ETA 偏差很大是因为 TOPAS 的多线程扩展与历史总量不线性、每个 spot 有固定启动成本、spot 分配不均、DICOM CT/material 初始化和 I/O 不可忽略。现在估计器优先使用相同病例/模式/spot count 的历史完成日志，并在运行后切换为观测的 sequential-spot progress ETA。

---

## 13. SSH 服务器功能

> GUI 当前未运行，下次启动即加载本节描述的源码。`config/ssh_server.json` 仍为 disabled、host/user 空白，不会连接或上传。

### 13.1 当前实现范围

新 `SSH server` 页面允许用户输入：

- server ID；
- direct hostname/IP 或 OpenSSH alias；
- SSH username；
- port；
- ssh-agent/macOS Keychain 或现有 private-key file 路径；
- remote job root；
- server TOPAS executable；
- server Geant4 setup script；
- Geant4 data root；
- remote parallel-job limit；
- enabled 状态。

安全规则：

- 不保存 password。
- 不读取/保存 private-key contents，只记录用户通过 macOS chooser 选择的现有路径。
- OpenSSH 使用 `BatchMode=yes`。
- 首先 `ssh-keyscan` 检查候选 host key。
- 页面显示 SHA-256 fingerprint，要求用户通过独立渠道核验。
- 用户明确 `Trust verified key` 后才写入项目 `config/ssh_known_hosts`。
- `StrictHostKeyChecking=yes` 和 project `UserKnownHostsFile` 强制启用。
- hostname/alias/port 改变时清空旧 fingerprint。
- key replacement 有单独强警告和 explicit confirmation。
- 页面有未保存修改时，Inspect/Test/Environment/Bundle 按钮锁定。
- 远端命令由应用固定生成，不允许用户从浏览器输入任意 shell command。

当前配置文件：`config/ssh_server.json`。目前仍为 disabled、host/user 空白，不会连接或上传。

### 13.2 Remote bundle

`scripts/15_prepare_remote_bundle.py` 在当前 run 缓存中创建 immutable bundle，包含：

- TOPAS parameter tree 的 staged copy；
- manifest 和文件 SHA-256；
- `run_remote_transport.sh`；
- `01_upload_bundle.sh`；
- `02_submit_server_topas.sh`；
- `03_remote_status.sh`；
- `04_download_results.sh`。

传输策略：

- CT 传到 server SHA-256-addressed shared cache；
- 只改 staged `patient.txt` 的 DICOM path；
- 本地 active TOPAS 文件不改；
- 上传 TOPAS 参数和 CT，不上传本机 TOPAS/Geant4 executable；
- 不上传 RTPLAN、RTDOSE、RTSTRUCT；
- server launcher source 用户配置的 Geant4 setup，并调用 server TOPAS；
- download 后仍在本机做 grid validation、calibration、profiles、Gamma 和 RTDOSE export。

当前仍是“准备 audited scripts + 用户显式执行脚本”的初步服务器接口，尚未完成 GUI 内远程队列自动提交、实时远程资源监控、断线重连或多服务器调度。

相关文件：

- `gui/ssh_server.py`
- `gui/web_app.py`
- `scripts/15_prepare_remote_bundle.py`
- `config/ssh_server.json`
- `config/ssh_known_hosts`
- `config/README_SSH_SERVER.md`

---

## 14. 关键源码地图

### GUI/服务

| 文件 | 责任 |
|---|---|
| `launch_gui.py` | Web GUI 入口 |
| `gui/web_app.py` | HTTP API、HTML/CSS/JS、workflow actions、导入、results、SSH 页面 |
| `gui/tps_topas_gui.py` | 旧/共享 GUI workflow 状态与运行辅助 |
| `gui/batch_queue.py` | 持久多病例队列、调度、pause/resume/cancel/retry、进度 |
| `gui/runtime_monitor.py` | CPU、memory、threads、process、ETA 数据 |
| `gui/case_results.py` | patient/plan/run cache、manifest、archive、trash |
| `gui/line_dose.py` | CT/TPS/MC frame、三线性 line sampling、CSV |
| `gui/mc_rtdose.py` | MC DICOM RTDOSE 导出和 DICOM identity 校验 |
| `gui/machine_models.py` | 机器 ZIP inspect/import/registry/version lifecycle |
| `gui/ssh_server.py` | SSH config、host-key pin、connection/env checks |

### 物理/生成脚本

| 文件 | 责任 |
|---|---|
| `scripts/01_parse_ion_plan.py` | RT Ion Plan → energy_layers/spots/report |
| `scripts/02_check_dicom_geometry.py` | DICOM identity/reference/geometry |
| `scripts/03_build_topas_dose_scoring.py` | TPS 同网格 scorer |
| `scripts/03_validate_topas_dose_scoring.py` | TOPAS grid output validation |
| `scripts/04_generate_topas_plan.py` | source、spot、history allocation |
| `scripts/06_export_three_direction_profiles.py` | depth/X/Y profiles |
| `scripts/07_validate_case_compatibility.py` | 支持范围 gate |
| `scripts/08_generate_case_geometry.py` | patient/source geometry |
| `scripts/09_prepare_topas_run.py` | run entry 和 collision-safe archive |
| `scripts/10_initialize_case.py` | 新 case 初始化 |
| `scripts/11_gamma_analysis.py` | global 3D Gamma |
| `scripts/12_validate_topas_preflight.py` | formal preflight summary |
| `scripts/13_import_topas_test_beam_model.py` | TOPAS_Test 参考模型导入 |
| `scripts/14_calibrate_mc_dose.py` | N_plan/N_sim audit |
| `scripts/15_prepare_remote_bundle.py` | SSH remote bundle |
| `scripts/16_generate_water_phantom_spot.py` | 水模单能单 spot 的 TOPAS 参数文件生成 |
| `scripts/17_run_water_phantom_spot.py` | 水模输运 + IDD/PDD/profile 导出与实测对比 |
| `scripts/utils/commissioned_beam.py` | commissioned profile loader/validation |
| `scripts/utils/water_phantom.py` | 实测 IDD 解析、一维 scorer 读取、射程/宽度指标、一维 gamma |
| `scripts/utils/mc_dose_calibration.py` | machine-bound calibration consumer |

### 状态和文档

- `README.md`：快速说明。
- `WORKFLOW.md`：完整操作、协议、限制、命令行等价流程；第 10 节是水模单能单 spot 验证通道。
- `PROJECT_STATUS.md`：截至最近一次同步（2026-08-25）的状态，已按磁盘核实。若有计算在跑，实时状态以 queue/API 为准。
- `OPTIMIZATION_REPORT.md`：工程与计算效率问题清单，13 项，含证据、修复方案、验收标准和工作量。
- `machine_model/MACHINE_MODEL_PACKAGE.md`：标准机器包规范。
- `config/README_SSH_SERVER.md`：SSH 配置和 host-key 流程。
- 本文件：当前跨功能总交接。

---

## 15. 测试与已验证事项

最新修改后执行：

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests
```

结果：**134 tests PASS**（2026-08-25 在 `PLAN1699_副本` 中复核，耗时约 6 s）。

当前自动化测试覆盖：

- batch queue 基本调度和持久状态；
- recoverable cache deletion；
- machine package inspection/import/lifecycle；
- SSH 用户配置、无 secret 保存、host-key pin；
- remote bundle path rewrite 和 server runtime 脚本；
- 水模验证的物理分析层（`tests/test_water_phantom.py`，71 个用例）：解析式峰的 R80/R50/R20 闭式解、
  精确高斯的矩 sigma 与 FWHM 回收、一维 gamma 的同曲线满分与移位失败、被转置多维数组的拒绝、
  实测 IDD 解析与探测器等面积半径、spot 轴投影的逐轴 VSAD 与几何反算门限、bin 数吸附、
  以及 `--analysis-only` 的三个门。

**未覆盖**（重要）：全计划物理路径仍然缺少数值回归测试——`allocate_histories` 的取整不变量、`N_plan` 计算、
三维 Gamma 数值、相空间反传播都没有断言。水模通道（`tests/test_water_phantom.py`）已经覆盖了射程/宽度指标、
一维 gamma 和 spot 轴投影，但那是一条独立通道，不能替代全计划路径的断言。

最新 SSH 页面还在隔离的本地 mock preview 中完成交互检查：direct/alias 切换、identity-file chooser 状态、unsaved lock、fingerprint 展示和浏览器 console 均正常。

自动化测试不等于物理 commissioning。当前仍缺大量不同 DICOM vendor/geometry、真实测量和高统计 Monte Carlo regression cases。

---

## 16. 已知限制和风险

### P0：物理/临床

1. 通用 Schneider 表不是机构 CT scanner/protocol 标定。
2. MRF4 几何、材料、安装位置、周期结构、WET 和残余散射/碎裂尚未独立实现与验收。
3. 当前 IDD-derived spectrum 被认为已经包含上游能损，因此不另加 WET；这避免双计，但不能替代 MRF4 残余影响验证。
4. `C_machine=1` 只是没有经验 correction，不代表绝对剂量已经临床标定。
5. 需要 monitor chamber 可追溯性和独立测量证据。
6. 当前结果为 physical DoseToMedium，不是 RBE/biological dose。
7. 100k histories 对 43,919 spots 属于低统计，单 spot、单 voxel、单 line 明显噪声。
   由于 wall time 由每-spot 固定开销主导而非 histories 数量，提高 histories 是目前最便宜的改善途径（见第 2.3 节）。
8. 高 Gamma pass rate 是协议结果，不能视作完整物理正确性的证明。
9. 目前只有单一随机种子的结果，没有多种子统计和不确定度估计。注意：同 seed 同线程数会逐字节重现，
   因此"再跑一次"不构成独立样本。

### P1：适用范围

当前自动 workflow 仅支持：

- 一个 carbon-ion PBS beam；
- HFS；
- gantry 90°；
- couch/pitch/roll 0°；
- axis-aligned regular RTDOSE；
- 规则轴向 DICOM CT，或 axis-aligned rectangular 0-HU water phantom。

未支持/需扩大：

- 多 beam 汇总；
- 任意 gantry/couch/patient position 的 IEC 61217 transform；
- 倾斜/非轴向 CT 和完整方向余弦；
- couch/fixation/ROI material override；
- 多机构/多机器完整绑定。

### P2：工程化

1. Pause/Resume 不是 restart-persistent TOPAS checkpoint。
2. GUI 进程崩溃后的孤儿计算不会自动无风险重连。
3. SSH 目前没有 GUI 一键远程提交/暂停/恢复/资源监控。
4. 还没有一键 PDF QA 报告。
5. 需要更多 DICOM 回归 fixture 和物理指标测试。当前 13 个测试全部覆盖 GUI/队列/机器包/SSH 管道，
   物理路径（allocation 取整、`N_plan`、Gamma 数值、几何反投影）零覆盖。
6. 项目目录当前没有可用 Git 工作树状态，不要假定可用 `git diff`/rollback；修改前应自行建立备份或版本控制。
7. `scripts/10_initialize_case.py` 用 `copy_if_missing` 把脚本复制进每个 case 且永不更新，代码会分叉。
   磁盘上已经发生：`dicom/Dicom/hzRoom1_90_RF4_250701/scripts/` 中 `utils/commissioned_beam.py` 与
   `10_initialize_case.py` 已与主目录不一致，`15_prepare_remote_bundle.py` 缺失。
8. `safe_root()` 没有阻止把 case root 建在 `dicom/` 内部或另一个 case 之内，已造成约 550 MB 意外副本
   和一个 `patient-anonymous--study-...` 的伪造病例身份。
9. 完整的工程问题清单、证据、修复方案和验收标准见 `OPTIMIZATION_REPORT.md`（13 项，含优先级与工作量）。

---

## 17. 建议后续优先级

### 第一优先：不要再重复计算，先把设置改对

1. ~~把自定义 threads 上限收敛到本机逻辑核数~~ —— **已于 2026-08-22 完成**，见 `OPTIMIZATION_REPORT.md` 优化项 1。
2. 下次 `Run TOPAS` 前**改变 seed 或 histories**；沿用 seed 1699 + 64 threads 只会重现已有结果。
3. 先做一次 histories–wall time 标定（4 线程，100k / 300k / 1M 各一次），拿到两参数模型。
4. 依据该曲线跑一次高统计正式运行，然后重跑 profiles 和 Gamma。
5. 重启 GUI 即可加载当前 SSH 页面源码（GUI 现在没有运行，无需等待）。

### 第二优先：提升结果说服力

1. 用多个独立 random seeds/批次运行并合并 uncertainty。
2. 增加 voxel/line volume averaging、误差条和收敛图。
3. 导入实际测量 IDD 和 X/Y profiles，在同图显示 TPS/measurement/MC。
4. 增加 R80/R50、distal falloff、FWHM、penumbra、center shift 指标。
5. 在 commissioning 完成后才定义正式 acceptance criteria。

### 第三优先：完成机器与扫描协议绑定

1. 导入机构 CT HU–material/RSP package，并明确绑定 scanner/protocol。
2. 建立 MRF4/nozzle geometry package 与 calculation binding。
3. 建立有测量证据的 absolute-output calibration package 与审批流程。
4. 新增其他 TreatmentMachineName 时，按不可变模型版本导入，不修改现有机器文件。

### 第四优先：服务器计算

1. 当前 run 后重启并测试新 SSH GUI。
2. 用无患者数据的小 TOPAS phantom 验证 server TOPAS/Geant4 环境。
3. 独立核验 host fingerprint。
4. 经机构批准后测试 CT bundle upload。
5. 再实现 GUI 远程 submit/status/download、断线重连和远程队列。

---

## 18. 推荐阅读顺序

Claude 接手时建议按以下顺序阅读，避免仅凭旧摘要修改：

1. `CLAUDE_PROJECT_HANDOFF.md`（本文件）
2. `PROJECT_STATUS.md`（2026-08-22 已按磁盘同步）
3. `OPTIMIZATION_REPORT.md`（工程问题清单与修复优先级）
4. `README.md`
5. `WORKFLOW.md`
6. `case_config.json`
7. `plan_parsed/compatibility_summary.txt`
8. `plan_parsed/plan_summary.txt`
9. 当前 run 的 `manifest.json`
10. `gui/web_app.py`、`gui/batch_queue.py`、`gui/case_results.py`
11. 与具体任务相关的 scripts/utils
12. `machine_model/.../profile.json` 与 `particle_calibration.json`
13. 若处理 SSH，再读 `config/README_SSH_SERVER.md` 和 `gui/ssh_server.py`

接手后任何会修改活动输出、DICOM、机器模型或运行进程的动作，都应先完成只读状态检查，并明确区分当前 production、历史 cached run 和 archive。

