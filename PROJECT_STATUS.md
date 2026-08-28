# PLAN1699 Current Project Status

Last synchronized: 2026-08-25 (Asia/Shanghai), verified directly against files on disk.
The 2026-08-22 synchronization of the patient-CT case below is unchanged; the water-phantom
single-spot validation channel added on 2026-08-24/25 is recorded in its own section.

Working directory of this synchronization: `/Users/jiangzhenmin/Desktop/PLAN1699_副本`.
The original `/Users/jiangzhenmin/Desktop/PLAN1699` still exists. Source code contains no
absolute paths — every script derives its root from `Path(__file__).resolve().parents[1]` —
but 83 audit artifacts (manifests, summaries, calibration JSON) correctly record the original
absolute path where they were produced. Those recorded paths are provenance, not configuration,
and must not be rewritten.

Engineering findings and the prioritized fix list are tracked separately in
[OPTIMIZATION_REPORT.md](./OPTIMIZATION_REPORT.md).

## Current case identity

- Active DICOM: 199 CT, 1 RT Ion Plan, 3 RTDOSE, 1 RTSTRUCT.
- Plan: `hzroom1-h-rf4-COM-250916`, Carbon-12 PBS, one HFS/G90/couch0 beam.
- Active layers/spots: 48 / 43,919.
- Energy range: 203.67-379.73 MeV/u.
- Patient model: 512x512x199 axial DICOM CT with generic, uncommissioned Schneider conversion.
- TPS/TOPAS dose grid: `[Z,Y,X]=[152,154,185]`, 2 mm isotropic, 4,330,480 float64 values.

## Current run preparation

- Histories: 100,000. Seed: 1699.
- Threads: the last completed transport requested **64**. On this machine that is a
  significant over-subscription — `hw.logicalcpu = 15` — and it is measurably harmful.
  See "Measured thread behaviour" below. Prepared entry `plan_parsed/topas_run_preparation_summary.txt`
  still records `Threads: 64` (that historical record is left as-is).
  Since 2026-08-22 the requested count is capped at `hw.logicalcpu` on every path into TOPAS
  (`gui/runtime_monitor.clamp_threads`), and `topas/run_full_plan_qa.txt` now carries
  `i:Ts/NumberOfThreads = 12`. See OPTIMIZATION_REPORT.md item 1.
- No TOPAS transport is currently running. Batch queue job `418087a0f0ad` is `completed`
  (started 2026-08-21T13:19:55+08:00, finished 2026-08-21T16:32:44+08:00; TOPAS reported
  Real = 11,541.4 s, User = 19,999.4 s, Sys = 1,017.2 s).
- The current production binary is `topas_output/production/RTDOSE_00003_DoseToMedium_TPSGrid.bin`,
  34,643,840 bytes, SHA-256 `46cc0286045b54c9ce88ba28cd87cb7b6194a6587cf4846e0ac912c656b0b8bb`.
  It is never rewritten; calibration is applied in memory by downstream consumers.
- Current derived geometry and the complete 43,919-spot/48-source input use the 680 mm commissioned source plane.
- Compatibility gate: READY, 13 PASS / 2 WARN / 0 BLOCK (`plan_parsed/compatibility_summary.txt`).
- TOPAS run entry / formal preflight: READY. Future `Run TOPAS` operations rebuild stale preparation
  automatically and create a particle-calibration audit after transport.
- Progress UI: implemented in `gui/web_app.py`; TOPAS runs expose monotonic history/spot progress
  from `Begin processing for Run: N, History: M`.
- Task pause/resume: GUI-launched commands run in an isolated process group; `Pause task`/`Resume task`
  uses SIGSTOP/SIGCONT without discarding process memory or partial TOPAS output. This is an
  in-session pause, not a restart-persistent checkpoint.

## The 2026-08-21 run reproduced an existing result bit-for-bit

This is the single most important fact about the current state, and it is easy to misread.

The transport that finished at 16:32:44 produced a binary whose SHA-256 is **identical** to
`.../run-full_plan_100000_commissioned/topas_runs/archived-20260821T131348_028242/dose/RTDOSE_00003_DoseToMedium_TPSGrid.bin`,
which came from the earlier 2026-08-20 run (log `run_full_plan_qa_20260820_152030.log`, Real = 17,547.6 s).
Both used seed 1699 and 64 threads; Geant4 in MT mode is reproducible for a fixed seed and a fixed
thread count, so the two runs are the same calculation performed twice.

Two consequences:

