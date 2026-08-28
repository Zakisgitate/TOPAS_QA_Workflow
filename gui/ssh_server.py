"""User-configurable, host-key-pinned SSH support for remote TOPAS transport.

Connection settings may be edited by the local GUI, but remote commands remain
application-defined. Passwords and private-key contents never enter this project;
OpenSSH agent/Keychain or an existing identity file performs authentication.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import subprocess
from typing import Any, Iterable


CONFIG_RELATIVE_PATH = Path("config/ssh_server.json")
DEFAULT_TIMEOUT_SECONDS = 15
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_HOSTNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$")
_SAFE_IPV6 = re.compile(r"^[0-9A-Fa-f:]+$")
_SAFE_USER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_REMOTE_PATH = re.compile(r"^/[A-Za-z0-9._+/@%=-]+(?:/[A-Za-z0-9._+@%=-]+)*$|^/$")
_KEY_TYPES = {"ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa"}
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


def _finding(level: str, check: str, detail: str) -> dict[str, str]:
    return {"level": level, "check": check, "detail": detail}


def _is_absolute_remote_path(value: Any) -> bool:
    text = str(value or "")
    path = PurePosixPath(text)
    return (
        bool(_SAFE_REMOTE_PATH.fullmatch(text))
        and path.is_absolute()
        and len(path.parts) >= 3
    )


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ssh_mode(config: dict[str, Any]) -> str:
    raw = str(config.get("ssh_mode", "")).strip().lower()
    if raw in {"direct", "alias"}:
        return raw
    return "alias" if str(config.get("ssh_host_alias", "")).strip() else "direct"


def _ssh_host(config: dict[str, Any]) -> str:
    return str(
        config.get("ssh_host", "")
        or config.get("ssh_host_alias", "")
    ).strip()


def _valid_direct_host(value: str) -> bool:
    host = value.strip().strip("[]")
    return bool(_SAFE_HOSTNAME.fullmatch(host) or (_SAFE_IPV6.fullmatch(host) and ":" in host))


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def _fingerprint_key_blob(blob: str) -> str:
    try:
        raw = base64.b64decode(blob.encode("ascii"), validate=True)
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("Server returned an invalid SSH public key") from exc
    digest = base64.b64encode(hashlib.sha256(raw).digest()).decode("ascii").rstrip("=")
    return "SHA256:" + digest


def _resolved_known_hosts(app_root: Path, value: Any) -> Path:
    raw = Path(str(value or "").strip())
    if raw.is_absolute():
        path = raw.resolve()
    else:
        path = (app_root / raw).resolve()
    # Host identity belongs to the application configuration.  Keeping this file
    # below APP_ROOT prevents a patient case or HTTP payload from substituting it.
    try:
        path.relative_to(app_root.resolve())
    except ValueError as exc:
        raise RuntimeError("known_hosts_file must stay inside the application folder") from exc
    return path


def load_server_config(app_root: Path) -> dict[str, Any]:
    path = (app_root / CONFIG_RELATIVE_PATH).resolve()
    if not path.is_file():
        raise RuntimeError(f"Fixed SSH server configuration is missing: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot read fixed SSH server configuration: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("SSH server configuration must be a JSON object")
    payload = dict(payload)
    payload.setdefault("schema_version", 2)
    payload.setdefault("ssh_mode", "alias" if payload.get("ssh_host_alias") else "direct")
    payload.setdefault("ssh_host", payload.get("ssh_host_alias", ""))
    payload.setdefault("ssh_user", "")
    payload.setdefault("auth_mode", "agent")
    payload.setdefault("identity_file", "")
    payload.setdefault("known_hosts_file", str(CONFIG_RELATIVE_PATH.parent / "ssh_known_hosts"))
    payload["config_path"] = str(path)
    payload["known_hosts_path"] = str(
        _resolved_known_hosts(app_root, payload.get("known_hosts_file"))
    )
    return payload


def _validate_editable_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    server_id = str(config.get("server_id", "")).strip()
    if not _SAFE_IDENTIFIER.fullmatch(server_id) or "CHANGE_ME" in server_id:
        errors.append("Server ID must contain only letters, numbers, dot, underscore or hyphen")

    mode = _ssh_mode(config)
    host = _ssh_host(config)
    if mode == "alias":
        if not _SAFE_IDENTIFIER.fullmatch(host) or "CHANGE_ME" in host:
            errors.append("OpenSSH alias is invalid")
    elif not _valid_direct_host(host):
        errors.append("SSH hostname/IP is invalid")

    user = str(config.get("ssh_user", "")).strip()
    if user and not _SAFE_USER.fullmatch(user):
        errors.append("SSH username is invalid")
    if mode == "direct" and not user:
        errors.append("SSH username is required for a direct connection")

    port = _integer(config.get("ssh_port", 0))
    if not 1 <= port <= 65535:
        errors.append("SSH port must be within 1–65535")

    auth_mode = str(config.get("auth_mode", "agent")).strip().lower()
    if auth_mode not in {"agent", "identity_file"}:
        errors.append("Authentication mode must be agent or identity_file")
    identity = str(config.get("identity_file", "")).strip()
    if auth_mode == "identity_file":
        if not identity:
            errors.append("Choose an existing SSH private-key file")
        else:
            path = Path(identity).expanduser()
            if not path.is_absolute() or not path.is_file():
                errors.append("SSH identity file must be an existing absolute file")
            elif path.suffix.lower() == ".pub":
                errors.append("Choose the private key, not the .pub file")

    for field, label in (
        ("remote_root", "Remote job root"),
        ("topas_executable", "Server TOPAS executable"),
        ("geant4_environment_script", "Server environment script"),
        ("geant4_data_root", "Server Geant4 data root"),
    ):
        if not _is_absolute_remote_path(config.get(field)):
            errors.append(f"{label} must be a safe absolute server path with at least two components")

    maximum = _integer(config.get("max_parallel_jobs", 0))
    if not 1 <= maximum <= 32:
        errors.append("Remote parallel jobs must be within 1–32")
    return errors


def save_server_config(app_root: Path, values: dict[str, Any]) -> dict[str, Any]:
    """Validate and atomically save user-entered, non-secret connection settings."""
    current = load_server_config(app_root)
    mode = str(values.get("ssh_mode", "direct")).strip().lower()
    host = str(values.get("ssh_host", "")).strip()
    user = str(values.get("ssh_user", "")).strip()
    auth_mode = str(values.get("auth_mode", "agent")).strip().lower()
    identity = str(values.get("identity_file", "")).strip()
    if identity:
        identity = str(Path(identity).expanduser().resolve())
    payload: dict[str, Any] = {
        "schema_version": 2,
        "enabled": bool(values.get("enabled", False)),
        "server_id": str(values.get("server_id", "")).strip(),
        "ssh_mode": mode,
        "ssh_host": host,
        "ssh_user": user,
        "ssh_port": _integer(values.get("ssh_port", 0)),
        "auth_mode": auth_mode,
        "identity_file": identity if auth_mode == "identity_file" else "",
        "known_hosts_file": str(current.get("known_hosts_file", "config/ssh_known_hosts")),
        "host_key_sha256": str(current.get("host_key_sha256", "")),
        "remote_root": str(values.get("remote_root", "")).strip(),
        "topas_executable": str(values.get("topas_executable", "")).strip(),
        "geant4_environment_script": str(values.get("geant4_environment_script", "")).strip(),
        "geant4_data_root": str(values.get("geant4_data_root", "")).strip(),
        "max_parallel_jobs": _integer(values.get("max_parallel_jobs", 0)),
    }
    errors = _validate_editable_config(payload)
    if errors:
        raise RuntimeError("Cannot save SSH server settings: " + "; ".join(errors))

    old_identity = (_ssh_mode(current), _ssh_host(current), _integer(current.get("ssh_port", 0)))
    new_identity = (mode, host, payload["ssh_port"])
    if old_identity != new_identity:
        payload["host_key_sha256"] = ""
    config_path = Path(str(current["config_path"]))
    _atomic_json(config_path, payload)
    return public_server_status(app_root)


def ssh_destination(config: dict[str, Any]) -> str:
    host = _ssh_host(config)
    user = str(config.get("ssh_user", "")).strip()
    return f"{user}@{host}" if user else host


def _connection_endpoint(config: dict[str, Any]) -> dict[str, Any]:
    """Resolve the network hostname used for host-key scanning and lookup."""
    mode = _ssh_mode(config)
    host = _ssh_host(config)
    port = _integer(config.get("ssh_port", 22), 22)
    if mode == "direct":
        scan_host = host.strip("[]")
        lookup = f"[{scan_host}]:{port}" if port != 22 else scan_host
        return {"hostname": scan_host, "port": port, "lookup": lookup}
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("Local OpenSSH executable was not found")
    result = subprocess.run(
        [ssh, "-G", "-p", str(port), host],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "Cannot resolve the OpenSSH alias").strip())
    resolved: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if " " in line:
            key, value = line.split(None, 1)
            resolved[key.lower()] = value.strip()
    scan_host = resolved.get("hostname", host).strip("[]")
    resolved_port = _integer(resolved.get("port", port), port)
    host_key_alias = resolved.get("hostkeyalias", "").strip()
    lookup = host_key_alias or (f"[{scan_host}]:{resolved_port}" if resolved_port != 22 else scan_host)
    return {"hostname": scan_host, "port": resolved_port, "lookup": lookup}


def _known_host_records(path: Path, lookup: str | None = None) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    records: list[dict[str, str]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("|"):
            continue
        fields = line.split()
        if len(fields) < 3 or fields[1] not in _KEY_TYPES:
            continue
        hosts, key_type, blob = fields[:3]
        if lookup is not None and lookup not in hosts.split(","):
            continue
        try:
            fingerprint = _fingerprint_key_blob(blob)
        except RuntimeError:
            continue
        records.append(
            {"hosts": hosts, "keyType": key_type, "blob": blob, "fingerprint": fingerprint, "line": line}
        )
    return records


def _scan_host_keys(app_root: Path) -> dict[str, Any]:
    config = load_server_config(app_root)
    errors = _validate_editable_config(config)
    if errors:
        raise RuntimeError("Save valid connection settings first: " + "; ".join(errors))
    endpoint = _connection_endpoint(config)
    keyscan = shutil.which("ssh-keyscan")
    if not keyscan:
        raise RuntimeError("ssh-keyscan is not available on this Mac")
    try:
        result = subprocess.run(
            [keyscan, "-T", "8", "-p", str(endpoint["port"]), str(endpoint["hostname"])],
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Server host-key inspection timed out") from exc
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_line in result.stdout.splitlines():
        fields = raw_line.strip().split()
        if len(fields) < 3 or fields[1] not in _KEY_TYPES:
            continue
        key_type, blob = fields[1], fields[2]
        fingerprint = _fingerprint_key_blob(blob)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        candidates.append(
            {
                "keyType": key_type,
                "fingerprint": fingerprint,
                "knownHostsLine": f"{endpoint['lookup']} {key_type} {blob}",
            }
        )
    if not candidates:
        detail = (result.stderr or "No SSH host key was returned").strip()
        raise RuntimeError(f"Could not inspect server host keys: {detail[:1000]}")
    known_hosts = Path(str(config["known_hosts_path"]))
    trusted = _known_host_records(known_hosts, str(endpoint["lookup"]))
    trusted_fingerprints = [record["fingerprint"] for record in trusted]
    for candidate in candidates:
        candidate["trusted"] = candidate["fingerprint"] in trusted_fingerprints
        candidate["requiresReplacement"] = bool(trusted) and not candidate["trusted"]
    return {
        "serverId": str(config.get("server_id", "")),
        "hostname": endpoint["hostname"],
        "port": endpoint["port"],
        "lookup": endpoint["lookup"],
        "candidates": candidates,
        "trustedFingerprints": trusted_fingerprints,
        "warning": "Verify the selected SHA-256 fingerprint through an independent channel before trusting it.",
    }


def inspect_host_keys(app_root: Path) -> dict[str, Any]:
    return _scan_host_keys(app_root)


def trust_host_key(app_root: Path, fingerprint: str, *, replace: bool = False) -> dict[str, Any]:
    inspected = _scan_host_keys(app_root)
    selected = next(
        (item for item in inspected["candidates"] if item["fingerprint"] == fingerprint), None
    )
    if selected is None:
        raise RuntimeError("Selected fingerprint is no longer presented by the server")
    if selected["requiresReplacement"] and not replace:
        raise RuntimeError("The server key differs from the currently pinned key; explicit replacement confirmation is required")

    config = load_server_config(app_root)
    known_hosts = Path(str(config["known_hosts_path"]))
    known_hosts.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = known_hosts.read_text(encoding="utf-8").splitlines() if known_hosts.is_file() else []
    lookup = str(inspected["lookup"])
    kept: list[str] = []
    for line in existing_lines:
        fields = line.strip().split()
        if len(fields) >= 3 and lookup in fields[0].split(","):
            continue
        kept.append(line)
    kept.append(str(selected["knownHostsLine"]))
    temporary = known_hosts.with_suffix(known_hosts.suffix + ".tmp")
    temporary.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(known_hosts)

    persisted = {
        key: value
        for key, value in config.items()
        if key not in {"config_path", "known_hosts_path", "ssh_host_alias"}
    }
    persisted["schema_version"] = 2
    persisted["host_key_sha256"] = fingerprint
    _atomic_json(Path(str(config["config_path"])), persisted)
    return {
        "message": "Server host key pinned after explicit confirmation",
        "fingerprint": fingerprint,
        "status": public_server_status(app_root),
    }


def validate_server_config(app_root: Path, config: dict[str, Any]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    schema = _integer(config.get("schema_version", 0))
    findings.append(
        _finding(
            "PASS" if schema == 2 else "BLOCK",
            "Configuration schema",
            "User-configurable SSH schema version 2"
            if schema == 2
            else "Save the settings to migrate to schema version 2",
        )
    )

    enabled = config.get("enabled") is True
    findings.append(
        _finding(
            "PASS" if enabled else "WARN",
            "Server enabled",
            "Remote actions are enabled" if enabled else "Disabled until this server is commissioned",
        )
    )

    server_id = str(config.get("server_id", "")).strip()
    server_valid = bool(_SAFE_IDENTIFIER.fullmatch(server_id)) and "CHANGE_ME" not in server_id
    findings.append(
        _finding("PASS" if server_valid else "BLOCK", "Server ID", server_id or "Enter a server ID")
    )

    mode = _ssh_mode(config)
    host = _ssh_host(config)
    host_valid = (
        bool(_SAFE_IDENTIFIER.fullmatch(host)) and "CHANGE_ME" not in host
        if mode == "alias"
        else _valid_direct_host(host)
    )
    findings.append(
        _finding(
            "PASS" if host_valid else "BLOCK",
            "SSH connection target",
            f"{mode}: {host}" if host_valid else "Enter a valid hostname/IP or OpenSSH alias",
        )
    )

    user = str(config.get("ssh_user", "")).strip()
    user_valid = (not user or bool(_SAFE_USER.fullmatch(user))) and (mode != "direct" or bool(user))
    findings.append(
        _finding(
            "PASS" if user_valid else "BLOCK",
            "SSH username",
            user or ("Resolved by OpenSSH alias" if mode == "alias" else "Required for direct connections"),
        )
    )

    port = _integer(config.get("ssh_port", 0))
    findings.append(
        _finding("PASS" if 1 <= port <= 65535 else "BLOCK", "SSH port", str(port or "invalid"))
    )

    for field, label in (
        ("remote_root", "Remote job root"),
        ("topas_executable", "Server TOPAS"),
        ("geant4_environment_script", "Server environment script"),
        ("geant4_data_root", "Server Geant4 data"),
    ):
        value = str(config.get(field, ""))
        findings.append(
            _finding(
                "PASS" if _is_absolute_remote_path(value) else "BLOCK",
                label,
                value if _is_absolute_remote_path(value) else f"{field} must be a safe absolute server path",
            )
        )

    auth_mode = str(config.get("auth_mode", "agent")).strip().lower()
    identity = str(config.get("identity_file", "")).strip()
    auth_valid = auth_mode == "agent"
    auth_detail = "OpenSSH agent / macOS Keychain"
    if auth_mode == "identity_file":
        identity_path = Path(identity).expanduser()
        auth_valid = (
            identity_path.is_absolute()
            and identity_path.is_file()
            and identity_path.suffix.lower() != ".pub"
        )
        auth_detail = str(identity_path) if auth_valid else "Choose an existing private-key file"
        if auth_valid and identity_path.stat().st_mode & 0o077:
            findings.append(
                _finding(
                    "WARN",
                    "Identity-file permissions",
                    "The selected key is readable by group/others; OpenSSH may reject it",
                )
            )
    elif auth_mode != "agent":
        auth_valid = False
        auth_detail = "Choose SSH agent/Keychain or an existing identity file"
    findings.append(_finding("PASS" if auth_valid else "BLOCK", "Authentication", auth_detail))

    known_hosts = Path(str(config.get("known_hosts_path", "")))
    endpoint: dict[str, Any] | None = None
    endpoint_error = ""
    if host_valid:
        try:
            endpoint = _connection_endpoint(config)
        except RuntimeError as exc:
            endpoint_error = str(exc)
    records = _known_host_records(known_hosts, str(endpoint["lookup"])) if endpoint else []
    has_key = bool(records)
    findings.append(
        _finding(
            "PASS" if has_key else "BLOCK",
            "Pinned known_hosts",
            (
                f"{known_hosts} · {endpoint['lookup']}"
                if has_key and endpoint
                else endpoint_error or "Inspect and independently verify this server's host key"
            ),
        )
    )

    fingerprint = str(config.get("host_key_sha256", ""))
    fingerprint_valid = bool(re.fullmatch(r"SHA256:[A-Za-z0-9+/]{20,}={0,2}", fingerprint))
    findings.append(
        _finding(
            "PASS" if fingerprint_valid else "BLOCK",
            "Host-key fingerprint",
            fingerprint if fingerprint_valid else "Configure the independently verified SHA256 fingerprint",
        )
    )
    if has_key and fingerprint_valid:
        matches = any(record["fingerprint"] == fingerprint for record in records)
        findings.append(
            _finding(
                "PASS" if matches else "BLOCK",
                "Fingerprint verification",
                "Pinned key matches the confirmed SHA-256 fingerprint"
                if matches
                else "Pinned key does not match the confirmed SHA-256 fingerprint",
            )
        )

    ssh = shutil.which("ssh")
    rsync = shutil.which("rsync")
    findings.append(_finding("PASS" if ssh else "BLOCK", "Local OpenSSH", ssh or "ssh not found"))
    findings.append(_finding("PASS" if rsync else "BLOCK", "Local rsync", rsync or "rsync not found"))
    maximum = _integer(config.get("max_parallel_jobs", 0))
    findings.append(
        _finding(
            "PASS" if 1 <= maximum <= 32 else "BLOCK",
            "Remote parallel-job limit",
            str(maximum) if 1 <= maximum <= 32 else "max_parallel_jobs must be within 1–32",
        )
    )
    return findings


def config_ready(config: dict[str, Any], findings: Iterable[dict[str, str]]) -> bool:
    return config.get("enabled") is True and not any(item["level"] == "BLOCK" for item in findings)


def public_server_status(app_root: Path, case_root: Path | None = None) -> dict[str, Any]:
    config = load_server_config(app_root)
    findings = validate_server_config(app_root, config)
    bundles = discover_remote_bundles(case_root) if case_root else []
    return {
        "configured": config_ready(config, findings),
        "enabled": config.get("enabled") is True,
        "config": {
            "serverId": str(config.get("server_id", "")),
            "sshMode": _ssh_mode(config),
            "sshHost": _ssh_host(config),
            "sshUser": str(config.get("ssh_user", "")),
            "port": _integer(config.get("ssh_port", 0)),
            "authMode": str(config.get("auth_mode", "agent")),
            "identityFile": str(config.get("identity_file", "")),
            "remoteRoot": str(config.get("remote_root", "")),
            "topasExecutable": str(config.get("topas_executable", "")),
            "geant4EnvironmentScript": str(config.get("geant4_environment_script", "")),
            "geant4DataRoot": str(config.get("geant4_data_root", "")),
            "knownHosts": str(config.get("known_hosts_path", "")),
            "hostKeySha256": str(config.get("host_key_sha256", "")),
            "maxParallelJobs": _integer(config.get("max_parallel_jobs", 0)),
            "configPath": str(config.get("config_path", "")),
        },
        "findings": findings,
        "bundles": bundles,
        "canInspectHostKey": not any(
            "SSH hostname/IP" in error or "OpenSSH alias" in error or "SSH port" in error
            for error in _validate_editable_config(config)
        ),
        "policy": {
            "authentication": "OpenSSH agent / Keychain or an existing identity-file path; no password or private-key contents are stored",
            "hostKey": "StrictHostKeyChecking=yes with the project-pinned known_hosts file",
            "runtime": "Server-installed TOPAS and Geant4 only; local executables are never uploaded",
            "patientData": "Remote transport sends patient CT and generated TOPAS parameters; institutional approval is required",
        },
    }


def _ssh_base_command(app_root: Path, config: dict[str, Any]) -> list[str]:
    findings = validate_server_config(app_root, config)
    if not config_ready(config, findings):
        blocked = "; ".join(item["detail"] for item in findings if item["level"] == "BLOCK")
        if config.get("enabled") is not True:
            blocked = "server is disabled" + (f"; {blocked}" if blocked else "")
        raise RuntimeError(f"SSH server is not ready: {blocked}")
    ssh = shutil.which("ssh")
    if not ssh:
        raise RuntimeError("Local OpenSSH executable was not found")
    return [
        ssh,
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
        *(
            ["-i", str(config["identity_file"]), "-o", "IdentitiesOnly=yes"]
            if str(config.get("auth_mode", "agent")) == "identity_file"
            else []
        ),
        ssh_destination(config),
    ]


def _run_fixed_ssh(app_root: Path, remote_command: str, timeout: int) -> subprocess.CompletedProcess[str]:
    config = load_server_config(app_root)
    command = [*_ssh_base_command(app_root, config), remote_command]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"SSH connection timed out after {timeout} seconds") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "SSH exited without diagnostic output").strip()
        raise RuntimeError(f"SSH connection failed ({result.returncode}): {detail[:2000]}")
    return result


def test_connection(app_root: Path) -> dict[str, Any]:
    result = _run_fixed_ssh(
        app_root,
        "printf 'PLAN1699_SSH_OK\\n'; hostname 2>/dev/null || uname -n",
        DEFAULT_TIMEOUT_SECONDS,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines or lines[0] != "PLAN1699_SSH_OK":
        raise RuntimeError("Server returned an unexpected SSH handshake response")
    return {
        "connected": True,
        "host": lines[1] if len(lines) > 1 else "unknown",
        "message": "SSH connection and pinned server identity passed",
    }


def check_server_environment(app_root: Path) -> dict[str, Any]:
    config = load_server_config(app_root)
    # Values are configuration-owned, validated absolute paths, and quoted before
    # entering the fixed remote command.  No HTTP/user text reaches this shell.
    topas = shlex.quote(str(config["topas_executable"]))
    setup = shlex.quote(str(config["geant4_environment_script"]))
    g4_root = shlex.quote(str(config["geant4_data_root"]))
    remote_root = shlex.quote(str(config["remote_root"]))
    variable_words = " ".join(_GEANT4_DATA_VARIABLES)
    command = f"""set -u
