# SPDX-License-Identifier: Apache-2.0
"""Original offline snapshot identity tool; never executes or imports SavedVariables.

This records file identities, not ledger correctness, officer approval or GitHub freshness.
"""

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path

MAX_EXPORT_BYTES = 16 * 1024 * 1024
MAX_SAVED_BYTES = 64 * 1024 * 1024


def bounded_read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    if len(content) > limit:
        raise ValueError(f"{path.name} exceeds the {limit}-byte read limit")
    if not content:
        raise ValueError(f"{path.name} is empty")
    return content


def unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def invalid_number(value: str) -> None:
    raise ValueError(f"Non-finite JSON number: {value}")


def finite_float(value: str) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite JSON number")
    return result


def decode_json(content: bytes) -> dict:
    result = json.loads(
        content.decode("utf-8-sig"),
        object_pairs_hook=unique_object,
        parse_constant=invalid_number,
        parse_float=finite_float,
    )
    if not isinstance(result, dict):
        raise ValueError("JSON root must be an object")
    return result


def read_export(path: Path) -> tuple[bytes, dict]:
    # Text editors may append a newline/BOM. Interior bytes are never reformatted.
    content = bounded_read(path, MAX_EXPORT_BYTES)
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]
    content = content.strip(b" \t\r\n")
    document = decode_json(content)
    check_export(document)
    return content, document


def check_export(document: dict) -> None:
    if document.get("format") != "CappedDKP" or type(document.get("exportVersion")) is not int \
            or document["exportVersion"] != 1:
        raise ValueError("Use the complete /cdkp export|json version-1 journal")
    if "testMode" in document or "scenario" in document:
        raise ValueError("Practice exports cannot identify an official snapshot")
    database = document.get("database")
    if not isinstance(database, dict) or type(database.get("version")) is not int \
            or database["version"] != 1 or not isinstance(database.get("events"), list):
        raise ValueError("Expected version-1 database with a journal event array")
    for index, event in enumerate(database["events"], 1):
        if not isinstance(event, dict) or type(event.get("id")) is not int or event["id"] != index:
            raise ValueError(f"Gap, duplicate or invalid event identity at row {index}")
        if type(event.get("version")) is not int or event["version"] != 1 \
                or type(event.get("time")) is not int or event["time"] < 0 \
                or not isinstance(event.get("data"), dict) \
                or not isinstance(event.get("kind"), str) or not event["kind"] \
                or not isinstance(event.get("actor"), str) or not event["actor"]:
            raise ValueError(f"Invalid event envelope at row {index}")


def export_identity(path: Path) -> tuple[int, str]:
    content, document = read_export(path)
    return len(document["database"]["events"]), hashlib.sha256(content).hexdigest()


def compare_exports(approved_path: Path, local_path: Path) -> dict:
    approved_bytes, approved = read_export(approved_path)
    local_bytes, local = read_export(local_path)
    approved_events = approved["database"]["events"]
    local_events = local["database"]["events"]
    # Compare every stored field, including historic results, not just event IDs.
    def exact(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)

    first_conflict = next(
        (index for index, (left, right) in enumerate(zip(approved_events, local_events), 1)
         if exact(left) != exact(right)), None
    )
    approved["database"]["events"] = []
    local["database"]["events"] = []
    if exact(approved) != exact(local):
        status = "metadata-conflict"
    elif first_conflict is not None:
        status = "conflicting-history"
    elif len(local_events) > len(approved_events):
        status = "local-unsubmitted-events"
    elif len(local_events) < len(approved_events):
        status = "approved-extension"
    elif approved_bytes == local_bytes:
        status = "identical"
    else:
        status = "encoding-differs"
    return {
        "status": status, "firstConflictingEvent": first_conflict,
        "approvedEvents": len(approved_events), "localEvents": len(local_events),
        "approvedJournalSha256": hashlib.sha256(approved_bytes).hexdigest(),
        "localJournalSha256": hashlib.sha256(local_bytes).hexdigest(),
        "scope": "read-only structural comparison; no replay, approval, freshness, file pairing or copy authorization",
    }


def revision(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{40}", value):
        raise ValueError("Revision must be the full 40-hex commit checked manually on GitHub")
    return value.lower()


def identity(export_path: Path, saved_path: Path, commit: str) -> dict:
    count, journal_hash = export_identity(export_path)
    saved_hash = hashlib.sha256(bounded_read(saved_path, MAX_SAVED_BYTES)).hexdigest()
    return {
        "format": "CappedDKP-snapshot-identity",
        "version": 1,
        "reviewedRevision": revision(commit),
        "lastEventId": count,
        "journalSha256": journal_hash,
        "savedVariablesSha256": saved_hash,
        "scope": "identity-only; no replay, approval, freshness or SavedVariables/journal pairing validation",
    }


def verify(manifest: dict, actual: dict) -> None:
    for key, value in actual.items():
        if type(manifest.get(key)) is not type(value) or manifest[key] != value:
            raise ValueError(f"Snapshot identity mismatch: {key}; preserve both snapshots and reconcile")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("create", "verify"):
        sub = commands.add_parser(name)
        sub.add_argument("--export-json", type=Path, required=True)
        sub.add_argument("--saved-variables", type=Path, required=True)
        sub.add_argument("--revision", required=True, help="Manually checked approved GitHub commit; not queried")
        sub.add_argument("--manifest", type=Path, required=True)
    compare = commands.add_parser("compare", help="Compare approved and local JSON history without replacing anything")
    compare.add_argument("--approved-json", type=Path, required=True)
    compare.add_argument("--local-json", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "compare":
            result = compare_exports(args.approved_json, args.local_json)
            print(json.dumps(result, indent=2))
            print("PRESERVE BOTH snapshots. Never overwrite local-only events; reconcile with officers.")
            print("An approved extension still needs independent review, backup and successful addon replay.")
            return 0 if result["status"] == "identical" else 2
        actual = identity(args.export_json, args.saved_variables, args.revision)
        if args.command == "create":
            # Exclusive create also protects an accidentally selected input file.
            with args.manifest.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(actual, indent=2) + "\n")
        else:
            verify(decode_json(bounded_read(args.manifest, MAX_EXPORT_BYTES)), actual)
        print("IDENTITY ONLY: no approval, replay, freshness or file/journal pairing verification.")
        print(f"Last event ID: {actual['lastEventId']}")
        print(f"SavedVariables SHA-256: {actual['savedVariablesSha256']}")
        print("Compare in game (does not import files or authenticate approval):")
        print(f"/cdkp snapshotcheck|{actual['reviewedRevision']}|"
              f"{actual['lastEventId']}|{actual['journalSha256']}")
        return 0
    except (OSError, ValueError, RecursionError) as error:
        print(f"Snapshot ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
