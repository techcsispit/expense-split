"""Schema versioning and migration utilities for group records."""

import copy
from split.validation import validate_store, Diagnostic
from split.io import (
    load_all_groups,
    _atomic_write_json,
    _exclusive_database_lock,
    DEFAULT_DATA_PATH,
)

# Supported schema versions
# Legacy groups without a schema_version key are treated as unversioned (None).
# Version 1 is the canonical versioned schema for groups.
CURRENT_SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = {1}


def get_group_schema_version(group):
    """Return the schema_version of a group.
    
    Returns None if unversioned (legacy).
    """
    if not isinstance(group, dict):
        return None
    return group.get("schema_version")


def is_valid_schema_version_format(version):
    """Check if version is a valid schema version format (positive integer or None for unversioned)."""
    if version is None:
        return True
    if isinstance(version, bool):
        return False
    if isinstance(version, int) and version > 0:
        return True
    return False


def inspect_store_versions(groups_data):
    """Analyze the schema versions of groups in groups_data.

    Returns a dict with:
    - total_groups: count of groups
    - unversioned_groups: list of group_ids with no schema_version
    - version_counts: dict of version -> count (None key for unversioned)
    - future_version_groups: list of (group_id, version) where version > CURRENT_SCHEMA_VERSION
    - invalid_version_groups: list of (group_id, raw_version)
    - upgradable_groups: list of (group_id, current_version, target_version)
    """
    summary = {
        "total_groups": 0,
        "unversioned_groups": [],
        "version_counts": {},
        "future_version_groups": [],
        "invalid_version_groups": [],
        "upgradable_groups": [],
    }

    if not isinstance(groups_data, dict):
        return summary

    for group_id, group in groups_data.items():
        summary["total_groups"] += 1
        if not isinstance(group, dict):
            continue

        raw_version = group.get("schema_version")

        # Track version counts
        summary["version_counts"][raw_version] = summary["version_counts"].get(raw_version, 0) + 1

        if raw_version is None:
            summary["unversioned_groups"].append(group_id)
            summary["upgradable_groups"].append((group_id, None, CURRENT_SCHEMA_VERSION))
        elif isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version <= 0:
            summary["invalid_version_groups"].append((group_id, raw_version))
        elif raw_version > CURRENT_SCHEMA_VERSION:
            summary["future_version_groups"].append((group_id, raw_version))
        elif raw_version in SUPPORTED_SCHEMA_VERSIONS:
            # Current supported version
            if raw_version < CURRENT_SCHEMA_VERSION:
                # Upgradable if we ever introduce version 2, etc.
                summary["upgradable_groups"].append((group_id, raw_version, CURRENT_SCHEMA_VERSION))
        else:
            summary["invalid_version_groups"].append((group_id, raw_version))

    return summary


def migrate_unversioned_to_v1(group):
    """Deterministic migration of an unversioned legacy group to schema version 1.

    Preserves:
    - All existing fields, including optional metadata (archived_members, etc.)
    - All expenses with their IDs or legacy un-identified state
    - Single-payer strings and multi-payer dicts
    - Split types and share weights
    - Settlement records
    - Inserts 'schema_version': 1
    """
    migrated = copy.deepcopy(group)
    migrated["schema_version"] = 1
    return migrated


# Registry of migration functions: (source_version, target_version) -> migration_fn
# source_version is None for legacy unversioned groups
MIGRATION_REGISTRY = {
    (None, 1): migrate_unversioned_to_v1,
}


class MigrationError(Exception):
    """Raised when migration cannot safely proceed."""
    pass


