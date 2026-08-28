# TPS–TOPAS 计划重建与验证工作流

## 1. 当前完成状态

PLAN1699 已完成从 TPS DICOM 到 TOPAS 正式计算、独立粒子数标定、三方向结果、Gamma 和 DICOM RTDOSE 导出的闭环，并已改造成可复用、带安全门的本地 GUI 工作流。工作流现支持矩形 0-HU 水模和轴向真实 DICOM CT 两类患者模型。

当前已验证的数据与产物：

- 当前新病例 DICOM：199 张 CT、1 个 RT Ion Plan、3 个 RTDOSE、1 个 RTSTRUCT；几何引用和 Frame of Reference 检查通过。兼容性安全门为 13 项通过、HU 标定和 MRF4 两项警告、0 项阻断。
- 当前 RTPLAN：48 个有效能量层、43,919 个 spot，能量范围 203.67–379.73 MeV/u；输出每层能量、spot 坐标、FWHM、meterset 权重和统计图。
- 患者几何：等中心 `[-71.7, -220.0, -1134.0] mm`；TOPAS 已读取 512×512×199 的 DICOM CT，体素 1.171875×1.171875×1.5 mm，建立 2,845 种材料。`External+1mm` 通过 `RTROIInterpretedType=EXTERNAL` 自动识别，不再要求 ROI 精确命名为 `External`。
- 剂量网格：TOPAS `DoseToMedium` 评分网格与当前 TPS RPPD 完全一致，数组形状 `[Z,Y,X]=[152,154,185]`，体素 2 mm。
- 完整计划：43,919 个 spot 全部写入 TOPAS；原有 RTPLAN baseline 保留。新增的 machine-commissioned 模式已导入 `hzRoom1_90_RF4_250701` 束流数据，48 个当前能量层均与能谱/相空间表精确匹配；history 按 `meterset × energy-dependent number-per-MU` 确定性分配且每个正权重 spot 至少 1 个。
- TOPAS 预检：使用 TOPAS 4.2.p3 完整解析 CT、材料和 spot 参数链；零历史评分网格 4,330,480 个 float64 数值、位置、间距和数组重排均与 RPPD 一致。两次 TOPAS 初始化在当前机器上各约 6 秒。
- 历史 MC QA：旧水模曾完成 150,000-history 计算，但其 DICOM、56,349 spots 和 [153,152,201] 网格均不属于当前患者 CT 病例；相关文件已归档，不作为当前结果。
- GUI：可初始化新案例、逐步运行、一键执行准备阶段、查看实时日志和计算进度、运行零历史预检、启动/停止 TOPAS、浏览三个方向图并导出 CSV；`Machine models` 页面支持标准 ZIP 包的只读检查、不可变导入、版本选择和停用。

当前新病例的 100,000-history commissioned 生产计算已完成，并生成 34,643,840 字节的 `DoseToMedium` 网格。按 `N_plan/N_sim = 9,566,797,062.84/100,000` 得到独立标定系数 95,667.9706284；重新输出的 3%/3 mm、10% TPS 阈值全局 3D Gamma 通过率为 99.9640%（477,581/477,753）。该结果仍为单 spot 低统计研究结果，不能把孤立最大体素作为输出准确性指标。

> 2026-08-22 核实：2026-08-21 那次运行与其前一次（2026-08-20）的输出 SHA-256 完全相同——两次都用 seed 1699 + 64 threads，Geant4 MT 在固定种子和线程数下可复现。因此缓存中的 Gamma、三方向曲线和 MC RTDOSE 对当前 production binary **有效，无需重跑**；同时也说明"再跑一次相同配置"不会产生独立样本。要获得独立统计必须更换 seed。详见 `PROJECT_STATUS.md` 与 `CLAUDE_PROJECT_HANDOFF.md` 第 2 节。

## 2. GUI 启动

macOS 可直接双击项目根目录中的：

```text
Launch TPS-TOPAS GUI.command
```

也可在终端运行：

```bash
cd /Users/jiangzhenmin/Desktop/PLAN1699
./.venv/bin/python launch_gui.py
```

双击后会启动仅监听 `127.0.0.1` 的本地网页 GUI 并打开浏览器；DICOM 和剂量数据不会上传。关闭启动终端窗口即可关闭 GUI 服务。GUI 中所有图表均调用无界面的批处理绘图脚本生成，图题、坐标轴和图例全部为英文。界面本身使用英文技术名称，以便日志和输出文件一一对应。

**刷新或关闭网页不会中断正在进行的计算。** 计算跑在 GUI 服务器进程里（后台线程 + `start_new_session=True` 的独立进程组），和浏览器连接无关；随时重新打开 `http://127.0.0.1:8765/` 即可挂回去。重载后页面会自动从服务端恢复那次任务的 histories / threads / seed / output tag / 束流设置 / 能量层选择，并在任务结束前冻结这些输入框，避免重载后的表单与在跑的计算不一致；日志和进度也会完整重放。真正会终止计算的只有两件事：点击 `Stop current task`，或关掉启动 GUI 的那个终端窗口。

`SSH server` 页默认关闭，打开、刷新或保存设置都不会上传文件、启动服务器计算，也不会影响本机正在运行的 TOPAS。用户可以填写直接主机/IP与用户名，也可以使用 `~/.ssh/config` 中的 OpenSSH alias；认证采用 ssh-agent/macOS Keychain，或仅记录用户选择的现有私钥路径，项目不保存密码和私钥内容。保存服务器 TOPAS、Geant4 setup、Geant4 data 与远程工作目录后，先检查主机公钥，通过独立渠道核验页面显示的 SHA-256 指纹并明确点击信任，程序才会把精确公钥写入 `config/ssh_known_hosts`。之后依次执行连接测试和服务器环境检查。若主机或端口改变，旧指纹自动失效；历史 bundle 保持不可变。

服务器工作流只把正式粒子输运移到远端：阶段 1–7 仍在本机解析 DICOM、选择机器模型并生成 TOPAS 参数。`Prepare bundle` 在当前患者/计划/输出标签的标准化 analysis 缓存下生成不可变 bundle；CT 按目录内容 SHA-256 上传到服务器共享缓存，bundle 中的 TOPAS 参数副本改写为该服务器 CT 路径。本机活动 `topas/geometry/patient.txt` 不会改变。bundle 内四个脚本依次负责上传、提交、查看状态和下载；远程 launcher 会 source 固定的 Geant4 环境脚本，并只调用服务器配置中的 TOPAS executable。任何本机 TOPAS/Geant4 executable、RTPLAN、RTDOSE、RTSTRUCT 均不会上传。下载后仍需在本机做输出尺寸/header 校验、独立粒子数标定、profile、Gamma 和 MC RTDOSE 导出。

