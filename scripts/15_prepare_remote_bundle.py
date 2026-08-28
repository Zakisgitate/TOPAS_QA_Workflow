#!/usr/bin/env python3
"""Prepare an audited SSH transport bundle that uses server TOPAS/Geant4.

The bundle contains generated TOPAS parameter files and transfer/control shell
scripts.  It never copies a local TOPAS or Geant4 executable.  CT is uploaded
directly from the case into a content-addressed server cache, avoiding a second
large local copy.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import stat
import sys
import tempfile
from typing import Any, Iterable


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from gui.case_results import analysis_run_dir, case_identity, update_run_manifest  # noqa: E402
from gui.ssh_server import (  # noqa: E402
    config_ready,
    load_server_config,
    ssh_destination,
    validate_server_config,
)


_DICOM_DIRECTORY_PATTERN = re.compile(
    r'^(\s*s:Ge/Patient/DicomDirectory\s*=\s*)"[^"]*"\s*$', re.MULTILINE
)
_SAFE_TAG = re.compile(r"^[A-Za-z0-9_-]+$")
_GEANT4_DATA_VARIABLES = (
    "G4LEDATA",
    "G4LEVELGAMMADATA",
    "G4NEUTRONHPDATA",
    "G4ENSDFSTATEDATA",
    "G4SAIDXSDATA",
    "G4PARTICLEXSDATA",
    "G4PIIDATA",
    "G4REALSURFACEDATA",
    "G4ABLADATA",
    "G4INCLDATA",
    "G4RADIOACTIVEDATA",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _file_digest(path: Path, chunk_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def tree_inventory(root: Path, *, suffix: str | None = None) -> tuple[list[dict[str, Any]], str, int]:
    records: list[dict[str, Any]] = []
    tree_digest = hashlib.sha256()
    total = 0
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        if path.name.startswith("."):
            continue
        if suffix and path.suffix.lower() != suffix.lower():
            continue
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        sha256 = _file_digest(path)
        record = {"path": relative, "size_bytes": size, "sha256": sha256}
        records.append(record)
        total += size
        tree_digest.update(relative.encode("utf-8"))
        tree_digest.update(b"\0")
        tree_digest.update(str(size).encode("ascii"))
        tree_digest.update(b"\0")
        tree_digest.update(sha256.encode("ascii"))
        tree_digest.update(b"\n")
    return records, tree_digest.hexdigest(), total


def _shell_assign(name: str, value: str) -> str:
    return f"{name}={shlex.quote(value)}"


def _ssh_shell(config: dict[str, Any]) -> str:
    parts = [
        "ssh",
        "-p",
        str(int(config["ssh_port"])),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={config['known_hosts_path']}",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
    ]
    if str(config.get("auth_mode", "agent")) == "identity_file":
        parts.extend(
            ["-i", str(config["identity_file"]), "-o", "IdentitiesOnly=yes"]
        )
    return " ".join(shlex.quote(item) for item in parts)


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _copy_topas_parameters(source: Path, destination: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name.startswith(".") or name.endswith(".png")}

    shutil.copytree(source, destination, ignore=ignore)


def _rewrite_remote_paths(
    staged_topas: Path,
    *,
    local_root: Path,
    local_ct: Path,
    remote_job: str,
    remote_ct: str,
) -> None:
    patient = staged_topas / "geometry" / "patient.txt"
    if not patient.is_file():
        raise RuntimeError(f"TOPAS patient geometry is missing: {patient}")
    text = patient.read_text(encoding="utf-8")
    if not _DICOM_DIRECTORY_PATTERN.search(text):
        raise RuntimeError("TOPAS patient geometry has no DicomDirectory parameter to rewrite")
    text = _DICOM_DIRECTORY_PATTERN.sub(
        lambda match: f'{match.group(1)}"{remote_ct}"', text, count=1
    )
    text = re.sub(
        r"^\s*#\s*CT source:.*$",
        f"# CT source: content-addressed server cache {remote_ct}",
        text,
        flags=re.MULTILINE,
    )
    text = text.replace(str(local_ct), remote_ct).replace(str(local_root), remote_job)
    patient.write_text(text, encoding="utf-8")

    # Generated comments/audit paths may also contain the local case root.  They
    # are rewritten only in the staged copy, never in the active local case.
    for path in staged_topas.rglob("*.txt"):
        if path == patient:
            continue
        content = path.read_text(encoding="utf-8")
        rewritten = content.replace(str(local_ct), remote_ct).replace(str(local_root), remote_job)
        if rewritten != content:
            path.write_text(rewritten, encoding="utf-8")


def _remote_launcher(config: dict[str, Any], remote_job: str, job_id: str) -> str:
    variables = " ".join(_GEANT4_DATA_VARIABLES)
    return f"""#!/bin/sh
