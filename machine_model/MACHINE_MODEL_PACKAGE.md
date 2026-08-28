# Standard machine-model package

The GUI accepts one ZIP containing exactly one `machine_package.json`, either
at ZIP root or inside one enclosing folder. Inspection is read-only. Import is
possible only when the inspection has no `BLOCK` finding.

## Beam commissioning package

Set `package_kind` to `beam_commissioning`. The logical `files` map must include:

- `profile` → `profile.json`
- `particle_calibration` → `particle_calibration.json`
- `energy_spectrum` → the discrete incident-energy spectrum JSON
- `phase_space` → the Fermi–Eyges phase-space JSON
- `number_per_mu` → the energy-dependent primary-particles-per-MU table
- `measured_idd` → measured IDD evidence
- `measured_spot_sigma` → measured spot-sigma evidence
- `energy_list` → commissioned nominal-energy list

Every file in the ZIP except `machine_package.json` must appear in `files`, and
every logical name must have its exact lowercase SHA-256 in `sha256`. Extra
evidence is allowed only when it is also given a unique logical name and hash.
The beam package's particle binding may contain only an identity absolute-output
factor. A non-identity correction belongs in a separately approved
`absolute_output_calibration` package and is not applied merely by importing it.

Required units are fixed and checked exactly:

```json
{
  "energy_spectrum": "total MeV per carbon ion",
  "phase_space_position_sigma": "mm",
  "phase_space_angular_sigma": "rad",
  "number_per_mu": "primary carbon ions per MU",
  "measured_idd_depth": "mm",
  "measured_spot_sigma": "mm",
  "commissioned_energy": "MeV/u"
}
```

Use [beam_machine_package.template.json](package_templates/beam_machine_package.template.json)
as the manifest skeleton. Replace every placeholder and hash before inspection.

## Independent asset packages

These are deliberately not beam-model fields:

- `ct_calibration`: CT scanner/protocol HU–material/RSP calibration
- `nozzle_geometry`: MRF/nozzle geometry and WET evidence
- `absolute_output_calibration`: absolute output correction and measurement evidence

Each uses the same manifest envelope, a non-empty `units` object, a complete
`files`/`sha256` inventory, `subject.asset_id`, provenance and approval. Nozzle
and absolute-output assets also require `subject.treatment_machine_name`.
CT packages require `subject.scanner_name` and `subject.scan_protocol`; nozzle
packages require `subject.nozzle_id`; absolute-output packages require
`subject.calibration_protocol`.

Importing an independent asset only registers immutable, audited evidence. It
does not silently change TOPAS geometry, HU conversion or dose scaling. A
dedicated calculation binding must be implemented and selected before any such
asset can affect transport.

## Version and lifecycle rules

- Storage key: identifier + package version + content fingerprint.
- Existing content is never overwritten.
- One exact RTPLAN machine match may be selected automatically.
- Multiple active versions for the same machine require explicit GUI selection.
- Deactivated versions are excluded from new automatic calculations.
- There is no model-delete API. Historical calculations keep their recorded
  profile path/fingerprint; deactivation never removes those files.
- Import, deactivation and reactivation are blocked while any calculation is
  running or paused.