1. **The cached Gamma, three-direction profiles and MC RTDOSE are valid for the current production
   binary and do not need to be regenerated.** Their file timestamps (2026-08-21 01:06 / 01:07 / 01:14)
   precede the latest run's completion, which looks stale, but they were computed against a
   byte-identical binary. This supersedes the "re-export profiles / re-run Gamma / re-export RTDOSE"
   checklist that previously appeared in `CLAUDE_PROJECT_HANDOFF.md`.
2. **The 2026-08-21 13:20–16:32 run added no new information.** It consumed 3.2 h of wall time to
   recompute a result already on disk. Before launching another transport, change the seed (for
   independent statistics) or the history count (for lower noise); repeating the same seed and thread
   count only reproduces the same numbers.

Two archived dose files are 0 bytes — `archived-20260820T151958_596587` and
`archived-20260821T132000_154803`. These are incomplete/cancelled-run artifacts preserved for audit,
not usable dose.

## Measured thread behaviour

Same 43,919-spot / 100,000-history full plan, same machine (15 logical CPUs, 24 GB):

| Requested threads | Real | User | Sys | Log |
|---:|---:|---:|---:|---|
| 4 | 8,166.35 s | 15,753.9 s | 69.4 s | `run_full_plan_qa_20260818_130551.log` |
| 64 | 11,541.4 s | 19,999.4 s | 1,017.2 s | `run_full_plan_qa_20260821_132008.log` |
| 64 | 17,547.6 s | 20,505.2 s | 1,211.3 s | `run_full_plan_qa_20260820_152030.log` |

Requesting 64 threads on a 15-core machine is 1.4–2.1x slower in wall time than 4 threads, raises
kernel time by roughly 15x, and makes two identical configurations differ by 1.5x. Keep the requested
thread count at or below the physical core count. The GUI presets already do this
(`min(6, 8, 12, logical_cpus)`), and since 2026-08-22 the custom-threads field enforces it too:
the browser rejects anything above `hw.logicalcpu` and the backend clamps whatever still gets
through, so no path can write an over-subscribed `Ts/NumberOfThreads` again. The optimal count
within 4–15 has still not been measured.

`Tf/NumberOfSequentialTimes = 43919` in `topas/beam/plan_generated.txt` means TOPAS executes one
Geant4 run per spot. At 4 threads that is 0.186 s of wall time per spot, largely independent of the
history count, so raising histories costs far less than linearly. This is the cheapest available route
to reducing the low-statistics noise described under "Warnings and interpretation".

## Validation and result-management features

- A selectable commissioned beam model is now available for the exact RTPLAN machine `hzRoom1_90_RF4_250701`. It imports measured-IDD discrete spectra, Fermi-Eyges BiGaussian emittance, DICOM VSAD spot-axis projection and energy-dependent number-per-MU. Every derived table and raw commissioning-evidence file is SHA-256 gated. RTPLAN `TreatmentMachineName` now selects a unique machine profile automatically; `particle_calibration.json` binds profile/NF/output-correction hashes, and every allocation/cache records the exact machine-calibration fingerprint for future multi-machine use.
- The reference generator's phase-space position-sigma unit label was corrected from `cm` to `mm` because both its measured input and fitter output are explicitly millimetres; this avoids a factor-10 transverse source-size error. Correlations are physically clamped only when floating-point fitting puts them infinitesimally outside [-1,1].
- The loader independently propagates every imported phase-space state back through the measurement planes. Across 122 energies, measured spot-sigma reconstruction has 0.0018 mm median RMSE and 0.0455 mm maximum RMSE; a mismatch above the recorded 0.25 mm limits blocks activation.
- All 48 current RTPLAN energies match the commissioned spectra and phase-space table exactly. The active stage-6 input contains all 43,919 spots and 48 commissioned sources with 100,000 histories; every spot receives 1–8 histories, the maximum allocation rounding deviation is 0.517 history and the maximum geometric back-projection error is 0 mm.
- TOPAS 4.2.p3 passed the preflight and completed the full commissioned 100,000-history production calculation. Its independent scale is `N_plan/N_sim = 9,566,797,062.83574 / 100,000 = 95,667.97062835739`. The current calibration audit `.../run-full_plan_100000_commissioned/calibration/mc_dose_calibration_full_plan_100000_commissioned.json` was regenerated at 2026-08-21T16:32:44+08:00 and records `machine_calibration_verified: true`, `machine_dose_output_correction_factor: 1.0`, `allocation_l1_fraction: 0.1104` and `preliminary_low_statistics: true`.
- Reprocessed global 3D Gamma at 3%/3 mm and a 10% TPS threshold is 99.9640% (477,581/477,753 voxels). Median calibrated MC/TPS ratio where TPS is at least 50% of its maximum is 0.9999317. Verified current: the Gamma metrics record `MC_TOPAS_per_run_max_Gy = 4.941871885e-05`, which matches the maximum of the current production binary exactly.