set -eu
umask 077
{_shell_assign('JOB_ID', job_id)}
{_shell_assign('JOB_DIR', remote_job)}
{_shell_assign('TOPAS_EXECUTABLE', str(config['topas_executable']))}
{_shell_assign('GEANT4_ENVIRONMENT_SCRIPT', str(config['geant4_environment_script']))}
{_shell_assign('GEANT4_DATA_ROOT', str(config['geant4_data_root']))}
mkdir -p "$JOB_DIR/logs" "$JOB_DIR/state" "$JOB_DIR/topas_output/production" "$JOB_DIR/topas_output/test"
printf 'STARTING\\n' > "$JOB_DIR/state/status"
finish() {{
  code=$?
  trap - EXIT HUP INT TERM
  if [ "$code" -eq 0 ]; then printf 'COMPLETED\\n' > "$JOB_DIR/state/status"; else printf 'FAILED:%s\\n' "$code" > "$JOB_DIR/state/status"; fi
  date -u '+%Y-%m-%dT%H:%M:%SZ' > "$JOB_DIR/state/finished_utc"
  exit "$code"
}}
trap finish EXIT HUP INT TERM
if [ ! -f "$GEANT4_ENVIRONMENT_SCRIPT" ]; then echo "Missing server environment script: $GEANT4_ENVIRONMENT_SCRIPT" >&2; exit 21; fi
if [ ! -x "$TOPAS_EXECUTABLE" ]; then echo "Missing server TOPAS executable: $TOPAS_EXECUTABLE" >&2; exit 22; fi
if [ ! -d "$GEANT4_DATA_ROOT" ]; then echo "Missing server Geant4 data root: $GEANT4_DATA_ROOT" >&2; exit 23; fi
set +u
. "$GEANT4_ENVIRONMENT_SCRIPT"
set -u
export TOPAS_G4_DATA_DIR="$GEANT4_DATA_ROOT"
g4_configured=0
for variable in {variables}; do
  eval "value=\${{$variable-}}"
  if [ -n "$value" ] && [ -d "$value" ]; then g4_configured=$((g4_configured+1)); fi
