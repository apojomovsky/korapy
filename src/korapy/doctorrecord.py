"""The recorded last doctor run the dashboard attention band reads.

One small record beside the other state files: `korapy doctor`
records its checks here, and the snapshot layer reads them back
through the one reader in this module, so the page's poll reads a
local file and never runs a check (or a GitHub call) itself. Kept
apart from `doctor.py` so the read path stays light: recording needs
only the state dir, never the doctor's own seams.
"""

import json
import os
import time

from korapy import paths

#: The record file's own schema, beside the doctor report's: a bump
#: here changes only this file, never the printed report.
SCHEMA_VERSION = 1

#: The record file name inside the state dir, resolved here only.
LAST_RESULT_NAME = "doctor-last.json"


def last_result_path(env=None):
    """The last-doctor-run record: inside the state dir."""
    return os.path.join(paths.state_dir(env), LAST_RESULT_NAME)


def record_last_result(checks, at=None):
    """Record plain check dicts as the last doctor run, atomically.

    `checks` are `{"name", "status", "detail", "fix"}` dicts (the
    doctor verb converts its `Check` rows once, here). Returns the
    path. Raises OSError when the state dir cannot take the record;
    the verb reports that on stderr and still prints its report.
    """
    target = last_result_path()
    directory = os.path.dirname(target)
    if directory:
        os.makedirs(directory, exist_ok=True)
    moment = time.time() if at is None else float(at)
    document = {
        "schema_version": SCHEMA_VERSION,
        "at": moment,
        "checks": [
            {
                "name": row.get("name"),
                "status": row.get("status"),
                "detail": row.get("detail"),
                "fix": row.get("fix"),
            }
            for row in checks
        ],
    }
    paths.atomic_write_text(target, json.dumps(document, indent=2) + "\n")
    return target


def load_last_checks(path=None):
    """The recorded doctor checks, or None when no run was recorded.

    The one reader of the record: a missing file reads None (the
    band then reads `unknown`); an unreadable or wrong-shaped file
    raises, and the caller turns that into the same `unknown`
    instead of breaking the snapshot.
    """
    target = path if path is not None else last_result_path()
    try:
        with open(target, encoding="utf-8") as handle:
            document = json.load(handle)
    except FileNotFoundError:
        return None
    if not isinstance(document, dict) or not isinstance(document.get("checks"), list):
        raise ValueError(f"doctor record {target} is not a doctor result")
    checks = []
    for row in document["checks"]:
        if not isinstance(row, dict):
            raise ValueError(f"doctor record {target} holds a non-object check")
        checks.append(
            {
                "name": row.get("name"),
                "status": row.get("status"),
                "detail": row.get("detail"),
                "fix": row.get("fix"),
            }
        )
    return checks
