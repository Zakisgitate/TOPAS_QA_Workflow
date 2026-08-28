# LanZhou RF4 Commission Continuation

Date: 2026-08-26  
Status: Latest data and matched TOPAS_Test spot evidence ingested into an isolated incomplete draft; GUI and active profile unchanged.

## Completed

- Source folder audited:
  `/Users/jiangzhenmin/Desktop/兰州1号室RF4(260226最小mu和机器参数更新，RS厚度改为30.05-25A模板)/`
- Candidate machine name from the 25A template: `lzRoom1_90_RF4_260226`.
- Machine template values recorded: VSAD X/Y `6228.28 / 7007.64 mm`, snout `585.5 mm`, RS physical thickness `30.05 mm`, RS WET `35 mm`, RS tray-to-isocenter `595.5 mm`, PMMA density `1.19 g/cm3`, MRF4 aluminum WET `4 mm`.
- Fourteen measured water IDD curves were parsed and fitted to the existing ideal-water kernel library as a non-negative discrete spectrum.
- Spectrum fit audit: median normalized RMSE `1.047%`, maximum normalized RMSE `4.053%`, maximum absolute fitted-vs-measured R80 difference `0.331 mm`.
- 123 clinical energy layers, 123 Range rows, 123 absolute-output rows, 123 minimum-MU rows, 123 air SpotSummary rows and 30 RS SpotSummary rows were copied/registered.
- Range cross-check: `Range.csv - IDD-derived R80` median `6.009 mm`, maximum absolute difference `6.151 mm`; Range.csv remains a cross-check only.

## Spot Phase-Space Evidence Added 2026-08-28

- The uploaded `SpotSummary_lzRoom1PBS_RF4.txt` is byte-identical to
  `TOPAS_Test/LanZhou/9973/.../lzRoom1_90_RF4_250331_SpotProfileSummary.txt`.
  Both SHA-256 values are `23b08868f6d0686afe140f4b9eb10cc3c7dc2b57c1e0880208d81d8181f49f13`.
- The corresponding TOPAS_Test processed `lzRoom1_90_RF4_250331_SpotSigma.csv` was imported
  byte-for-byte into the isolated draft as `measured_spot_sigma.csv`. Both SHA-256 values are
  `ceeaf090d78899f957a6a3d04f31422719a9f7dcde55d84f2c5b62561ab01e3c`.
- This establishes the project processing convention for this exact summary: the two plane values
  are X/Y FWHM; `Sigma_fit` is their mean after division by `2.354820045`; the `Sigma` column is the
  processed TOPAS_Test model input and is not replaced by `Sigma_fit`.
- The current project fitted 123 Fermi-Eyges phase-space rows over 120.26--399.92 MeV/u at the
  -680 mm source plane. Maximum fit RMSE is `0.039674 mm`; maximum isocenter error is `0.048001 mm`.
- Reproducible builder: `scripts/20_derive_lanzhou_spot_phase.py`. It refuses to import the reference
  SpotSigma unless the source and reference SpotSummary hashes match exactly.
- Generated audit artifacts: `spot_sigma_derivation.json`, `spot_sigma_derivation_audit.csv`,
  `phase_space.json`, `SpotProfileCoeff.csv`, `phase_space_fit_audit.csv`, and
  `phase_space_fit_audit.json`.

## Isolated Draft

`machine_model/drafts/lzRoom1_90_RF4_260226_latest/`

The draft is explicitly `incomplete_unapproved_not_imported_not_for_clinical_use`. It has no GUI registry entry, is not the active profile, and has not been used to run TOPAS.

Builder: `scripts/19_build_lanzhou_latest_draft.py`

## Blocking Inputs Before an Importable Commissioned Profile

1. LanZhou RTPLAN to verify exact `TreatmentMachineName` and VSAD identity.
2. Independent sign-off that the matched LanZhou/9973 TOPAS_Test SpotSigma processing remains applicable to the 260226 machine release.
3. Primary carbon ions per MU `NF(E)` or an approved calibration pathway. The measured cGy/MU file is retained as an absolute-output reference and was not converted into NF(E).
4. Range.csv depth origin, range definition and any correction/WET convention.

Until these are supplied, do not copy this draft into `machine_model/beam_commissioning/`, do not change `active_profile.json`, and do not add a GUI machine option.