def plan_migration(groups_data):
    """Plan migration for a loaded store dictionary.

    Returns:
    {
        "status": "ready" | "no_op" | "error",
        "errors": [str, ...],
        "warnings": [str, ...],
        "plans": [
            {
                "group_id": group_id,
                "source_version": source_version,
                "target_version": target_version,
                "migrated_group": dict,
            },
            ...
        ],
        "proposed_store": dict (full store with migrated groups, or None if error),
        "validation_diagnostics": list of Diagnostic objects (if validation fails)
    }
    """
    result = {
        "status": "ready",
        "errors": [],
        "warnings": [],
        "plans": [],
        "proposed_store": None,
        "validation_diagnostics": [],
    }

    if not isinstance(groups_data, dict):
        result["status"] = "error"
        result["errors"].append("Top-level store must be a dictionary.")
        return result

    # 1. Inspect versions
    summary = inspect_store_versions(groups_data)

    if summary["invalid_version_groups"]:
        result["status"] = "error"
        for gid, ver in summary["invalid_version_groups"]:
            result["errors"].append(
                f"Group '{gid}' has invalid schema_version {ver!r}. Expected positive integer or omitted."
            )

    if summary["future_version_groups"]:
        result["status"] = "error"
        for gid, ver in summary["future_version_groups"]:
            result["errors"].append(
                f"Group '{gid}' has unsupported future schema_version {ver}. "
                f"Current supported maximum version is {CURRENT_SCHEMA_VERSION}."
            )

    # If there are already version errors, return early
    if result["errors"]:
        return result

    # 2. Pre-migration validation: validate current data integrity
    pre_diags, _ = validate_store(groups_data)
    if pre_diags:
        result["status"] = "error"
        result["validation_diagnostics"] = pre_diags
        result["errors"].append(
            f"Pre-migration validation failed with {len(pre_diags)} error(s). "
            "Data must pass integrity checks before migration."
        )
        return result

    # 3. Check if any migration needed
    if not summary["upgradable_groups"]:
        result["status"] = "no_op"
        result["proposed_store"] = copy.deepcopy(groups_data)
        return result

    # 4. Construct proposed migrated store deterministically
    proposed_store = copy.deepcopy(groups_data)
    for group_id, src_ver, tgt_ver in summary["upgradable_groups"]:
        group = proposed_store[group_id]
        trans_key = (src_ver, tgt_ver)
        migration_fn = MIGRATION_REGISTRY.get(trans_key)
        if not migration_fn:
            result["status"] = "error"
            result["errors"].append(
                f"No migration path registered for transition {src_ver!r} -> {tgt_ver} in group '{group_id}'."
            )
            return result

        migrated_group = migration_fn(group)
        proposed_store[group_id] = migrated_group
        result["plans"].append({
            "group_id": group_id,
            "source_version": src_ver,
            "target_version": tgt_ver,
            "migrated_group": migrated_group,
        })

    # 5. Post-migration validation: ensure the proposed store is completely valid
    post_diags, _ = validate_store(proposed_store)
    if post_diags:
        result["status"] = "error"
        result["validation_diagnostics"] = post_diags
        result["errors"].append(
            f"Post-migration validation failed with {len(post_diags)} error(s). "
            "Migrated dataset did not pass integrity validation."
        )
        return result

    result["proposed_store"] = proposed_store
    return result


def execute_migration(filepath=DEFAULT_DATA_PATH, dry_run=True):
    """Execute or simulate migration with database locking and failure safety.

    Parameters:
    - filepath: path to groups JSON file
    - dry_run: if True, do not modify the file; if False, atomically write migrated data

    Returns dict with:
    - success: bool
    - dry_run: bool
    - status: "ready" | "no_op" | "applied" | "error"
    - plans: list of migration plan items
    - errors: list of error strings
    - warnings: list of warning strings
    - diagnostics: list of Diagnostic objects
    """
    path = DEFAULT_DATA_PATH if filepath is None else filepath

    with _exclusive_database_lock(path):
        current_data = load_all_groups(path)
        plan_result = plan_migration(current_data)

        if plan_result["status"] == "error":
            return {
                "success": False,
                "dry_run": dry_run,
                "status": "error",
                "plans": plan_result["plans"],
                "errors": plan_result["errors"],
                "warnings": plan_result["warnings"],
                "diagnostics": plan_result["validation_diagnostics"],
            }

        if plan_result["status"] == "no_op":
            return {
                "success": True,
                "dry_run": dry_run,
                "status": "no_op",
                "plans": [],
                "errors": [],
                "warnings": plan_result["warnings"],
                "diagnostics": [],
            }

        if dry_run:
            return {
                "success": True,
                "dry_run": True,
                "status": "ready",
                "plans": plan_result["plans"],
                "errors": [],
                "warnings": plan_result["warnings"],
                "diagnostics": [],
            }

        # Apply changes atomically
        _atomic_write_json(plan_result["proposed_store"], path)
        return {
            "success": True,
            "dry_run": False,
            "status": "applied",
            "plans": plan_result["plans"],
            "errors": [],
            "warnings": plan_result["warnings"],
            "diagnostics": [],
        }

