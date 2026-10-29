"""Validated backup and atomic restore workflow for group data."""

import copy
import datetime
import hashlib
import json
import os
import tempfile
from pathlib import Path

from split.io import (
    load_all_groups,
    _atomic_write_json,
    _exclusive_database_lock,
    DEFAULT_DATA_PATH,
)
from split.validation import validate_store, Diagnostic

BACKUP_FORMAT_VERSION = 1


def compute_canonical_sha256(data):
    """Compute deterministic SHA-256 hex digest of JSON-serializable data.

    Uses sort_keys=True, separators=(',', ':'), ensure_ascii=False, and utf-8 encoding.
    """
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def create_backup_payload(groups_data):
    """Construct a versioned backup payload with integrity checksum.

    groups_data: dictionary mapping group IDs to group records.
    Returns: dictionary containing backup metadata, SHA-256 checksum, and group store.
    """
    checksum = compute_canonical_sha256(groups_data)
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    return {
        "backup_version": BACKUP_FORMAT_VERSION,
        "created_at": timestamp,
        "checksum": checksum,
        "groups": groups_data,
    }


def is_same_file(path_a, path_b):
    """Check if two paths point to the exact same filesystem file."""
    pa = Path(path_a).resolve()
    pb = Path(path_b).resolve()
    if pa == pb:
        return True
    try:
        if pa.exists() and pb.exists() and os.path.samefile(pa, pb):
            return True
    except (OSError, ValueError):
        pass
    return False


def create_backup(live_path=DEFAULT_DATA_PATH, output_path=None):
    """Create a consistent, verified backup of live_path to output_path.

    Parameters:
    - live_path: path to live store JSON file.
    - output_path: destination path for backup.

    Returns dict:
    - success: bool
    - error: str or None
    - backup_path: Path or None
    - groups_count: int
    - checksum: str or None
    """
    live = Path(DEFAULT_DATA_PATH if live_path is None else live_path)
    if output_path is None:
        return {"success": False, "error": "Output backup path must be specified."}

    out = Path(output_path)

    # Prevent overwriting live store with backup output
    if is_same_file(live, out):
        return {
            "success": False,
            "error": f"Backup destination '{out}' cannot point to the live database file.",
        }

    # Lock live store during snapshot read
    try:
        with _exclusive_database_lock(live):
            if not live.exists():
                return {
                    "success": False,
                    "error": f"Live database file not found at '{live}'.",
                }
            try:
                groups_data = load_all_groups(live)
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to read live database at '{live}': {e}",
                }

            # Optional: ensure groups_data is valid dictionary
            if not isinstance(groups_data, dict):
                return {
                    "success": False,
                    "error": f"Live store root must be an object, got {type(groups_data).__name__}.",
                }

            payload = create_backup_payload(groups_data)
            groups_count = len(groups_data)

            # Atomic write to output path
            try:
                _atomic_write_json(payload, out)
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to write backup to '{out}': {e}",
                }

            # Verify written backup immediately
            try:
                with open(out, "r", encoding="utf-8") as f:
                    written = json.load(f)
                read_checksum = compute_canonical_sha256(written.get("groups"))
                if read_checksum != payload["checksum"]:
                    return {
                        "success": False,
                        "error": "Integrity check failed: written backup checksum does not match.",
                    }
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to verify backup at '{out}': {e}",
                }

            return {
                "success": True,
                "error": None,
                "backup_path": out,
                "groups_count": groups_count,
                "checksum": payload["checksum"],
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Error acquiring database lock on '{live}': {e}",
        }


