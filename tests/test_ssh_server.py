from __future__ import annotations

import importlib.util
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from gui.ssh_server import public_server_status, save_server_config, trust_host_key


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "15_prepare_remote_bundle.py"
SPEC = importlib.util.spec_from_file_location("prepare_remote_bundle_for_test", SCRIPT_PATH)
assert SPEC and SPEC.loader
REMOTE_BUNDLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REMOTE_BUNDLE)


def _dicom(path: Path, modality: str, patient_id: str, study_uid: str) -> None:
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = generate_uid()
    meta.MediaStorageSOPInstanceUID = generate_uid()
    dataset = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
    dataset.is_little_endian = True
    dataset.is_implicit_VR = False
    dataset.Modality = modality
    dataset.PatientID = patient_id
    dataset.PatientName = "Remote^Bundle"
    dataset.StudyInstanceUID = study_uid
    dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    if modality == "RTPLAN":
        dataset.RTPlanLabel = "SSH_QA"
    dataset.save_as(path, write_like_original=False)


def _mini_case(root: Path) -> None:
    ct = root / "dicom" / "CT"
    rtplan = root / "dicom" / "RTPLAN"
    patient = root / "topas" / "geometry" / "patient.txt"
    scoring = root / "topas" / "scoring" / "dose.txt"
    ct.mkdir(parents=True)
    rtplan.mkdir(parents=True)
    patient.parent.mkdir(parents=True)
    scoring.parent.mkdir(parents=True)
    (root / "topas" / "run_full_plan_qa.txt").write_text(
        "includeFile = geometry/patient.txt\nincludeFile = scoring/dose.txt\n",
        encoding="utf-8",
    )
    patient.write_text(
        "# CT source: " + str(ct) + "\n"
        's:Ge/Patient/Type = "TsDicomPatient"\n'
        f's:Ge/Patient/DicomDirectory = "{ct}"\n',
        encoding="utf-8",
    )
    scoring.write_text(
        's:Sc/TPSDoseToMedium/OutputFile = "../topas_output/production/test"\n',
        encoding="utf-8",
    )
    study_uid = generate_uid()
    _dicom(ct / "CT_0001.dcm", "CT", "PATIENT_SSH", study_uid)
    _dicom(rtplan / "RP_0001.dcm", "RTPLAN", "PATIENT_SSH", study_uid)