`Run parameters` 右上角的 `Reset defaults` 可将 histories、threads、seed、剖面深度、Gamma DTA/DD、束流来源、手动 Energy/spot、输出标签、TOPAS 路径和结果视图恢复为初始值，并重新选择当前病例可发现的最新 MC 文件及默认 `PLAN / PHYSICAL` TPS RTDOSE。该操作不会更改当前病例目录，也不会删除或覆盖已导入 DICOM、日志、TOPAS 输出及分析结果。

`Beam Energy + spot source` 提供互斥的两种来源。默认的 `Use RTPLAN Energy + spots` 使用 RTPLAN 的能量层、spot IEC X/Y 和 delivery sequence；`Set Energy + one spot manually` 用用户输入生成单能量、单 spot 研究束流。其下的 `TOPAS beam model` 再选择 `RTPLAN baseline (uncommissioned)` 或 `Machine commissioned (IDD + emittance + VSAD)`。commissioned 模式只有在 RTPLAN `TreatmentMachineName` 精确匹配、DICOM X/Y VSAD 位于标定容差内、所选能量处于全部标定表范围内且文件 SHA-256 未变化时才可生成；否则直接阻断。手动模式仍使用当前 RTPLAN 的束流几何和等中心，不能当作 TPS 完整计划重建。模式和所有数值都会写入生成摘要、history allocation 和患者结果缓存。

这里的 SHA-256 gate 同时覆盖派生的能谱/相空间/NF 表和原始 IDD、spot-sigma、能量清单证据。加载器还会把每一个 Fermi–Eyges 状态传播回实测平面；证据文件变化、深度轴约定错误或实测 sigma 重建误差超过 profile 阈值都会阻止模型启用。

当前 commissioned 模型来自 `TOPAS_Test` 的杭州 RF4 数据，接入方法为：实测水中 pristine IDD 经 NNLS 拟合得到每个标称能量的离散总能谱；实测 spot sigma–depth 经 Fermi–Eyges 拟合得到 680 mm 上游平面的 `Sigma/SigmaPrime/Correlation`；RTPLAN `VirtualSourceAxisDistances` 用于把每个 isocenter spot 反投到该源平面并生成转角；能量相关 `NF.txt` 用于相对注量。每个能量层在同一次 TOPAS session 中使用独立 `Emittance` source，剂量仍在同一 TPS 网格累计。复核参考代码时发现其 Fermi–Eyges 输入/输出明确为 mm，但旧生成器把位置 sigma 标成 cm；本项目已按 mm 写入，避免 10 倍横向宽度错误，并把极小数值误差导致的相关系数越界截断到 TOPAS 可接受范围。实测 IDD 已包含上游喷嘴能损，因此程序不再叠加单独 nozzle WET，防止双重计算。

当前导入模型的相空间审计覆盖 122 个能量。按参考项目记录的反向深度约定重建实测 spot sigma，中位/最大 RMSE 为 0.0018/0.0455 mm，最大等中心 sigma 误差为 0.0454 mm；profile 的阻断阈值为 0.25 mm。

`Advanced beam overrides` 仅用于 baseline 研究和设备标定。启用后可设置标称能量比例、能量偏移、spot FWHM 比例和 TOPAS 能散。commissioned 模式会禁用这些缩放，因为任意改动都会破坏能谱、相空间和注量表之间的标定一致性；若要修改必须形成新的 commissioning profile 并重新验收。

阶段 3 完成后，`Energy layers` 会列出 RTPLAN 中每一个 LayerIndex、标称 MeV/u 和 spot 数。默认全选；可逐层点击，或使用 `Select all / Clear`。选择子集会强制阶段 6–8 重建并在生成摘要中记录 `Selected LayerIndex values`。子集运行用于单能层/部分能层研究，不是完整 TPS 计划重建，不能与完整计划剂量直接作临床结论。

## 3. 更换新 TPS 计划的标准流程

### 3.1 新建隔离案例

建议每个 TPS 计划使用独立目录，不要在已有案例中直接覆盖旧 DICOM 和 MC 结果。

1. 启动 GUI。
2. 点击 `Choose / create case`，在 macOS 文件夹选择器中选择一个新的空目录；GUI 会自动初始化缺失目录和模板文件，不覆盖已有文件。
3. 在 `Import TPS DICOM` 区域分别点击 `CT`、`RTPLAN`、`RTDOSE`、`RTSTRUCT`：CT 选择包含切片的文件夹，RTDOSE 可多选，RTPLAN 和 RTSTRUCT 单选。

GUI 会读取并校验每个文件的 DICOM `Modality`；同批 CT/RTDOSE 不能混入不同 Study 或 Frame of Reference。普通追加导入仍必须与已有类别匹配；用户明确确认替换时，允许依次导入新患者的四类 DICOM，此时中间状态会保持 `WAITING`，直到所有类别完成替换和阶段 1–2 检查。整批验证成功后才会写入：

```text
dicom/CT/*.dcm
dicom/RTPLAN/*.dcm
dicom/RTDOSE/*.dcm
dicom/RTSTRUCT/*.dcm
```

RTDOSE 中必须能唯一识别 `DoseUnits=GY`、`DoseType=PHYSICAL`、`DoseSummationType=PLAN` 的计划物理剂量，作为 TOPAS 的固定评分网格和默认 TPS reference。其他引用当前 RTPLAN、且患者/Study/Frame 一致的 GY/CGY RTDOSE（例如 `PLAN/EFFECTIVE` 或 `BEAM/PHYSICAL`）也会出现在 Results 选择器中，但不会改变 TOPAS 物理剂量评分网格。

若某一类别已有文件，GUI 会要求确认。确认后旧文件先移动到 `dicom_archive/<时间戳>/<类别>/`，再提交新文件，因此不会直接删除原始 DICOM。检测到不同 Study 或不同 RTPLAN SOP UID 时，程序会先把旧患者的 GUI 参数快照和现有 TOPAS 生产结果写入旧患者的标准化 `analysis/.../run-.../topas_runs/` 缓存，然后把 histories、threads、seed、beam override、能量层选择、Gamma和结果视图恢复默认，并清空当前 MC 路径。导入后仍需运行阶段 1 和阶段 2，完成完整 UID 引用及几何一致性检查。

每次导入都会在 `dicom/import_history/` 生成英文 CSV 审计记录，包括原始文件名、活动文件名、SOP/Study/Frame UID、字节数、SHA-256 和旧文件归档位置。