def load_and_inspect_backup(backup_path):
    """Load and verify backup payload structure and SHA-256 integrity.

    Returns dict:
    - valid: bool
    - error: str or None
    - backup_version: int or None
    - created_at: str or None
    - checksum: str or None
    - groups: dict or None
    """
    path = Path(backup_path)
    if not path.exists():
        return {
            "valid": False,
            "error": f"Backup file not found at '{path}'.",
            "groups": None,
        }

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return {
            "valid": False,
            "error": f"Malformed JSON in backup file '{path}': {e}",
            "groups": None,
        }
    except Exception as e:
        return {
            "valid": False,
            "error": f"Could not read backup file '{path}': {e}",
            "groups": None,
        }

    if not isinstance(data, dict):
        return {
            "valid": False,
            "error": f"Backup payload must be a JSON object, got {type(data).__name__}.",
            "groups": None,
        }

    # Check required fields: backup_version, checksum, groups
    backup_ver = data.get("backup_version")
    if backup_ver != BACKUP_FORMAT_VERSION:
        return {
            "valid": False,
            "error": f"Unsupported backup format version {backup_ver!r}. Expected version {BACKUP_FORMAT_VERSION}.",
            "groups": None,
        }

    expected_checksum = data.get("checksum")
    if not isinstance(expected_checksum, str) or not expected_checksum.strip():
        return {
            "valid": False,
            "error": "Backup payload is missing or has invalid 'checksum' field.",
            "groups": None,
        }

    groups = data.get("groups")
    if not isinstance(groups, dict):
        return {
            "valid": False,
            "error": f"Backup 'groups' field must be a JSON object, got {type(groups).__name__}.",
            "groups": None,
        }

    # Verify SHA-256 checksum
    actual_checksum = compute_canonical_sha256(groups)
    if actual_checksum.lower() != expected_checksum.lower():
        return {
            "valid": False,
            "error": (
                f"Integrity check failed: checksum mismatch in '{path}'. "
                f"Expected {expected_checksum}, calculated {actual_checksum}."
            ),
            "groups": None,
        }

    return {
        "valid": True,
        "error": None,
        "backup_version": backup_ver,
        "created_at": data.get("created_at"),
        "checksum": actual_checksum,
        "groups": groups,
    }


def validate_candidate_groups(groups_data):
    """Validate candidate groups using existing validation layer.

    Returns: (is_valid, diagnostics, stats)
    """
    diagnostics, stats = validate_store(groups_data)
    is_valid = (len(diagnostics) == 0)
    return is_valid, diagnostics, stats


def create_safety_backup(live_path):
    """Create a safety recovery backup of live store before replacement.

    Saves to: <live_path_parent>/<live_path_name>.pre-restore-<timestamp>.bak
    Returns: Path of safety backup.
    Raises: Exception on failure.
    """
    path = Path(live_path)
    if not path.exists():
        return None

    # Load live store and serialize safety backup
    live_data = load_all_groups(path)
    safety_payload = create_backup_payload(live_data)

    ts_str = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
    safety_path = path.parent / f"{path.name}.pre-restore-{ts_str}.bak"

    _atomic_write_json(safety_payload, safety_path)

    # Verify safety backup is readable and matches checksum
    with open(safety_path, "r", encoding="utf-8") as f:
        verify_data = json.load(f)
    if compute_canonical_sha256(verify_data.get("groups")) != safety_payload["checksum"]:
        raise RuntimeError("Safety backup verification failed.")

    return safety_path


