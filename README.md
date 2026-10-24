# expense-split

Split shared costs in a group, like a flat, a trip or a hackathon team, and see who owes whom. A command-line tool in plain Python with no dependencies. Groups are saved in `data/groups.json`.

## Running it

You need Python 3.8 or newer.

```
python3 -m split summary groupA
python3 -m split add-expense groupA "Pizza" 1200 priya
python3 -m split settle groupA ravi priya
python3 -m unittest discover tests
```

On Windows, use `python` instead of `python3`.

## Commands

| Command | Does |
|---|---|
| `summary <group>` | Members, expenses, and who owes whom |
| `validate-data` | Inspects stored group data, detects integrity errors, and reports diagnostics |
| `backup-data --output <file>` | Creates a validated, consistent snapshot of all groups with SHA-256 integrity verification |
| `restore-data <backup_file> (--dry-run\|--apply)` | Inspects or atomically restores group data from a validated backup |
| `add-expense <group> <description> <amount> <paid_by> [equal\|share]` | Records an expense. Split equally unless you say otherwise |
| `edit-expense <group> <expense_id> <description> <amount> <paid_by> [equal\|share]` | Updates an existing expense by its stable ID |
| `delete-expense <group> <expense_id>` | Deletes an expense by its stable ID |
| `settle <group> <payer> <receiver>` | Records that `payer` paid back what they owe `receiver` |

## How it's supposed to work

- **Data Integrity Validation**:
  - Run `python3 -m split validate-data` (or `python -m split validate-data` on Windows) to inspect persisted groups without modifying them.
  - Checks top-level structure, required group keys, valid member names, duplicate member detection, valid expense IDs, duplicate expense IDs, valid positive amounts in integer paise, valid payer and contributor references, split configurations and weights, and settlement records.
  - Read-only guarantee: `validate-data` never modifies, renumbers, or rewrites group or settlement data.
  - Returns exit code `0` when all groups pass validation, and exit code `1` when integrity errors are found. Useful before debugging balances or automating CI checks.
  - Example output:
    ```text
    ERROR group=flatmates record=expense id=12 field=paid_by
      Unknown payer 'david' is not a member of group 'flatmates'.
      Hint: Payer must be one of the group's members.

    Validation completed: 1 group checked, 1 error found.
    ```

- **Validated Backup and Atomic Restore**:
  - **Creating Backups**:
    - Run `python3 -m split backup-data --output backups/groups.json` (or `python -m split backup-data --output backups/groups.json` on Windows).
    - Preserves all groups, member rosters, single-payer and multiple-payer distributions, expense IDs (and un-identified legacy expenses), equal and weighted splits, archived members, and settlement histories.
    - Synchronized with live database locking to prevent concurrent writes from yielding an inconsistent snapshot.
    - Rejects output paths pointing to the live database file itself to protect active data.
    - Backup format specification: versioned envelope containing `backup_version` (`1`), UTC ISO `created_at` timestamp, deterministic canonical SHA-256 `checksum`, and the full `groups` dictionary.
  - **Restoring Data**:
    - Restoration supports full-replacement semantics (does not merge or guess diffs).
    - Requires exactly one mode flag: either `--dry-run` or `--apply`.
    - **Dry-run Mode (`--dry-run`)**: Run `python3 -m split restore-data backups/groups.json --dry-run` to inspect and validate the backup. Verifies backup envelope structure, validates the SHA-256 checksum, runs data integrity validation on candidate groups, and reports counts of groups, expenses, and settlements without modifying the live store or backup.
    - **Apply Mode (`--apply`)**: Run `python3 -m split restore-data backups/groups.json --apply` to execute atomic restoration.
    - **Failure Safety and Pre-Restore Recovery**:
      - Under an exclusive database lock, creates a pre-restore safety snapshot (named `<live>.pre-restore-<timestamp>.bak`) of the current live database before replacing it.
      - If backup checksum verification, data integrity validation, safety backup creation, or atomic write fails, the live store is left byte-for-byte unmodified.
      - Live database replacement is performed atomically via a synced temporary file (`os.replace`), preventing partial writes or database corruption.
  - **Handling Invalid or Corrupted Backups**:
    - Corrupted payload files, checksum mismatches, or candidate data violating integrity constraints (e.g., unknown payers or malformed splits) are rejected with clear diagnostics and exit code `1` before modifying live data:
      ```text
      Restore failed: Integrity check failed: checksum mismatch in 'backups/corrupted.json'. Expected a1b2..., calculated c3d4...
      ```

- Every expense is assigned a unique, stable ID within its group. Editing an expense preserves its ID, and deleting an expense leaves all other expense IDs unchanged (no renumbering).
- Legacy expenses without an explicit ID can be referenced by their 1-based order in the group.
- Editing or deleting an expense preserves the exact historical record of all settlements; balances and settlement plans are recalculated from remaining expenses and existing payments.
- An amount must be more than 0, and whoever paid must be a member of the group.
- Single-payer and multiple-payer expenses:
  - An expense can be paid by a single group member: `add-expense flatmates "Groceries" 900 karan`. In `groups.json`, this is stored as `"paid_by": "karan"`.
  - An expense can also be funded by multiple contributors with specified amounts: `add-expense flatmates "Dinner" 1200 karan=800,arjun=400`. In `groups.json`, this is stored as `"paid_by": {"karan": 800.0, "arjun": 400.0}`.
  - The sum of contributor contributions must equal the expense total exactly in integer paise.
  - Payer contributions specify who paid for the expense, which remains separate from the split (who owes or benefits from the expense).
  - Existing JSON files with single-payer strings remain fully supported without migration.
- An equal split keeps the paise, and the shares always add up to exactly the total. ₹1000 between three people is ₹333.34, ₹333.33 and ₹333.33.
- `share` splits by weights, given after it as `name=weight`: `add-expense flatmates "Groceries" 900 karan share karan=2 arjun=1` means Karan's share is twice Arjun's. Multiple contributors can also be combined with weighted splits: `add-expense flatmates "Groceries" 900 karan=600,arjun=300 share karan=2 arjun=1`.
- The summary shows each pair of people at most once, with the amount after everything is netted out. If Ravi owes Priya ₹300 and Priya owes Ravi ₹100, it shows "Ravi owes Priya ₹200.00".
- Nobody is ever shown owing themselves.
- After `settle ravi priya`, Ravi no longer owes Priya anything, and the summary shows that.

## Code

- `split/calc.py`: splitting and working out debts
- `split/io.py`: reading and writing `data/groups.json`
- `split/cli.py`: the commands
- `tests/`: tests, run with `python3 -m unittest discover tests`

## Contributing

Fork the repo, make your changes on a new branch, and open a pull request. Run the tests first.

If you find a bug, open an issue with the steps to reproduce it, what you expected, and what happened instead.

Part of Source Start by CSI SPIT. MIT licensed.