### 3.2 点击准备阶段

点击 `Run stages 1–7`，GUI 会依次执行：

1. `DICOM geometry check`：检查 UID 引用、Frame of Reference、CT/RPDOSE 网格、等中心和范围。
2. `Compatibility gate`：确认新计划位于当前已实现的物理和坐标变换范围内。
3. `Parse RT Ion Plan`：生成 `energy_layers.csv`、`spots.csv` 和英文汇报图。
4. `Generate case geometry`：水模模式由 RTSTRUCT External 生成 G4_WATER box；真实 CT 模式生成 `TsDicomPatient`。baseline 使用患者外部的安全模拟源平面；commissioned 使用 profile 记录的 680 mm 上游相空间平面并校验患者净空。
5. `Build TPS dose grid`：在 TOPAS 中建立与 TPS RPPD 完全相同的并行 DoseToMedium 网格。
6. `Generate full spot plan`：baseline 按相对 meterset 权重；commissioned 按 `meterset × number-per-MU(E)` 分配 histories。commissioned 为每个能量层写入离散能谱、BiGaussian emittance 和 VSAD spot 轴，并保存逐 spot 几何/注量审计列。
7. `Prepare TOPAS run`：生成线程数、随机种子、物理列表、入口文件和唯一的病例输出名。

每个派生文件都有时间戳检查。更换 DICOM 后，旧的解析、几何、预检或 MC 结果会在 GUI 中显示为 `WAITING`，不能被当成当前计划的就绪结果。

### 3.3 TOPAS 零历史预检

点击 `TOPAS preflight`。它会：

- 解析全部能量层和 spot，但不输运粒子；
- 初始化完整 `DoseToMedium` scorer；
- 输出零历史二进制；
- 自动验证 TOPAS/TPS 的网格尺寸、间距、放置、数据类型和数组重排方式。

水模预检通常很快；当前 512×512×199 患者 CT 的两次 TOPAS 初始化各约 6 秒。正式运行按钮要求准备阶段和预检均为当前版本的 `READY`。

### 3.4 正式计算

点击 `Run TOPAS` 后，GUI 会先显示计算方案选择窗口。当前预设为：

| 方案 | Histories | Threads | 基准预估时间 |
| --- | ---: | ---: | ---: |
| Quick diagnostic | 100,000 | 4 | 约 1.3 h（粗略范围 0.9–2.2 h） |
| Balanced QA | 500,000 | 6 | 约 4.5 h（粗略范围 3.4–7.9 h） |
| Recommended | 1,000,000 | 8 | 约 7.0 h（粗略范围 5.3–12.3 h） |
| Higher statistics | 2,000,000 | 12 | 约 9.7 h（粗略范围 7.3–17.0 h） |

预设的 threads 由 `min(4/6/8/12, 逻辑核数)` 得到；本机 15 核，因此显示值即 4/6/8/12。

> 上表的预估时间来自旧的水模回退基准，与当前病例的实测有明显偏差：`Quick diagnostic` 这一档
> （100,000 histories / 4 threads）在当前患者 CT 上实测为 **8,166.35 s ≈ 2.27 h**，而非表中的 1.3 h。
> 500k / 1M / 2M 三档目前**没有实测数据**，其时间应视为未经验证的外推。估计器本身的重写见
> `OPTIMIZATION_REPORT.md` 优化项 5。

也可选择 `Custom` 自行输入 histories 和 threads，界面会即时更新预估。预估以 PLAN1699 水模 150,000 histories、4 threads、6,831.7 秒的实测为回退基准；若当前病例已有完成运行，估计器会优先使用相同病例/模式/spot 数的实测日志。患者 CT、能量分布、系统负载和散热会使实际耗时变化，因此显示中心值和较宽的粗略范围。

**线程数实测（重要）**：同一 43,919-spot / 100,000-history 全计划，在 15 逻辑核 / 24 GB 的机器上——

| 请求线程 | Real | User | Sys |
|---:|---:|---:|---:|
| 4 | 8,166.35 s | 15,753.9 s | 69.4 s |
| 64 | 11,541.4 s | 19,999.4 s | 1,017.2 s |
| 64 | 17,547.6 s | 20,505.2 s | 1,211.3 s |

请求 64 线程比请求 4 线程**慢 1.4–2.1 倍**，内核时间高约 15 倍，且相同配置两次相差 1.5 倍。
原因是 `Tf/NumberOfSequentialTimes = 43919`——每个 spot 一次 Geant4 Run，每次 Run 只有 1–8 个 history，
线程越多每次 Run 的同步开销越大。**请把请求线程数控制在物理核数以内。**

同一原因还带来一个有利结论：wall time 由每-spot 固定开销（4 线程时约 0.186 s/spot）主导，与 histories
数量基本无关，因此提高 histories 的边际成本远低于线性——这是目前改善低统计噪声最便宜的途径。

计算参数包括：

- `Histories`：当前项目默认 100,000。**建议不低于所选 spot 数**（当前计划 43,919），否则无法给每个正权重 spot 至少分配 1 个 primary。
  自 2026-08-23 起这不再是硬性拒绝：低于该值时按权重排序，只有最重的前 N 个 spot 各拿 1 个 primary，其余 spot **整个从 TOPAS 时间轴上删除**。
  删除是关键——一个 0 history 的 spot 仍然要付一次完整的 Geant4 Run（约 0.186 s/spot），不删的话「少给 histories」根本不会更快。
  这样 10,000 histories 的试跑约 31 分钟而不是 2.3 小时，适合做几何/射程/流程 sanity check。
  **但结果不是计划剂量**：大片区域完全没有剂量，绝对粒子数标定、Gamma 和 TPS profile 对比对这种运行一律无效。
  生成摘要会写入 `Run class: SPARSE TEST RUN (NOT A PLAN DOSE)` 和覆盖率，运行前 GUI 也会单独弹一次确认。
- `Threads`：不要超过本机物理核数。预设方案已按 `min(6/8/12, 逻辑核数)` 收敛；自 2026-08-22 起自定义输入框也按本机逻辑核数限制——前端直接拒绝超限提交，后端 `clamp_threads()` 再兜底收敛，队列旧快照重试时同样收敛。当前病例最近一次运行请求的是 64，属于过度订阅（历史记录，不再可能重现）。
- `Random seed`：默认 1699。**注意：seed 与 threads 都不变时，Geant4 MT 会逐字节重现上一次结果。** 需要独立统计样本时必须更换 seed。
- `Beam Energy + spot source`：默认使用 RTPLAN；也可切换为手动单 Energy、单 spot 研究束流。
- `TOPAS beam model`：baseline 用于回退/A-B 对照；commissioned 用于当前精确匹配的杭州 RF4 机器。切换模型会自动重建阶段 4、6–8。
- `Energy layers`：默认使用全部 RTPLAN 能量层；可以点击选择一个或多个 LayerIndex。选择为空时禁止生成，选择子集时界面会二次确认。