class SshServerTests(unittest.TestCase):
    def test_default_server_is_disabled_and_has_no_credentials(self) -> None:
        app_root = Path(__file__).resolve().parents[1]
        status = public_server_status(app_root, app_root)
        self.assertFalse(status["enabled"])
        self.assertFalse(status["configured"])
        serialized = json.dumps(status).casefold()
        self.assertIn("password", serialized)  # policy says passwords are never stored
        self.assertNotIn("private_key", serialized)
        self.assertTrue(status["config"]["topasExecutable"].startswith("/"))
        self.assertTrue(status["config"]["geant4EnvironmentScript"].startswith("/"))

    def test_user_settings_are_validated_and_saved_without_secret_contents(self) -> None:
        with tempfile.TemporaryDirectory(prefix="plan1699-ssh-config-") as temporary:
            app_root = Path(temporary)
            config_dir = app_root / "config"
            config_dir.mkdir()
            (config_dir / "ssh_server.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "enabled": False,
                        "server_id": "initial",
                        "ssh_mode": "direct",
                        "ssh_host": "",
                        "ssh_user": "",
                        "ssh_port": 22,
                        "auth_mode": "agent",
                        "identity_file": "",
                        "known_hosts_file": "config/ssh_known_hosts",
                        "host_key_sha256": "",
                        "remote_root": "/srv/plan1699",
                        "topas_executable": "/opt/topas/bin/topas",
                        "geant4_environment_script": "/opt/geant4/bin/geant4.sh",
                        "geant4_data_root": "/opt/geant4/data",
                        "max_parallel_jobs": 1,
                    }
                ),
                encoding="utf-8",
            )
            values = {
                "enabled": True,
                "server_id": "research-topas-01",
                "ssh_mode": "direct",
                "ssh_host": "compute.example.org",
                "ssh_user": "researcher",
                "ssh_port": 2222,
                "auth_mode": "agent",
                "identity_file": "",
                "remote_root": "/srv/plan1699",
                "topas_executable": "/opt/topas/bin/topas",
                "geant4_environment_script": "/opt/geant4/bin/geant4.sh",
                "geant4_data_root": "/opt/geant4/data",
                "max_parallel_jobs": 2,
            }
            status = save_server_config(app_root, values)
            stored = json.loads((config_dir / "ssh_server.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["schema_version"], 2)
            self.assertEqual(stored["ssh_host"], "compute.example.org")
            self.assertEqual(status["config"]["sshUser"], "researcher")
            self.assertNotIn("password", stored)
            self.assertNotIn("private_key", stored)
            self.assertEqual((config_dir / "ssh_server.json").stat().st_mode & 0o777, 0o600)

    def test_host_key_requires_explicit_pinning_and_is_written_exactly(self) -> None:
        with tempfile.TemporaryDirectory(prefix="plan1699-ssh-key-") as temporary:
            app_root = Path(temporary)
            config_dir = app_root / "config"
            config_dir.mkdir()
            blob = base64.b64encode(b"test-ed25519-key-material").decode("ascii")
            fingerprint = "SHA256:" + base64.b64encode(
                hashlib.sha256(b"test-ed25519-key-material").digest()
            ).decode("ascii").rstrip("=")
            (config_dir / "ssh_server.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "enabled": False,
                        "server_id": "research-topas-01",
                        "ssh_mode": "direct",
                        "ssh_host": "compute.example.org",
                        "ssh_user": "researcher",
                        "ssh_port": 22,
                        "auth_mode": "agent",
                        "identity_file": "",
                        "known_hosts_file": "config/ssh_known_hosts",
                        "host_key_sha256": "",
                        "remote_root": "/srv/plan1699",
                        "topas_executable": "/opt/topas/bin/topas",
                        "geant4_environment_script": "/opt/geant4/bin/geant4.sh",
                        "geant4_data_root": "/opt/geant4/data",
                        "max_parallel_jobs": 1,
                    }
                ),
                encoding="utf-8",
            )
            inspected = {
                "lookup": "compute.example.org",
                "candidates": [
                    {
                        "keyType": "ssh-ed25519",
                        "fingerprint": fingerprint,
                        "knownHostsLine": f"compute.example.org ssh-ed25519 {blob}",
                        "trusted": False,
                        "requiresReplacement": False,
                    }
                ],
            }
            with patch("gui.ssh_server._scan_host_keys", return_value=inspected):
                result = trust_host_key(app_root, fingerprint)
            self.assertEqual(result["fingerprint"], fingerprint)
            self.assertEqual(
                (config_dir / "ssh_known_hosts").read_text(encoding="utf-8").strip(),
                f"compute.example.org ssh-ed25519 {blob}",
            )
            stored = json.loads((config_dir / "ssh_server.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["host_key_sha256"], fingerprint)

    def test_remote_bundle_rewrites_staged_ct_and_uses_server_runtime(self) -> None:
        with tempfile.TemporaryDirectory(prefix="plan1699-ssh-test-") as temporary:
            temporary_root = Path(temporary)
            case_root = temporary_root / "case"
            _mini_case(case_root)
            original_patient = (
                case_root / "topas" / "geometry" / "patient.txt"
            ).read_text(encoding="utf-8")
            config = {
                "schema_version": 2,
                "enabled": True,
                "server_id": "fixed-topas-01",
                "ssh_mode": "direct",
                "ssh_host": "fixed-topas.example.org",
                "ssh_user": "researcher",
                "ssh_port": 22,
                "auth_mode": "agent",
                "identity_file": "",
                "known_hosts_path": str(temporary_root / "known_hosts"),
                "host_key_sha256": "SHA256:abcdefghijklmnopqrstuvwxyz1234567890ABCDE",
                "remote_root": "/srv/plan1699",
                "topas_executable": "/opt/topas/bin/topas",
                "geant4_environment_script": "/opt/geant4/bin/geant4.sh",
                "geant4_data_root": "/opt/geant4/data",
            }
            with (
                patch.object(REMOTE_BUNDLE, "load_server_config", return_value=config),
                patch.object(REMOTE_BUNDLE, "validate_server_config", return_value=[]),
                patch.object(REMOTE_BUNDLE, "config_ready", return_value=True),
            ):
                bundle = REMOTE_BUNDLE.prepare_bundle(
                    case_root, temporary_root, "remote_test"
                )
            manifest = json.loads(
                (bundle / "remote_bundle_manifest.json").read_text(encoding="utf-8")
            )
            staged_patient = (
                bundle / "topas" / "geometry" / "patient.txt"
            ).read_text(encoding="utf-8")
            launcher = (bundle / "run_remote_transport.sh").read_text(encoding="utf-8")
            upload = (bundle / "01_upload_bundle.sh").read_text(encoding="utf-8")

            self.assertEqual(
                (case_root / "topas" / "geometry" / "patient.txt").read_text(
                    encoding="utf-8"
                ),
                original_patient,
            )
            self.assertNotIn(str(case_root), staged_patient)
            self.assertIn(manifest["ct"]["remote_cache_directory"], staged_patient)
            self.assertIn("/opt/topas/bin/topas", launcher)
            self.assertIn("/opt/geant4/bin/geant4.sh", launcher)
            self.assertIn("/opt/geant4/data", launcher)
            self.assertIn('export TOPAS_G4_DATA_DIR="$GEANT4_DATA_ROOT"', launcher)
            self.assertEqual(manifest["server_runtime"]["runtime_source"], "server-installed")
            self.assertFalse(manifest["server_runtime"]["local_executables_uploaded"])
            self.assertIn(str(bundle), upload)
            self.assertIn(str(case_root / "dicom" / "CT"), upload)
            self.assertIn("rsync", upload)
            self.assertFalse((bundle / "bin").exists())

            for name in (
                "run_remote_transport.sh",
                "01_upload_bundle.sh",
                "02_submit_server_topas.sh",
                "03_remote_status.sh",
                "04_download_results.sh",
            ):
                result = subprocess.run(
                    ["/bin/sh", "-n", str(bundle / name)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")