done
{{
  printf 'job_id\\t%s\\n' "$JOB_ID"
  printf 'host\\t%s\\n' "$(hostname 2>/dev/null || uname -n)"
  printf 'topas_executable\\t%s\\n' "$TOPAS_EXECUTABLE"
  printf 'topas_version\\t%s\\n' "$("$TOPAS_EXECUTABLE" --version 2>&1 | sed -n '1p' || true)"
  printf 'geant4_environment_script\\t%s\\n' "$GEANT4_ENVIRONMENT_SCRIPT"
  printf 'geant4_data_root\\t%s\\n' "$GEANT4_DATA_ROOT"
  printf 'topas_g4_data_dir\\t%s\\n' "$TOPAS_G4_DATA_DIR"
  printf 'individual_g4_dataset_variables\\t%s/{len(_GEANT4_DATA_VARIABLES)}\\n' "$g4_configured"
  printf 'started_utc\\t%s\\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
}} > "$JOB_DIR/state/server_runtime.tsv"
date -u '+%Y-%m-%dT%H:%M:%SZ' > "$JOB_DIR/state/started_utc"
printf '%s\\n' "$$" > "$JOB_DIR/state/transport_pid"
printf 'RUNNING\\n' > "$JOB_DIR/state/status"
cd "$JOB_DIR/topas"
"$TOPAS_EXECUTABLE" run_full_plan_qa.txt >> "$JOB_DIR/logs/topas.log" 2>&1
"""


def _local_scripts(
    bundle: Path,
    config: dict[str, Any],
    *,
    final_bundle: Path,
    local_ct: Path,
    remote_ct: str,
    remote_job: str,
) -> None:
    ssh_shell = _ssh_shell(config)
    host = ssh_destination(config)
    bundle_path = str(final_bundle)
    remote_prepare = (
        f"umask 077; mkdir -p {shlex.quote(remote_ct)} {shlex.quote(remote_job)} "
        f"{shlex.quote(remote_job + '/topas_output/production')} "
        f"{shlex.quote(remote_job + '/topas_output/test')} "
        f"{shlex.quote(remote_job + '/logs')} {shlex.quote(remote_job + '/state')}"
    )
    common = "\n".join(
        (
            "#!/bin/sh",
            "set -eu",
            "umask 077",
            _shell_assign("SSH_HOST", host),
            _shell_assign("SSH_RSH", ssh_shell),
            _shell_assign("LOCAL_BUNDLE", bundle_path),
            _shell_assign("LOCAL_CT", str(local_ct)),
            _shell_assign("REMOTE_CT", remote_ct),
            _shell_assign("REMOTE_JOB", remote_job),
        )
    )
    upload = f"""{common}
$SSH_RSH "$SSH_HOST" {shlex.quote(remote_prepare)}
rsync -a --partial --checksum -e "$SSH_RSH" "$LOCAL_CT/" "$SSH_HOST:$REMOTE_CT/"
rsync -a --partial --checksum --delete -e "$SSH_RSH" "$LOCAL_BUNDLE/topas/" "$SSH_HOST:$REMOTE_JOB/topas/"
rsync -a --partial --checksum -e "$SSH_RSH" "$LOCAL_BUNDLE/remote_bundle_manifest.json" "$LOCAL_BUNDLE/run_remote_transport.sh" "$SSH_HOST:$REMOTE_JOB/"
$SSH_RSH "$SSH_HOST" {shlex.quote('chmod 700 ' + remote_job + '/run_remote_transport.sh')}
printf 'Uploaded bundle to %s:%s\\n' "$SSH_HOST" "$REMOTE_JOB"
"""
    submit_command = (
        f"cd {shlex.quote(remote_job)}; "
        "if [ -f state/launcher_pid ] && kill -0 \"$(cat state/launcher_pid)\" 2>/dev/null; then "
        "echo 'Remote job is already running' >&2; exit 9; fi; "
        "printf 'SUBMITTED\\n' > state/status; "
        "nohup ./run_remote_transport.sh > logs/launcher.log 2>&1 < /dev/null & "
        "echo $! > state/launcher_pid; cat state/launcher_pid"
    )
    submit = f"""{common}
pid=$($SSH_RSH "$SSH_HOST" {shlex.quote(submit_command)})
printf 'Submitted remote TOPAS transport with launcher PID %s\\n' "$pid"
"""
    status_command = (
        f"cd {shlex.quote(remote_job)} 2>/dev/null || exit 4; "
        "printf 'status\\t'; cat state/status 2>/dev/null || printf 'NOT_SUBMITTED\\n'; "
        "printf 'pid\\t'; cat state/launcher_pid 2>/dev/null || printf -- '-\\n'; "
        "printf 'topas_log_bytes\\t'; wc -c < logs/topas.log 2>/dev/null || printf '0\\n'; "
        "tail -n 20 logs/topas.log 2>/dev/null || true"
    )
    status = f"""{common}