选择方案并确认后，如果 histories、threads、seed、束流来源、束流模型、Energy/spot参数或能量层选择与当前准备结果不同，GUI 会自动重新执行阶段 4、6–8：源平面几何、spot/history与commissioned source、运行入口和零历史 preflight；通过后才启动正式输运。日志实时显示在 Workflow 左列、`Preparation and calculation` 下方的 `Commands and live output` 面板。

任务运行时，`Pause task` 使用 POSIX `SIGSTOP` 暂停独立的任务进程组，TOPAS及其子进程的内存状态和当前输出文件保持不变；按钮随后切换为 `Resume task`，点击后用 `SIGCONT` 从同一进程状态继续。暂停不会归档或重建任务，也不会把已完成histories清零。它不是磁盘检查点：暂停期间必须保持GUI进程和电脑运行，不能关闭GUI、重启或关机。暂停状态下仍可使用 `Stop current task` 终止任务。

生产 scorer 仍采用 TOPAS `Exit` 防碰撞策略，但用户不再需要手工移动旧 `.bin`。Step 7 使用 `--overwrite` 或确认启动新输运时，程序先将已有生产 `.bin`、`.binheader`、spot allocation、生成计划、阶段 7 摘要和最新运行日志移动/复制到当前患者的 `topas_runs/archived-<timestamp>/`，更新 `manifest.json` 后再准备空的生产输出路径。旧剂量不会被静默覆盖或删除。

### 3.5 三方向分析与 CSV

计算完成后，GUI 会自动选择生产 `.bin`。也可在 `MC binary` 中输入已有结果路径，或点击 `Use latest` 自动发现最新结果，并设置：

- `TPS RTDOSE`：列出当前患者、Study、Frame 和 RTPLAN 下全部可用 GY/CGY RTDOSE，显示文件名、`DoseSummationType`、`DoseType`、单位和 beam number；默认选择 `PLAN / PHYSICAL`；
- `Profile depth (mm)`：横向 X/Y 剖面相对束流入射体表的深度；默认 100 mm。患者 CT 的入射点来自等中心 Y/Z 处 External 与 +X 束流轴的交点。
- `Output tag`：输出文件后缀，只允许字母、数字、下划线和连字符。

点击 `Export profiles`，得到：

```text
analysis/patient-<patient-id>--study-<hash>/plan-<plan-label>--<hash>/run-<tag>/figures/depth_direction_<tag>.png
analysis/patient-<patient-id>--study-<hash>/plan-<plan-label>--<hash>/run-<tag>/figures/transverse_x_<tag>.png
analysis/patient-<patient-id>--study-<hash>/plan-<plan-label>--<hash>/run-<tag>/figures/transverse_y_<tag>.png
analysis/patient-<patient-id>--study-<hash>/plan-<plan-label>--<hash>/run-<tag>/profiles/depth_dose_<tag>.csv
analysis/patient-<patient-id>--study-<hash>/plan-<plan-label>--<hash>/run-<tag>/profiles/transverse_profile_x_<tag>.csv
analysis/patient-<patient-id>--study-<hash>/plan-<plan-label>--<hash>/run-<tag>/profiles/transverse_profile_y_<tag>.csv
analysis/patient-<patient-id>--study-<hash>/plan-<plan-label>--<hash>/run-<tag>/profiles/profile_export_summary_<tag>.txt
```

深度 CSV 同时保留 TPS Gy、粒子数标定后的 MC Gy、平滑值、独立归一化值、DICOM patient X 和 `Depth_from_beam_entry_mm`。横向 CSV 保留 IEC 相对坐标与 DICOM patient 坐标，便于排查坐标变换。

### 3.6 交互式 Line Dose

进入 `Results` 后默认打开 `Interactive line dose`。该功能由独立 RT Line Dose Viewer 的规则网格几何和三线性插值方法适配而来，并针对本项目增加 TOPAS `DoseToMedium` 层：

- 顶部 `TPS RTDOSE` 下拉菜单可切换当前 RTPLAN 下的不同 RTDOSE；Line Dose 立即重载，图中状态和曲线标签显示所选 PLAN/BEAM、PHYSICAL/EFFECTIVE 类型和文件名；
- `Axial / Coronal / Sagittal` 可切换三个正交剂量断面，滑块选择具体切片；
- `TPS / MC (TOPAS) / MC − TPS difference` 可切换图像层；
- RTPLAN 等中心投影为黄金十字中心线；首次打开和切换平面自动选择最接近等中心的切片，`Go to isocenter` 可随时复位；
- 在剂量图上拖动 A→B 即可定义任意直线，后端按 DICOM patient XYZ 坐标在原始三维网格上插值；
- TPS 与 MC 曲线同时显示，悬停可读取距离、XYZ 和两条剂量值；
- 统计区域输出最大值及位置、均值和近似 FWHM；
- `Export line CSV` 输出每个采样点的距离、patient XYZ、TPS Gy、粒子数标定 MC Gy、legacy 峰值拟合值、显示值和网格内标记，同时写入当前患者/计划/run缓存。

commissioned 结果默认显示 `Particle-calibrated Gy`：程序先按 RTPLAN `TreatmentMachineName` 唯一匹配机器 profile，再校验该机器目录中的 `particle_calibration.json`、profile 指纹及 `number_per_mu.txt` 哈希。程序读取本次运行的 `spot_history_allocation.csv`，逐 spot 验证 `AllocationBasis=MU_i×NF_machine(E_i)`，计算 `N_plan=sum(MU_i×NF_machine(E_i))`、`N_sim=sum(AllocatedHistories_i)`，然后只对原始 TOPAS 网格应用一次 `N_plan/N_sim×C_machine`。当前机器 `C_machine=1`（不应用经验修正）；TPS 剂量不参与该系数的确定。`Independent global max (%)` 和 `Legacy TPS-peak fit` 仍可用于形状诊断，但不会用于新的 Gamma 协议。

### 3.7 MC DICOM RTDOSE 导出与直接查看

