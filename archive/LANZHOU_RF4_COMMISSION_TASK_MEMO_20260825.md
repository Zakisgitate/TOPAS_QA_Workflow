# LanZhou 1 Room RF4 Commission Task Memo

Date: 2026-08-25  
Status: Paused, pending explicit user authorization.  
Scope: Record current status only. Do not import into the GUI, change the active profile, or start TOPAS.

## Pause Reason

The earlier LanZhou draft built from machine_3 is not the requested machine data and must not be used. The draft and its build script remain isolated, unapproved, and not imported:

- machine_model/drafts/lzRoom1_90_RF4_241230/
- scripts/18_build_machine3_lanzhou_draft.py

Do not delete or activate either item unless the user explicitly requests it.

## Current Source Data

Use this latest source folder:

/Users/jiangzhenmin/Desktop/兰州1号室RF4(260226最小mu和机器参数更新，RS厚度改为30.05-25A模板)/

| Purpose | File | Coverage |
|---|---|---|
| Machine template | lzRoom1_90_RF4-25A.xlsx | Machine, snout, RS, MRF4, VSAD, discrete energy configuration |
| Measured single-spot water IDD | IDD_lzRoom1_90_RF4.csv | 14 measured energies |
| Range table | Range.csv | 123 energy layers |
| Absolute-dose reference | Absolute_lzRoom1_90_RF4.csv | 123 energy layers, cGy/MU |
| Air spot summary | SpotSummary_lzRoom1PBS_RF4.txt | 123 energies, five air planes |
| RS spot summary | SpotSummary_lzRoom1PBS_RF4 - RS.txt | 30 energies, five air planes |
| Energy and minimum MU | Energy-and-limitation-25A.csv | 123 energies, 14 minimum-MU entries |

## Confirmed Machine Parameters

| Item | Current value |
|---|---|
| Candidate TreatmentMachineName | lzRoom1_90_RF4_260226 |
| Machine alias | 90DegreeRoom1 |
| Particle | Carbon |
| Gantry | 90 deg |
| VSAD X | 6228.28 mm |
| VSAD Y | 7007.64 mm |
| IDD isocenter-to-snout | 680.0 mm |
| IDD isocenter-to-water-surface | 150.0 mm |
| IDD detector | Circular, diameter 80.0 mm |
| RS name | RangeShifter_35mm |
| RS physical thickness | 30.05 mm |
| RS WET | 35 mm |
| RS material and density | PMMA, 1.19 g/cm3 |
| RS tray-to-isocenter | 595.5 mm |
| MRF4 | Aluminum, WET 4 mm |

The candidate machine name must be checked exactly against DICOM TreatmentMachineName after a LanZhou RTPLAN is supplied. A different DICOM name requires a new immutable package version, not editing an imported package.

## Energy, IDD, and Range Status

- The clinical energy grid contains 123 layers: 1443.12--4799.04 MeV total, or 120.26--399.92 MeV/u.
- IDD_lzRoom1_90_RF4.csv is measured water, single spot, RF4 evidence. It contains 14 representative energies:
  120.26, 131.05, 141.40, 173.01, 190.19, 211.44, 240.63, 261.03, 282.69, 309.82, 330.09, 355.62, 383.26, and 399.92 MeV/u.
- Range.csv is a 123-layer range or R80 reference and must be retained for cross-checking.
- R80 calculated directly from the raw IDD using the project's normal distal 80% peak crossing is about 6 mm shallower than Range.csv. Therefore Range.csv cannot be treated as the raw IDD R80 without confirming its depth origin, correction, and range definition.
- At 240.63 MeV/u, raw IDD R80 by that algorithm is about 111.98 mm; Range.csv reports 118.0 mm.

## Spot and Absolute-Dose Open Items

- SpotSummary contains X/Y widths on planes labelled 300, 150, 0, -150, and -300. It does not state whether the reported widths are sigma, FWHM, or another metric.
- Do not use these values directly as the Sigma[mm] required by the project's Fermi-Eyges phase-space model.
- Prefer raw lateral SpotProfile curves. If unavailable, written confirmation is needed for the SpotSummary width definition and the plane-coordinate convention.
- Absolute_lzRoom1_90_RF4.csv is a 123-layer cGy/MU reference measured at 20.66 mm water depth, 104 x 104 mm2 field, 2 mm spot spacing, and a 5 mm circular detector.
- This is not the NF(E) table required by the current beam package. Do not manufacture NF(E), which must mean primary carbon ions per MU. Absolute output needs a separately approved calibration pathway if NF(E) is unavailable.

## Suggested Restart Order

1. Check RTPLAN TreatmentMachineName and X/Y VSAD against the latest template.
2. Confirm the SpotSummary physical definition or obtain raw SpotProfile data.
3. Confirm the Range.csv R80 depth origin and measurement/correction definition.
4. Decide whether absolute dose is in scope and whether NF(E) can be supplied.
5. Build spectra, phase space, evidence files, and fit audits in an isolated draft directory only.
6. After review, import a package, add the GUI choice, and then run water-phantom validation.