def restore_backup(backup_path, live_path=DEFAULT_DATA_PATH, dry_run=True):
    """Restore group data from a validated backup file.

    Parameters:
    - backup_path: path to backup file
    - live_path: path to destination live store
    - dry_run: if True, simulate and validate without modifying store;
               if False, atomically replace live store after creating safety backup.

    Returns dict:
    - success: bool
    - dry_run: bool
    - error: str or None
    - diagnostics: list of Diagnostic objects
    - stats: dict of validation stats
    - groups_count: int
    - expenses_count: int
    - settlements_count: int
    - safety_backup_path: Path or None
    """
    live = Path(DEFAULT_DATA_PATH if live_path is None else live_path)
    b_path = Path(backup_path)

    # Prevent restoring if backup_path is the same file as live store
    if is_same_file(b_path, live):
        return {
            "success": False,
            "dry_run": dry_run,
            "error": f"Backup file '{b_path}' cannot be the same file as the live database.",
            "diagnostics": [],
            "stats": {},
            "groups_count": 0,
            "expenses_count": 0,
            "settlements_count": 0,
            "safety_backup_path": None,
        }

    # Step 1: Read and inspect backup format & checksum
    inspection = load_and_inspect_backup(b_path)
    if not inspection["valid"]:
        return {
            "success": False,
            "dry_run": dry_run,
            "error": inspection["error"],
            "diagnostics": [],
            "stats": {},
            "groups_count": 0,
            "expenses_count": 0,
            "settlements_count": 0,
            "safety_backup_path": None,
        }

    candidate_groups = inspection["groups"]

    # Step 2: Validate candidate data integrity
    is_valid, diagnostics, stats = validate_candidate_groups(candidate_groups)
    if not is_valid:
        return {
            "success": False,
            "dry_run": dry_run,
            "error": f"Backup data failed integrity validation with {len(diagnostics)} error(s).",
            "diagnostics": diagnostics,
            "stats": stats,
            "groups_count": stats.get("groups_checked", len(candidate_groups)),
            "expenses_count": stats.get("expenses_checked", 0),
            "settlements_count": stats.get("settlements_checked", 0),
            "safety_backup_path": None,
        }

    groups_count = stats.get("groups_checked", len(candidate_groups))
    expenses_count = stats.get("expenses_checked", 0)
    settlements_count = stats.get("settlements_checked", 0)

    if dry_run:
        return {
            "success": True,
            "dry_run": True,
            "error": None,
            "diagnostics": [],
            "stats": stats,
            "groups_count": groups_count,
            "expenses_count": expenses_count,
            "settlements_count": settlements_count,
            "safety_backup_path": None,
        }

    # Step 3: Apply mode under exclusive database lock
    try:
        with _exclusive_database_lock(live):
            # Re-verify backup in case it changed between inspection and lock
            re_inspection = load_and_inspect_backup(b_path)
            if not re_inspection["valid"]:
                return {
                    "success": False,
                    "dry_run": False,
                    "error": re_inspection["error"],
                    "diagnostics": [],
                    "stats": {},
                    "groups_count": 0,
                    "expenses_count": 0,
                    "settlements_count": 0,
                    "safety_backup_path": None,
                }

            re_candidate = re_inspection["groups"]
            re_valid, re_diags, re_stats = validate_candidate_groups(re_candidate)
            if not re_valid:
                return {
                    "success": False,
                    "dry_run": False,
                    "error": f"Backup data failed integrity validation with {len(re_diags)} error(s).",
                    "diagnostics": re_diags,
                    "stats": re_stats,
                    "groups_count": len(re_candidate),
                    "expenses_count": 0,
                    "settlements_count": 0,
                    "safety_backup_path": None,
                }

            # Create safety backup of current live store before replacement
            try:
                safety_path = create_safety_backup(live)
            except Exception as e:
                return {
                    "success": False,
                    "dry_run": False,
                    "error": f"Failed to create pre-restore safety backup: {e}",
                    "diagnostics": [],
                    "stats": {},
                    "groups_count": groups_count,
                    "expenses_count": expenses_count,
                    "settlements_count": settlements_count,
                    "safety_backup_path": None,
                }

            # Atomically replace live store with restored candidate groups
            try:
                _atomic_write_json(re_candidate, live)
            except Exception as e:
                return {
                    "success": False,
                    "dry_run": False,
                    "error": f"Failed to write restored data to '{live}': {e}",
                    "diagnostics": [],
                    "stats": {},
                    "groups_count": groups_count,
                    "expenses_count": expenses_count,
                    "settlements_count": settlements_count,
                    "safety_backup_path": safety_path,
                }

            # Verify written live store
            try:
                written_live = load_all_groups(live)
                if compute_canonical_sha256(written_live) != re_inspection["checksum"]:
                    raise RuntimeError("Verification failed: live store does not match backup checksum.")
            except Exception as e:
                return {
                    "success": False,
                    "dry_run": False,
                    "error": f"Post-restore verification error: {e}",
                    "diagnostics": [],
                    "stats": {},
                    "groups_count": groups_count,
                    "expenses_count": expenses_count,
                    "settlements_count": settlements_count,
                    "safety_backup_path": safety_path,
                }

            return {
                "success": True,
                "dry_run": False,
                "error": None,
                "diagnostics": [],
                "stats": re_stats,
                "groups_count": groups_count,
                "expenses_count": expenses_count,
                "settlements_count": settlements_count,
                "safety_backup_path": safety_path,
            }
    except Exception as e:
        return {
            "success": False,
            "dry_run": False,
            "error": f"Error acquiring database lock on '{live}': {e}",
            "diagnostics": [],
            "stats": {},
            "groups_count": 0,
            "expenses_count": 0,
            "settlements_count": 0,
            "safety_backup_path": None,
        }