Results 中可将当前 MC 网格导出为标准 DICOM Part 10 RTDOSE。输出采用 Explicit VR Little Endian、独立 SOP/Series UID、原 Study/Frame of Reference，并引用当前 RTPLAN：

- `Particle-calibrated QA Gy`：默认模式，MC 乘以 commissioned `N_plan/N_sim`；
- `Legacy TPS-peak fit`：MC 再按 TPS 峰值拟合，仅供历史/形状诊断；
- `Raw TOPAS per-run Gy`：保留本次有限模拟的未标定 DoseToMedium 原始值。

粒子数模式的 JSON 审计记录机器名、profile/NF/binding 哈希、`N_plan`、`N_sim`、`C_machine`、总 scale、allocation 快照和协议；`spot_history_allocation_metadata.json` 用 allocation SHA-256 将本次运行锁定到机器标定版本，缓存 TOPAS 结果时也复制完整机器 profile 快照。原始 `.bin` 永不改写，因此缓存结果不会重复缩放。导出前程序按 `ReferencedRTPlanSequence` 唯一锁定当前物理PLAN RTDOSE，并要求其 `PatientID`、`PatientName`、`StudyInstanceUID`、`FrameOfReferenceUID` 与当前RTPLAN一致；写盘后严格重读患者层级、像素、网格、UID引用和量化误差。该模式是独立粒子归一的研究物理剂量估计，不等同于经临床验收的绝对剂量。

`Import MC RTDOSE` 可点击导入与当前TPS RTDOSE患者、Study、网格、方向和Frame of Reference完全一致的MC RTDOSE。导入后无需运行TOPAS即可在Interactive line dose中查看；不一致的RTDOSE会被拒绝。

不能通过只改PatientID把一个病例的MC剂量挂到另一个病例：MC网格、坐标和RTPLAN引用同样属于病例身份的一部分。切换病例时必须通过四个导入按钮完成CT、RTPLAN、RTDOSE、RTSTRUCT整套切换，并为该计划重新生成/计算MC；过渡期间的混合DICOM树不能导出MC RTDOSE。

### 3.8 标准化患者结果缓存

新的分析结果按PatientID、Study UID哈希、RTPlanLabel和RTPLAN SOP UID哈希隔离：