topas={topas}
setup={setup}
g4_root={g4_root}
remote_root={remote_root}
emit() {{ printf '%s\\t%s\\n' "$1" "$2"; }}
emit host "$(hostname 2>/dev/null || uname -n)"
emit os "$(uname -srm 2>/dev/null || true)"
if [ -f "$setup" ]; then emit setup PASS; set +u; . "$setup"; set -u; else emit setup BLOCK; fi
export TOPAS_G4_DATA_DIR="$g4_root"
if [ -x "$topas" ]; then
  emit topas PASS
  emit topas_version "$("$topas" --version 2>&1 | sed -n '1p' || true)"
else emit topas BLOCK; fi
if [ -d "$g4_root" ]; then emit geant4_root PASS; else emit geant4_root BLOCK; fi
g4_ok=0
g4_missing=0
for variable in {variable_words}; do
  eval "value=\${{$variable-}}"
  if [ -n "$value" ] && [ -d "$value" ]; then g4_ok=$((g4_ok+1)); else g4_missing=$((g4_missing+1)); fi
done
emit geant4_datasets "$g4_ok/$((g4_ok+g4_missing))"
emit topas_g4_data_dir "$TOPAS_G4_DATA_DIR"
parent=$(dirname "$remote_root")
if [ -d "$remote_root" ] && [ -w "$remote_root" ]; then emit remote_root PASS
elif [ -d "$parent" ] && [ -w "$parent" ]; then emit remote_root CREATABLE
else emit remote_root BLOCK; fi
emit cpu "$(getconf _NPROCESSORS_ONLN 2>/dev/null || nproc 2>/dev/null || echo unknown)"
emit memory_kib "$(awk '/MemTotal/ {{print $2; exit}}' /proc/meminfo 2>/dev/null || sysctl -n hw.memsize 2>/dev/null || echo unknown)"
emit disk_kib "$(df -Pk "$remote_root" 2>/dev/null | awk 'NR==2 {{print $4}}' || df -Pk "$parent" 2>/dev/null | awk 'NR==2 {{print $4}}' || echo unknown)"
emit server_time_utc "$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
"""
    result = _run_fixed_ssh(app_root, command, 30)
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "\t" in line:
            key, value = line.split("\t", 1)
            values[key.strip()] = value.strip()
    findings = [
        _finding("PASS" if values.get("setup") == "PASS" else "BLOCK", "Geant4/TOPAS setup script", values.get("setup", "No result")),
        _finding("PASS" if values.get("topas") == "PASS" else "BLOCK", "Server TOPAS executable", values.get("topas_version") or values.get("topas", "No result")),
        _finding("PASS" if values.get("geant4_root") == "PASS" else "BLOCK", "Geant4 data root", values.get("geant4_root", "No result")),
        _finding("PASS" if values.get("remote_root") in {"PASS", "CREATABLE"} else "BLOCK", "Remote job root", values.get("remote_root", "No result")),
    ]
    dataset_value = values.get("geant4_datasets", "0/0")
    try:
        present, total = (int(item) for item in dataset_value.split("/", 1))
    except (TypeError, ValueError):
        present, total = 0, len(_GEANT4_DATA_VARIABLES)
    topas_data_ready = (
        values.get("geant4_root") == "PASS"
        and values.get("topas_g4_data_dir") == str(config["geant4_data_root"])
    )
    findings.append(
        _finding(
            "PASS" if topas_data_ready else "BLOCK",
            "TOPAS Geant4 data binding",
            (
                f"TOPAS_G4_DATA_DIR={values.get('topas_g4_data_dir', '<missing>')}; "
                f"{present}/{total} optional individual G4 dataset variables already exist"
            ),
        )
    )
    return {
        "ready": not any(item["level"] == "BLOCK" for item in findings),
        "values": values,
        "findings": findings,
        "message": "Server TOPAS/Geant4 environment passed" if not any(item["level"] == "BLOCK" for item in findings) else "Server environment has blocking findings",
    }


def discover_remote_bundles(case_root: Path | None) -> list[dict[str, Any]]:
    if case_root is None:
        return []
    analysis = case_root.resolve() / "analysis"
    if not analysis.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for manifest_path in analysis.rglob("remote_bundle_manifest.json"):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                continue
            records.append(
                {
                    "jobId": str(payload.get("job_id", "")),
                    "outputTag": str(payload.get("output_tag", "")),
                    "createdUtc": str(payload.get("created_utc", "")),
                    "path": str(manifest_path.parent),
                    "remoteJobDirectory": str(payload.get("remote", {}).get("job_directory", "")),
                    "ctBytes": int(payload.get("ct", {}).get("size_bytes", 0) or 0),
                    "topasBytes": int(payload.get("topas", {}).get("size_bytes", 0) or 0),
                    "serverId": str(payload.get("server_runtime", {}).get("server_id", "")),
                }
            )
        except (OSError, ValueError, TypeError):
            continue
    records.sort(key=lambda item: (item["createdUtc"], item["jobId"]), reverse=True)
    return records[:20]