- Interactive line dose supports axial/coronal/sagittal planes, opens on the nearest RTPLAN-isocenter slice, and displays a gold isocenter crosshair for line placement.
- MC `DoseToMedium` can be exported as derived DICOM RTDOSE in particle-calibrated QA Gy, raw per-run or legacy TPS-peak-fit mode. Export is locked to the active RTPLAN reference and verifies PatientID, PatientName, StudyInstanceUID and FrameOfReferenceUID before and after serialization. The default particle mode carries a JSON calibration audit and remains research/non-clinical.
- A compatible same-patient/same-study MC RTDOSE can be imported from the GUI and reviewed with CT/TPS geometry without rerunning TOPAS; foreign-patient objects are rejected even when selected manually.
- Optional research beam overrides are available before stage 6 for energy scale, energy offset, spot-size scale and energy spread. Defaults preserve the DICOM values and overrides force stages 6–8 to rebuild.
- RTPLAN energy layers are individually clickable after stage 3. All 48 are selected by default; any subset is audited and forces stages 6–8 to rebuild.
- Existing production `.bin`/header files are automatically moved into the current standardized patient cache before Step 7 or another transport, rather than being overwritten or blocking the run.
- A different Study/RTPLAN replacement snapshots the previous GUI settings and result, then resets the new patient's run parameters while preserving the old cache.
- New profiles, Gamma results, MC RTDOSE files, line-dose CSV files and archived TOPAS runs are isolated under `analysis/patient-.../plan-.../run-.../manifest.json` and can be loaded again from the Results cache selector.
- The `SSH server` page now lets a user configure either a direct hostname/IP or an OpenSSH alias, username, port, agent/Keychain or an existing identity-file path, server TOPAS/Geant4 paths and job root. It never stores passwords or private-key contents. The page inspects candidate server keys, requires independent SHA-256 fingerprint verification and explicit pinning, then tests the connection and commissioned server runtime. Remote commands remain application-defined.
- Remote transport bundles are immutable and isolated below the current patient/plan/run cache. CT is uploaded to a SHA-256-addressed server cache; only the staged TOPAS parameter tree has its DICOM path rewritten. Generated upload/submit/status/download scripts explicitly source the server Geant4 environment and run the server TOPAS executable. Local TOPAS/Geant4 executables, RTPLAN, RTDOSE and RTSTRUCT are not uploaded.

## Water-phantom single-energy single-spot validation

A separate command-line channel runs one commissioned energy as a single spot in a uniform water
phantom, without importing a TPS plan and without building the three-dimensional dose grid. It is
documented in [WORKFLOW.md](./WORKFLOW.md) section 10 and is deliberately not wired into the GUI.

- `scripts/16_generate_water_phantom_spot.py` writes the TOPAS decks; `scripts/17_run_water_phantom_spot.py`
  runs the transport and exports curves, metrics and figures. Shared physics/analysis code lives in
  `scripts/utils/water_phantom.py`.
- One Geant4 run and three strictly one-dimensional scorer families (IDD in a cylinder reproducing the
  commissioning detector, PDD on the central axis, lateral profiles in thin slabs) make a 0.5 mm depth
  step affordable. Every scorer has exactly one binned axis, cross-checked against the sidecar
  `.binheader`, so a transposed array cannot be read back as a curve.
- The beam model is the same commissioned one used for the full plan: measured-IDD discrete spectra,
  Fermi-Eyges emittance, VSAD spot-axis projection under the same 0.01 mm geometric back-check, and
  energy-dependent number-per-MU when `--meterset-mu` is given.
- Completed runs for machine profile `hzRoom1_90_RF4_250701` at 240.63 MeV/u:
  `single_spot_E240p63_20k/` and `wp_E240.63_20000/` (both 20,000 histories, TOPAS Real = 646.28 s for the
  first) and `single_spot_E240p63_100k/` (100,000 histories, 4 threads, seed 1699, TOPAS Real = 1,436.99 s).
  The two 20,000-history runs are the same configuration transported twice and agree bit-for-bit
  (gamma 100.0%, max 0.377, R80 difference -0.561 mm in both), which is the same reproducibility
  property the full-plan runs show for a fixed seed and thread count.