```text
analysis/
  patient-<patient-id>--study-<hash>/
    plan-<plan-label>--<hash>/
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

`manifest.json` 保存病例/计划身份、所选 TPS RTDOSE、MC来源、beam设置、能量层、粒子标定和DICOM导出审计。`calibration/` 保存当前输出的 `N_plan/N_sim` 审计，`topas_runs/` 保存生产二进制/header、运行参数快照、spot allocation、生成计划和日志。GUI启动或切换病例时扫描这些manifest，在 Results 的 `Cached result run` 中列出结果；加载缓存结果时同步恢复它记录的 TPS RTDOSE。旧版直接位于 `analysis/figures|profiles|gamma` 的结果保留为只读回退，不会被删除。

### 3.9 三维 Gamma 分析

GUI 中输入：

- `Gamma DTA (mm)`：用户指定的 distance-to-agreement；
- `Gamma DD (%)`：用户指定的全局 dose-difference 百分比；
- `MC binary` 和 `Output tag`：与曲线导出共用。

点击 `Gamma analysis` 后执行全局三维 Gamma：当前选择的 TPS RTDOSE 为 reference，TOPAS `DoseToMedium` 为 evaluation，`Gamma <= 1` 判为通过。默认 reference 仍是 `PLAN/PHYSICAL`。若选择 `PLAN/EFFECTIVE` 或 `BEAM/PHYSICAL`，GUI 会二次警告并将结果标记为诊断/研究比较，不能当作标准 TOPAS 物理计划剂量验证。当前协议固定排除低于所选 TPS 最大剂量 10% 的体素，并对 MC 做三线性插值。

新协议要求 commissioned allocation，并按 `N_plan/N_sim` 对 MC 做独立粒子数标定；TPS 剂量不参与 scale。若运行是 baseline、手动 spot、allocation 缺失/过期，Gamma 会明确停止，不再静默回退到 `TPS_max/MC_max`。GUI 和报告始终同时显示 DD、DTA、10% 阈值、`N_plan`、`N_sim` 和 scale。

输出：

```text
analysis/patient-<patient-id>--study-<hash>/plan-<plan-label>--<hash>/run-<tag>/gamma/gamma_summary_<tag>.txt
analysis/patient-<patient-id>--study-<hash>/plan-<plan-label>--<hash>/run-<tag>/gamma/gamma_metrics_<tag>.csv
analysis/patient-<patient-id>--study-<hash>/plan-<plan-label>--<hash>/run-<tag>/gamma/gamma_map_<tag>.npy
analysis/patient-<patient-id>--study-<hash>/plan-<plan-label>--<hash>/run-<tag>/figures/gamma_map_<tag>.png
analysis/patient-<patient-id>--study-<hash>/plan-<plan-label>--<hash>/run-<tag>/figures/gamma_pass_fail_<tag>.png
```

`gamma_metrics` CSV 包含通过率、通过/失败/评价体素数、判定条件、TPS/MC 峰值、`N_plan`、`N_sim`、独立 scale、allocation L1差异、高剂量区 MC/TPS 中位比、空间搜索点数和计算时间。`.npy` 文件为 `[Z,Y,X]` float32 Gamma 体数据；低剂量阈值外为 `NaN`。当前搜索范围限定在 DTA 半径内，可准确判断 `Gamma <= 1` 的通过率；失败体素数值不作为无界完整 Gamma 分布解释。

## 4. 当前安全门范围

只有满足以下条件的新案例才会进入自动重建：

- 单个碳离子 PBS beam；
- Patient Position 为 HFS；
- Gantry 90°；couch、pitch、roll 均为 0°；
- RPPD 为规则、递增、轴对齐的三维计划物理剂量网格；
- 患者模型满足以下二者之一：
  - CT 为带 phantom/artificial 元数据的均匀 0 HU 数据，且 RTSTRUCT 有轴对齐矩形 External，用作 G4_WATER box；
  - CT 为规则轴向切片，使用 TOPAS `TsDicomPatient` 和项目内 Schneider HU–材料表；
- External 优先按 DICOM `RTROIInterpretedType=EXTERNAL` 识别，并兼容 `External+1mm`、`Body` 等常见名字，不再依赖精确字符串。

不符合以上范围时返回 `BLOCK`，而不是猜测坐标变换或材料。真实 CT 若使用项目自带的通用 Schneider 表会返回 `WARN`；只有导入并审核本机构/扫描协议标定后，才可讨论临床材料与射程准确性。检测到 MRF4/其他范围调制器但没有已标定几何和 WET 时也返回 `WARN`；当前仍可进行“无物理 MRF4 的研究基线形状 QA”，但不可据此做临床剂量结论。

## 5. 结果应如何解释

当前可用于：

- DICOM 引用和坐标一致性检查；
- RTPLAN 能量层、spot、FWHM 和相对权重审计；
- TOPAS 评分网格与 TPS RPPD 的严格对齐验证；
- 在明确限制下比较独立归一化的深度范围、横向场宽和整体形状；
- 定位坐标轴、符号、等中心、入口深度或明显束流范围错误。

当前不可用于：

- 绝对剂量或 MU 一致性；
- RBE/生物剂量验证；
- 临床通过/失败判断；
- 使用通用 Schneider 表对患者异质性输运作临床准确性结论；
- 未标定范围调制器的剂量学结论。

## 6. 仍需完善的工作（按优先级）

### P0：机器模型标定

1. 导入本机构、对应 CT 扫描协议的 HU–质量密度/材料/相对阻止本领标定，替换并验证通用 Schneider 表。
2. 实现 MRF4 的 CAD/周期轮廓、材料、密度、安装位置和方向，并用 WET/IDD 实测验证。
3. 已接入当前杭州 RF4 机器的实测 IDD 离散能谱；下一步需用独立测量/不同计划复核能量—射程和谱形，并为 profile 建立正式版本/审批记录。
4. 已接入当前机器的 Fermi–Eyges 相空间与 VSAD spot 轴；下一步需用多个能量、多个 air gap 的 X/Y profile 做独立验收，并量化模型不确定度。
5. 已按 TOPAS_Test 方法把 energy-dependent number-per-MU 同时用于 spot 注量和 `N_plan/N_sim` 后处理，形成不依赖 TPS 峰值的粒子数标定；仍需 monitor chamber 可追溯性、独立剂量测量和端到端验收。

### P1：定量验证

1. 导入实际测量的深度剂量和 X/Y profile，形成 TPS/measurement/MC 三曲线。
2. 增加多随机种子或批次统计、不确定度条和收敛检查。
3. 在模型标定后增加 R80/R50、distal falloff、FWHM、penumbra、中心轴偏移等指标，并为当前 3D Gamma 预先定义临床接受标准。
4. 已明确三种显示协议并将 commissioned `N_plan/N_sim` 设为剂量/Gamma默认；后续应增加多随机种子不确定度，并评估低 history spot 分配偏差。

### P2：扩大病例范围与工程化

1. 支持任意 gantry/couch/patient position 的完整 IEC 61217 变换和多 beam 汇总。
2. 支持倾斜/非轴向 CT、完整 DICOM 方向余弦、床板/固定装置和 ROI 材料覆盖。
3. 继续完善 GUI 中的测量数据映射、跨程序退出的安全进程重连和一键 PDF 报告；当前已实现实时进度/ETA、病例结果归档以及运行期间的任务暂停/恢复。
4. 为所有已支持的 DICOM 变体建立自动化回归测试案例。

即使 commissioned 模式通过软件预检，在完成独立端到端验收、CT 标定、MRF4 残余散射/碎裂验证和绝对剂量标定之前，本工具仍应标注为研究/QA 重建工具，而不是临床剂量验证系统。

## 7. 多病例批处理队列

GUI 的 `Batch queue` 可通过 `Choose / create case` 逐个选择或新建病例目录，并在该页面分别导入 CT、RTPLAN、RTDOSE、RTSTRUCT；此操作不切换 Workflow 当前病例。四类 DICOM 都就绪后加入队列，再重复上述操作即可加入任意数量计划。加入队列时会为该条目固化 histories、threads、seed、束流模式、输出标签和其他运行参数；批处理导入的病例固定使用其自身 RTPLAN 的全部能量层，避免把当前病例的 layer index 错套到新计划。

- 每条队列任务依次执行 **阶段 1–7 准备 → 零 history 预检 → TOPAS 输运 → 三方向 profile 导出 → Gamma 分析**（自 2026-08-23 起；此前在输运结束后就停下，profile 和 Gamma 需要手工补跑）。
- 输运之后的两个阶段只读取刚写出的剂量文件，因此**它们失败不会丢弃计算结果**：任务标记为 `completed_with_warnings`（琥珀色），剂量二进制仍然有效，Retry 或在 Workflow 页手工重跑分析即可。输运本身失败仍然是硬失败（`failed`），且不会继续跑分析。
- 队列快照来自 Workflow 表单，其中 `mc_binary` 和 `tps_dose_uid` 指向当时打开的那个病例。执行时会清空这两个跨病例字段并在日志中记录，让每条任务只分析自己的输出——否则会静默地拿别的病人的剂量做对比。
- 选择病例目录时会做结构检查（自 2026-08-23）：直接含 `.dcm` 文件的目录、位于另一个病例内部的目录、以及项目自身的功能目录（`gui`/`scripts`/`topas`/`analysis` 等）都会被拒绝并说明原因。结构正常的病例目录不受影响，无论它在不在项目 `dicom/` 下。
- 未导入 RTPLAN 和 CT 的空目录不再会生成 `patient-anonymous` 缓存。此前这类占位身份会把不同计划的结果收进同一棵目录树。
- `Local parallel jobs` 只能选择 1 或 2；相同病例目录不会同时运行两条任务。
- `Start / auto-run` 填满可用槽位，任务完成或失败后自动调度下一条等待任务。
- `Stop scheduling` 只停止启动新任务，不向任何正在运行的 TOPAS 进程发送信号。
- 每个运行条目可独立 Pause、Resume、Cancel、Retry；取消和移除队列记录均不删除病例文件或已有结果。
- 队列索引保存在 `analysis/_batch_queue/queue.json`，每条日志保存在该病例的 `analysis/_batch_queue/job-<ID>/run.log`。
- GUI 异常退出时，原 `running/paused` 条目会标为 `interrupted`，不会在无法确认孤儿 TOPAS 进程的情况下自动重复计算；核查系统进程后使用 Retry。

Results 页可以对选中的标准化 cached run 执行 `Delete cached`。该操作不删除 DICOM、当前 production dose 或机器模型，而是把整个 `run-<tag>` 原子移动到病例内的 `analysis/_trash/<patient>/<plan>/` 并写入 `deletion.json`。若共享队列记录显示该病例处于 queued、running、paused 或 cancelling，删除请求会被拒绝，避免影响当前或即将开始的计算。

同时运行两个病例会叠加两条任务各自设置的 TOPAS threads 和内存需求。若单病例已经接近本机 CPU 或内存上限，应保持单槽运行；双槽适合较低 threads 或经过资源验证的病例。

## 8. Machine models 标准接口

GUI 的 `Machine models` 页接收标准 ZIP 包。`Inspect package` 对临时目录执行 schema、完整文件清单、每个文件 SHA-256、单位、版本、来源和审批记录检查，不写入 `machine_model`。束流包还实例化完整 `CommissionedBeamModel`，检查 `particle_calibration.json` 绑定，并对当前 RTPLAN 逐项校验 `TreatmentMachineName`、X/Y VSAD 和所有能量的离散谱、相空间、number-per-MU 覆盖。检查结果按 `PASS/WARN/BLOCK` 展示；存在任何 `BLOCK` 时不能导入。

用户确认 `Import model` 后程序会重新检查包内容，并按“机器/资产 ID + package version + 内容指纹”写入不可变目录，同时记录 `machine_model/model_registry.json` 和 `package_import.json`。不会覆盖同名版本。同一 RTPLAN 机器只有一个活动 profile 时可自动选择；存在多个活动版本时，Workflow 的 `Commissioned version` 必须明确选择，所选 profile 路径会进入批处理设置快照、阶段 4/6 的命令参数和运行生成审计。停用只改变注册表状态，不删除内容；历史 manifest 引用数会显示在页面中。模型导入、停用和重新启用在任一计算处于 running/paused/cancelling 时一律阻断。

标准包格式和 manifest 模板见 `machine_model/MACHINE_MODEL_PACKAGE.md` 与 `machine_model/package_templates/beam_machine_package.template.json`。束流包包含 profile、粒子标定绑定、离散能谱、Fermi–Eyges 相空间、number-per-MU、实测 IDD、实测 spot sigma 和 commissioned energy list。以下三类数据采用独立 package kind，不能混入 beam profile：

- `ct_calibration`：扫描仪/扫描协议 HU–material/RSP；
- `nozzle_geometry`：MRF/nozzle 几何和 WET；
- `absolute_output_calibration`：绝对输出修正及测量证据。

目前这三类独立资产的接口负责不可变保存、哈希和审批审计，导入本身不会静默改变 TOPAS 计算。只有在后续建立相应的专用计算绑定并明确选择后，才允许影响 HU 转换、喷嘴输运或绝对剂量比例。

## 9. 命令行等价流程

GUI 每个按钮都有可审计的命令行等价操作：

```bash
./.venv/bin/python scripts/02_check_dicom_geometry.py --root .
./.venv/bin/python scripts/07_validate_case_compatibility.py --root . --overwrite
./.venv/bin/python scripts/01_parse_ion_plan.py --root . --overwrite
# 首次接入匹配机器时导入一次；更换机器必须导入其独立标定模型
./.venv/bin/python scripts/13_import_topas_test_beam_model.py --root . \
  --source-root /Users/jiangzhenmin/Desktop/TOPAS_Test --overwrite