$SSH_RSH "$SSH_HOST" {shlex.quote(status_command)}
"""
    download_dir = str(final_bundle / "downloaded_topas_output")
    download = f"""{common}
{_shell_assign('DOWNLOAD_DIR', download_dir)}
mkdir -p "$DOWNLOAD_DIR"
rsync -a --partial --checksum -e "$SSH_RSH" "$SSH_HOST:$REMOTE_JOB/topas_output/" "$DOWNLOAD_DIR/"
rsync -a --partial --checksum -e "$SSH_RSH" "$SSH_HOST:$REMOTE_JOB/state/" "$DOWNLOAD_DIR/server_state/"
rsync -a --partial --checksum -e "$SSH_RSH" "$SSH_HOST:$REMOTE_JOB/logs/" "$DOWNLOAD_DIR/server_logs/"
printf 'Downloaded remote outputs to %s\\n' "$DOWNLOAD_DIR"
"""
    _write_executable(bundle / "01_upload_bundle.sh", upload)
    _write_executable(bundle / "02_submit_server_topas.sh", submit)
    _write_executable(bundle / "03_remote_status.sh", status)
    _write_executable(bundle / "04_download_results.sh", download)


def prepare_bundle(root: Path, app_root: Path, output_tag: str) -> Path:
    root = root.expanduser().resolve()
    app_root = app_root.expanduser().resolve()
    if not _SAFE_TAG.fullmatch(output_tag):
        raise RuntimeError("output_tag may contain only letters, digits, underscore and hyphen")
    config = load_server_config(app_root)
    findings = validate_server_config(app_root, config)
    if not config_ready(config, findings):
        details = "; ".join(item["detail"] for item in findings if item["level"] == "BLOCK")
        if config.get("enabled") is not True:
            details = "server is disabled" + (f"; {details}" if details else "")
        raise RuntimeError(f"Remote bundle cannot be prepared: {details}")

    topas_source = root / "topas"
    ct_source = root / "dicom" / "CT"
    entry = topas_source / "run_full_plan_qa.txt"
    if not entry.is_file():
        raise RuntimeError("Prepare stages 1–7 before creating a remote transport bundle")
    if not ct_source.is_dir():
        raise RuntimeError("Current case has no CT directory")
    ct_files, ct_sha256, ct_bytes = tree_inventory(ct_source, suffix=".dcm")
    if not ct_files:
        raise RuntimeError("Current case has no CT DICOM files to upload")

    identity = case_identity(root)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    seed = hashlib.sha256(
        f"{identity.plan_uid}|{output_tag}|{ct_sha256}|{timestamp}".encode("utf-8")
    ).hexdigest()[:10]
    job_id = f"{identity.patient_key[:36]}-{output_tag[:32]}-{timestamp}-{seed}"
    job_id = re.sub(r"[^A-Za-z0-9._-]+", "-", job_id).strip("-")
    remote_root = str(config["remote_root"]).rstrip("/")
    remote_ct = f"{remote_root}/ct-cache/{ct_sha256}"
    remote_job = f"{remote_root}/jobs/{job_id}"

    run = analysis_run_dir(root, output_tag, create=True)
    remote_parent = run / "remote"
    remote_parent.mkdir(parents=True, exist_ok=True)
    destination = remote_parent / job_id
    if destination.exists():
        raise RuntimeError(f"Remote bundle already exists: {destination}")
    staging = Path(tempfile.mkdtemp(prefix=f".{job_id}-", dir=remote_parent))
    try:
        staged_topas = staging / "topas"
        _copy_topas_parameters(topas_source, staged_topas)
        _rewrite_remote_paths(
            staged_topas,
            local_root=root,
            local_ct=ct_source,
            remote_job=remote_job,
            remote_ct=remote_ct,
        )
        topas_files, topas_sha256, topas_bytes = tree_inventory(staged_topas)
        _write_executable(
            staging / "run_remote_transport.sh",
            _remote_launcher(config, remote_job, job_id),
        )
        manifest = {
            "schema_version": 1,
            "bundle_type": "PLAN1699_TOPAS_REMOTE_TRANSPORT",
            "job_id": job_id,
            "created_utc": _iso_now(),
            "case_root": str(root),
            "output_tag": output_tag,
            "identity": {
                "patient_key": identity.patient_key,
                "plan_key": identity.plan_key,
                "patient_id": identity.patient_id,
                "study_uid": identity.study_uid,
                "rtplan_uid": identity.plan_uid,
                "rtplan_label": identity.plan_label,
            },
            "ct": {
                "source_directory": str(ct_source),
                "remote_cache_directory": remote_ct,
                "file_count": len(ct_files),
                "size_bytes": ct_bytes,
                "tree_sha256": ct_sha256,
                "files": ct_files,
                "contains_phi": True,
            },
            "topas": {
                "source_directory": str(topas_source),
                "staged_directory": "topas",
                "file_count": len(topas_files),
                "size_bytes": topas_bytes,
                "tree_sha256": topas_sha256,
                "files": topas_files,
                "local_absolute_paths_rewritten": True,
            },
            "remote": {
                "job_directory": remote_job,
                "entry_file": f"{remote_job}/topas/run_full_plan_qa.txt",
                "output_directory": f"{remote_job}/topas_output",
            },
            "server_runtime": {
                "server_id": str(config["server_id"]),
                "ssh_mode": str(config.get("ssh_mode", "alias")),
                "ssh_destination": ssh_destination(config),
                "host_key_sha256": str(config["host_key_sha256"]),
                "topas_executable": str(config["topas_executable"]),
                "geant4_environment_script": str(config["geant4_environment_script"]),
                "geant4_data_root": str(config["geant4_data_root"]),
                "runtime_source": "server-installed",
                "local_executables_uploaded": False,
            },
            "workflow": [
                "01_upload_bundle.sh",
                "02_submit_server_topas.sh",
                "03_remote_status.sh",
                "04_download_results.sh",
            ],
            "safety": {
                "host_key_checking": "strict, pinned project known_hosts",
                "credentials_stored": False,
                "local_case_modified": False,
                "clinical_use": False,
            },
        }
        (staging / "remote_bundle_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        _local_scripts(
            staging,
            config,
            final_bundle=destination,
            local_ct=ct_source,
            remote_ct=remote_ct,
            remote_job=remote_job,
        )
        (staging / "README.txt").write_text(
            "Remote TOPAS transport bundle\n\n"
            "1. Obtain institutional approval before transferring patient CT.\n"
            "2. Run 01_upload_bundle.sh. It uploads CT and generated parameters, never local executables.\n"
            "3. Run 02_submit_server_topas.sh. The remote launcher sources the selected server Geant4 environment and runs server TOPAS.\n"
            "4. Use 03_remote_status.sh, then 04_download_results.sh after COMPLETED.\n"
            "5. Validate downloaded hashes/headers locally before profile, Gamma, or DICOM export.\n",
            encoding="utf-8",
        )
        staging.rename(destination)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    update_run_manifest(
        root,
        output_tag,
        additions={
            "latest_remote_bundle": str(destination),
            "latest_remote_job_id": job_id,
            "remote_server_id": str(config["server_id"]),
        },
    )
    return destination


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--app-root", type=Path, default=APP_ROOT)
    parser.add_argument("--output-tag", required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    destination = prepare_bundle(args.root, args.app_root, args.output_tag)
    print(f"Remote bundle prepared: {destination}")
    print("No local TOPAS/Geant4 executable was copied.")
    print("The generated launcher uses the selected server TOPAS and Geant4 environment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