- Raising the statistics from 20,000 to 100,000 histories moved max gamma from 0.377 to 0.359 and the R80
  difference from -0.561 mm to -0.567 mm. The range and shape conclusions are therefore already converged
  at 20,000 histories on this channel; more histories mainly reduce point-to-point curve noise.
- The 100,000-history run compared against the commissioned measured IDD: one-dimensional global gamma
  at 3%/3 mm and a 10% threshold is **100.0% over 103 points** (max 0.359, mean 0.216, overlap
  13-413 mm). Range agreement is R80 -0.567 mm, R50 -0.859 mm, R100 -0.735 mm, distal 80-20 falloff
  -0.144 mm. Mean absolute dose difference is 3.52% and the 16.92% maximum sits on the Bragg-peak
  gradient, which is why gamma still passes. Shallow-depth agreement is -0.12% to -0.32%.
- `single_spot_E240p63_1M/` holds generated decks only. Its dose binaries are zero bytes: the transport
  has never been run. Scaling the 100,000-history time puts it near four hours.
- `scripts/17_run_water_phantom_spot.py --analysis-only` re-exports curves, metrics and figures from the
  dose binaries an earlier transport wrote. It regenerates neither the decks nor the transport, refuses
  `--overwrite`, and fails when a `.bin` is missing or empty rather than producing an empty curve.
- `tests/test_water_phantom.py` covers the analysis library and the deck helpers. The suite is now
  134 tests and passes.

## Warnings and interpretation

- The included Schneider HU-to-material table is generic and is not an institutional scanner calibration.
- MRF4 is present in RTPLAN. The imported IDD-derived energy spectrum already includes upstream nozzle energy loss, so no extra WET slab is added; independent validation of residual MRF4 scattering/fragmentation effects is still required.
- Relative energy spectrum, angular/emittance and number-per-MU are modeled for the matching machine. `N_plan/N_sim` now supplies an independently determined particle-normalized physical-dose estimate without fitting TPS dose. Monitor-chamber traceability, independent end-to-end acceptance and model uncertainty remain incomplete.
- This is research physical-dose QA, not RBE dose or clinical acceptance. The 100,000-history run is low-statistics at individual-spot level; its isolated maximum voxel is not a reliable output metric. Because runtime is dominated by the per-spot fixed cost rather than by the history count, raising histories is the cheapest available way to reduce this noise; see "Measured thread behaviour" above.
- The water-phantom gamma result above validates the commissioned beam model's shape and range
  self-consistency against its own commissioning evidence. It is not an independent measurement,
  and it does not address CT calibration, MRF4 geometry or absolute output; the P0 items in
  WORKFLOW.md section 6 remain open.
- Remote CT transfer is disabled until the user configures and independently verifies the selected server identity and institutional authorization for patient-data transfer. `config/ssh_server.json` is still `disabled` with blank host/user, so nothing connects or uploads. The GUI is not running at the time of this synchronization, so the next launch will load the current SSH page source.

## Historical isolation

Outputs tied to the former water-phantom case (56,349 spots, 153x152x201 grid and 150,000-history MC) were moved to `archive/legacy_water_phantom_20260818/`. Their numerical contents were preserved, but they are not valid for the active patient-CT case. The former zero-byte production output was archived separately in the same bundle as an incomplete-run artifact.

## Unintended directories found on disk

These are not part of the intended layout and are recorded here so they are not mistaken for
valid cases. Nothing has been deleted; see OPTIMIZATION_REPORT.md item 4 for the guard that is missing.

- `dicom/Dicom/hzRoom1_90_RF4_250701/` (259 MB) and `dicom/Dicom/hzroom1-h-rf4-COM-250916/` (130 MB)
  are case roots that were created *inside* the DICOM tree. The first contains a full nested copy of
  `scripts/`, `gui/`, `machine_model/`, `analysis/` and another `dicom/`.
- `dicom/Dicom/hzRoom1_90_RF4_250701/CT/` was itself used as a case root, producing the meaningless
  identity `analysis/patient-anonymous--study-ffa63583df/plan-plan--ffa63583df/`. A case with neither
  RTPLAN nor CT identity should have been refused rather than given an `anonymous` cache key.
- The nested `scripts/` copy has already diverged from the master tree: `10_initialize_case.py` and
  `utils/commissioned_beam.py` differ, and `15_prepare_remote_bundle.py` is absent. Any run launched
  from that directory would silently use older physics code.

`dicom/1699` (160 MB) and `dicom/20260813165426` (130 MB) are earlier DICOM imports. Their status is
undetermined — confirm whether they are still needed before removing anything.