./.venv/bin/python scripts/08_generate_case_geometry.py --root . \
  --beam-model-mode commissioned --overwrite
./.venv/bin/python scripts/03_build_topas_dose_scoring.py --root . --overwrite
./.venv/bin/python scripts/04_generate_topas_plan.py --root . \
  --beam-model-mode commissioned --total-histories 100000 --overwrite
# --threads 不要超过本机物理核数（`sysctl -n hw.physicalcpu`）；本机为 15。
# --seed 沿用 1699 会逐字节重现已有结果，需要独立样本时请更换。
./.venv/bin/python scripts/09_prepare_topas_run.py --root . --histories 100000 --threads 8 --seed 1699 --overwrite
cd topas
/Users/jiangzhenmin/bin/topas validate_plan_full_parse.txt
/Users/jiangzhenmin/bin/topas validate_dose_grid.txt
../.venv/bin/python ../scripts/03_validate_topas_dose_scoring.py --root .. --overwrite
../.venv/bin/python ../scripts/12_validate_topas_preflight.py --root .. --overwrite
/Users/jiangzhenmin/bin/topas run_full_plan_qa.txt
../.venv/bin/python ../scripts/14_calibrate_mc_dose.py --root .. \
  --mc-binary ../topas_output/production/<case>_DoseToMedium_TPSGrid.bin \
  --output-tag full_plan_100000_commissioned --overwrite
```

正式 TOPAS 输出生成后，再运行：

```bash
./.venv/bin/python scripts/06_export_three_direction_profiles.py \
  --root . \
  --mc-binary topas_output/production/<case>_DoseToMedium_TPSGrid.bin \
  --profile-depth-mm 100 \
  --output-tag full_plan_100000 \
  --mc-label "MC (TOPAS particle-calibrated)" \
  --full-plan \
  --overwrite
```

Gamma 命令行等价操作：

```bash
./.venv/bin/python scripts/11_gamma_analysis.py \
  --root . \
  --mc-binary topas_output/production/<case>_DoseToMedium_TPSGrid.bin \
  --dta-mm 3 \
  --dd-percent 3 \
  --low-dose-threshold-percent 10 \
  --output-tag full_plan_100000 \
  --overwrite
```

## 10. 水模单能单 Spot 验证

用于在**不导入任何 TPS 计划、不生成完整三维剂量网格**的前提下，直接在 TOPAS 中跑均匀水模里指定能量的单个 spot，导出 PDD/IDD、射程指标和横向 profile，与 TPS 或实测 IDD 对比。这是一条与 GUI 主流程完全隔离的命令行通道：不读 CT/RTPLAN/RTSTRUCT/RTDOSE，不写 `topas_output/production/`，不影响任何病例缓存。

### 10.1 为什么单独做一条通道

- **采样步长可以做得很细。** 全计划 QA 每个能量层一次 Geant4 run、并把剂量打进 TPS 网格；水模验证只有一次 run、只有三族严格一维的 scorer，因此把深度步长压到 0.5 mm 甚至更细仍然可承受。
- **每个 scorer 只有一个分 bin 的轴。** IDD 用复现实测探测器口径的圆柱，PDD 用中心轴细圆柱，profile 用选定深度上的薄条。三个方向里另外两个恒为 1 bin，读取时按 sidecar `.binheader` 校验（`scripts/utils/water_phantom.py:read_topas_1d`），所以一个被静默转置的多维数组不可能被当成曲线。
- **束流模型与全计划完全一致。** 同一套 commissioned 离散能谱、Fermi–Eyges 相空间、VSAD spot 轴投影和 energy-dependent number-per-MU；spot 轴投影仍然受 0.01 mm 几何反算门限约束。因此水模结果验证的就是全计划所用的那个模型，而不是另一套简化模型。

### 10.2 两条命令

```bash
# 1) 生成水模输入（只写 TOPAS 参数文件，不跑输运）
./.venv/bin/python scripts/16_generate_water_phantom_spot.py --root . \
  --energy-mevu 240.63 \
  --beam-model-mode commissioned \
  --histories 100000 --threads 4 --seed 1699 \
  --depth-step-mm 0.5 --lateral-step-mm 0.5 \
  --output-tag single_spot_E240p63_100k --overwrite

# 2) 跑输运并导出曲线/指标/图（内部会先调用上一步，可直接只跑这一条）
./.venv/bin/python scripts/17_run_water_phantom_spot.py \
  --energy-mevu 240.63 \
  --histories 100000 --threads 4 --seed 1699 \
  --depth-step-mm 0.5 \
  --output-tag single_spot_E240p63_100k
```

`--energy-mevu` 必须**精确命中**一个 commissioned 能量，不做能量插值。`--threads` 与全计划同规则，受 `gui.runtime_monitor.clamp_threads` 限制在本机核数以内。相同 seed + 相同 threads 逐字节重现。

只重新出图/出表、不重跑输运时加 `--analysis-only`：它既不重新生成参数文件也不调用 TOPAS，直接读上一次输运写下的剂量二进制；与 `--overwrite` 互斥，且当 `.bin` 缺失或为 0 字节时报错而不是产出空曲线。

### 10.3 常用参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--depth-step-mm` | 0.5 | 深度 bin 宽；会被自动吸附成整除水模深度的步长 |
| `--lateral-step-mm` | 0.5 | 横向 profile bin 宽 |
| `--profile-depths-mm` | 由实测 IDD 推导 | 默认取入射区、半峰、Bragg 峰、远端 R80 四个深度 |
| `--idd-radius-mm` | 实测探测器半径 | 复现实测 IDD 的积分口径；方形探测器按等面积圆折算 |
| `--pdd-radius-mm` | 5 | 中心轴 PDD 的取样半径 |
| `--phantom-depth-mm` / `--phantom-lateral-mm` | 由能量推导 / 200 | 水模尺寸 |
| `--surface-distance-mm` | 取自实测曲线，否则 150 | 等中心到水模表面距离 |
| `--meterset-mu` | 无 | 给出后按 `N_plan = MU × NF(E)` 启用绝对剂量比例；不给则只输出相对曲线 |
| `--beam-model-mode` | `commissioned` | `baseline` 仅用于对照，不用于验证结论 |

### 10.4 输出

结果落在 `analysis/_water_phantom/<machine_profile>/<tag>/`：

- `setup/water_phantom_spot_setup.json` — 完整几何、束流、scorer 定义（后续分析的唯一权威来源）
- `topas/{geometry,source,scoring,physics,run_water_phantom_spot}.txt` — 生成的参数文件
- `logs/topas.{stdout,stderr}.log`
- `curves/idd.csv`、`pdd.csv`、`profile_{x,y}_NN.csv`，以及在实测深度点上重采样的 `idd_at_measured_depths.csv`
- `metrics/water_phantom_metrics.json` — R100/R90/R80/R50/R20/R10、proximal R80、80–20 远端半影、峰入比；profile 的矩 sigma、Gaussian 拟合 sigma、FWHM、width_80/20、80–20 penumbra；以及与实测 IDD 的 `idd_vs_measured` 对比块
- `figures/{depth_dose_idd_pdd,transverse_profiles}.png`

矩 sigma 与 Gaussian 拟合 sigma **分别报告、不做合并**：碳离子 spot 在深处有非高斯的碎裂尾，取其一会把这一点掩盖掉。

`idd_vs_measured` 里的一维 global gamma 先把两条曲线各自归一到自身最大值，再把被评估曲线重采样到 0.05 mm 网格，因此距离项不受仿真 bin 宽量化影响；默认 3%/3 mm、10% 阈值。它比较的是**形状与射程**，不是绝对输出。

### 10.5 已有结果（hzRoom1_90_RF4_250701, 240.63 MeV/u）

100,000 histories、4 threads、seed 1699、0.5 mm 深度步长，TOPAS 输运 `Real=1436.99 s`：

- 与 commissioned 实测 IDD 的一维 gamma（3%/3 mm，10% 阈值）：**103 点 100.0% 通过**，max γ 0.359，mean γ 0.216，重叠区间 13–413 mm
- 射程一致性：R80 −0.567 mm，R50 −0.859 mm，R100 −0.735 mm，80–20 远端半影 −0.144 mm
- 剂量差：mean |Δ| 3.52%，max |Δ| 16.92%（集中在 Bragg 峰陡梯度上，故 gamma 仍全通过）
- 浅表深度点（`idd_at_measured_depths.csv`）：−0.12% ~ −0.32%

20,000 histories 的两个目录 `single_spot_E240p63_20k/` 和 `wp_E240.63_20000/`（后者用默认 tag）是**同一次配置的两次独立输运**，结果逐位一致：gamma 同为 100.0%（max γ 0.377，103 点），R80 差同为 −0.561 mm。这与全计划 QA 的可重现性一致——相同 seed + 相同 threads 就是同一次计算。

`single_spot_E240p63_1M/` 只生成了输入、**尚未运行输运**（剂量二进制为 0 字节），按 100k 的耗时线性外推约需 4 小时。

20k → 100k 只把 max γ 从 0.377 降到 0.359、R80 差从 −0.561 变到 −0.567 mm。也就是说在这条通道上，射程/形状结论在 20,000 histories 就已经收敛，提高统计量主要改善的是曲线的逐点噪声，而不是结论本身。

### 10.6 边界

- 目前只有命令行，没有 GUI 入口；这是刻意的隔离决定，见 `scripts/17_run_water_phantom_spot.py` 的模块 docstring。
- 结论仍受第 6 节 P0 未完成项约束：CT 标定、MRF4 几何和绝对剂量标定未完成之前，水模 gamma 通过率只说明**束流模型形状/射程自洽**，不构成临床验收。
- 分析函数与参数文件生成辅助函数由 `tests/test_water_phantom.py` 覆盖（解析式峰的 R80/R50 闭式解、精确高斯的 sigma/FWHM 回收、gamma 的同曲线满分与移位失败、转置数组拒绝、spot 轴投影反算、`--analysis-only` 三个门）。
