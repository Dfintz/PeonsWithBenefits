# SPDX-License-Identifier: Apache-2.0
"""Offline Raid-Helper bridge for CappedDKP. Original code; read-only toward Raid-Helper.

fetch  GET-only download of balances and events into JSON files (needs RAIDHELPER_API_KEY).
build  Offline conversion of those JSON files into CappedDKP\\RaidHelperData.lua.

The addon has no network access, and this tool never sends a balance back to Raid-Helper.
The API key is read only from the environment and is never written to any output.
"""

import argparse
import datetime
import json
import math
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FORMAT = "capped-dkp-raidhelper"
VERSION = 1
BASE_URL = "https://raid-helper.xyz/api/v4"
API_DOMAINS = ("raid-helper.xyz", "raid-helper.dev")
KEY_VARIABLE = "RAIDHELPER_API_KEY"
LIMITS = {"dkp": 400, "events": 120, "signups": 60, "facts": 200}
MAX_VALUE = 1_000_000
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
MAX_PAGES = 20
TIMEOUT_SECONDS = 30
LUA_KEYWORDS = {
    "and", "break", "do", "else", "elseif", "end", "false", "for", "function", "if", "in",
    "local", "nil", "not", "or", "repeat", "return", "then", "true", "until", "while",
}


class BridgeError(Exception):
    pass


def clean(value, limit):
    """Mirror RaidHelper.lua: strip controls, collapse spaces, cut on a UTF-8 boundary.

    Pipes are left alone because the addon doubles them when it loads the file.
    """
    if not isinstance(value, str):
        return None
    text = "".join(" " if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in value)
    text = " ".join(text.split())
    raw = text.encode("utf-8")
    if len(raw) > limit:
        cut = limit
        while cut > 0 and 0x80 <= raw[cut] < 0xC0:
            cut -= 1
        text = raw[:cut].decode("utf-8").strip()
    return text or None


def discord_id(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9]{17,20}", value) is not None


def event_id(value):
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    return value if isinstance(value, str) and re.fullmatch(r"[0-9]{1,24}", value) else None


def number(value):
    """Raid-Helper sends numbers as strings; return a finite, bounded number or None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not re.fullmatch(r"[+-]?[0-9]+(\.[0-9]+)?", value):
            return None
        value = float(value)
    if not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > MAX_VALUE:
        return None
    return int(value) if value == int(value) else float(value)


def unix_seconds(value):
    """Accept unix seconds, unix milliseconds or an ISO-8601 string; else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[0-9]+(\.[0-9]+)?", text):
            value = float(text)
        else:
            try:
                parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)
            value = parsed.timestamp()
    if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        return None
    if value > 1e11:
        value /= 1000
    return int(value) if value < 4_102_444_800 else None


def reject_constant(text):
    raise ValueError(f"Non-finite JSON number: {text}")


def read_json(path):
    path = Path(path)
    with path.open("rb") as stream:
        content = stream.read(MAX_INPUT_BYTES + 1)
    if len(content) > MAX_INPUT_BYTES:
        raise BridgeError(f"{path.name} is larger than {MAX_INPUT_BYTES} bytes")
    try:
        return json.loads(content.decode("utf-8-sig"), parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError) as error:
        raise BridgeError(f"{path.name} is not valid JSON: {error}") from None


def rows_of(document, *names):
    if isinstance(document, list):
        return document
    if isinstance(document, dict):
        for name in names:
            if isinstance(document.get(name), list):
                return document[name]
    raise BridgeError("Expected a list or an object containing " + " or ".join(names))


def normalize_dkp(document):
    raw = rows_of(document, "result", "dkp", "members")
    rows = []
    for entry in raw:
        if not isinstance(entry, dict) or not discord_id(entry.get("id")):
            continue
        value = number(entry.get("dkp", entry.get("value")))
        if value is None:
            continue
        rows.append({"id": entry["id"], "name": clean(entry.get("name"), 40) or "unnamed", "value": value})
    rows.sort(key=lambda row: (row["name"].lower(), row["id"]))
    return rows[: LIMITS["dkp"]], len(raw)


def normalize_signups(value):
    kept, total = [], 0
    for entry in value if isinstance(value, list) else []:
        total += 1
        if not isinstance(entry, dict) or not discord_id(str(entry.get("userId", ""))):
            continue
        if len(kept) >= LIMITS["signups"]:
            continue
        row = {"userId": str(entry["userId"]), "name": clean(entry.get("name"), 40) or "unnamed"}
        for target, source, limit in (
            ("class", "className", 24), ("spec", "specName", 24), ("role", "roleName", 24), ("status", "status", 16),
        ):
            text = clean(entry.get(source), limit)
            if text:
                row[target] = text
        kept.append(row)
    return kept, total


def normalize_events(document):
    raw = rows_of(document, "postedEvents", "events")
    events = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        identifier = event_id(entry.get("id", entry.get("eventId")))
        start = unix_seconds(entry.get("startTime", entry.get("startTimeUnix", entry.get("start"))))
        if identifier is None or start is None:
            continue
        signups, total = normalize_signups(entry.get("signUps", entry.get("signups")))
        row = {"id": identifier, "title": clean(entry.get("title"), 80) or "Untitled event", "start": start}
        finish = unix_seconds(entry.get("endTime", entry.get("endTimeUnix")))
        closing = unix_seconds(entry.get("closeTime", entry.get("closingTime", entry.get("closingTimeUnix"))))
        if finish:
            row["end"] = finish
        if closing:
            row["closing"] = closing
        for target, source, limit in (("channel", "channelName", 40), ("leader", "leaderName", 40)):
            text = clean(entry.get(source), limit)
            if text:
                row[target] = text
        row["signupTotal"] = total
        row["signups"] = signups
        events.append(row)
    events.sort(key=lambda row: (row["start"], row["id"]))
    return events[-LIMITS["events"]:], len(raw)


def normalize_facts(document):
    if document is None:
        return [], 0
    raw = rows_of(document, "facts")
    facts = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        moment = unix_seconds(entry.get("time"))
        text = clean(entry.get("text"), 160)
        if moment is None or not text:
            continue
        row = {"source": clean(entry.get("source"), 24) or "external", "time": moment, "text": text}
        encounter = clean(entry.get("encounter"), 60)
        if encounter:
            row["encounter"] = encounter
        facts.append(row)
    facts.sort(key=lambda row: (row["time"], row["text"]))
    return facts[-LIMITS["facts"]:], len(raw)


def build_bundle(dkp_document, events_document, facts_document, server, generated):
    server_text = clean(str(server), 24)
    if not server_text or not re.fullmatch(r"[0-9]{1,24}", server_text):
        raise BridgeError("--server must be the numeric Discord server ID")
    dkp, dkp_total = normalize_dkp(dkp_document) if dkp_document is not None else ([], 0)
    events, event_total = normalize_events(events_document) if events_document is not None else ([], 0)
    facts, fact_total = normalize_facts(facts_document)
    return {
        "format": FORMAT, "version": VERSION, "generated": int(generated), "server": server_text,
        "totals": {"dkp": dkp_total, "events": event_total, "facts": fact_total},
        "dkp": dkp, "events": events, "facts": facts,
    }


def lua_string(text):
    out = ['"']
    for byte in text.encode("utf-8"):
        char = chr(byte)
        if char == '"':
            out.append('\\"')
        elif char == "\\":
            out.append("\\\\")
        elif 32 <= byte <= 126:
            out.append(char)
        else:
            out.append(f"\\{byte:03d}")
    out.append('"')
    return "".join(out)


def lua_key(key):
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) and key not in LUA_KEYWORDS:
        return key
    return f"[{lua_string(key)}]"


def lua_value(value, depth=0):
    pad = "    " * (depth + 1)
    close = "    " * depth
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return lua_string(value)
    if isinstance(value, list):
        if not value:
            return "{}"
        return "{\n" + "".join(f"{pad}{lua_value(item, depth + 1)},\n" for item in value) + close + "}"
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{\n" + "".join(
            f"{pad}{lua_key(key)} = {lua_value(item, depth + 1)},\n" for key, item in value.items()
        ) + close + "}"
    raise BridgeError(f"Cannot write {type(value).__name__} as Lua")


def render_lua(bundle):
    header = (
        "-- Generated by tools\\capped_dkp_raidhelper.py. Do not edit by hand.\n"
        "-- Balances, names and events below come from Raid-Helper and are not covered by the addon license.\n"
        "-- This file is loaded as Lua: install only a file you generated yourself.\n"
        "local _, addon = ...\n"
    )
    text = header + "addon.RaidHelperBundle = " + lua_value(bundle) + "\n"
    if len(text.encode("utf-8")) > MAX_OUTPUT_BYTES:
        raise BridgeError("The generated file would exceed the size limit")
    return text


def write_atomic(path, text):
    path = Path(path)
    if path.suffix.lower() != ".lua":
        raise BridgeError("--out must be a .lua file")
    directory = path.resolve().parent
    if not directory.is_dir():
        raise BridgeError(f"Output folder does not exist: {directory}")
    handle, temporary = tempfile.mkstemp(dir=directory, prefix=".raidhelper-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(text.encode("utf-8"))
        os.replace(temporary, path)
    except BaseException:
        if os.path.exists(temporary):
            os.unlink(temporary)
        raise


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: it could carry the Authorization header to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def default_opener():
    return urllib.request.build_opener(NoRedirect)


def check_base_url(base_url):
    parts = urllib.parse.urlsplit(base_url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password:
        raise BridgeError("The Raid-Helper URL must use https without credentials")
    if not any(host == domain or host.endswith("." + domain) for domain in API_DOMAINS):
        raise BridgeError("The API key is only sent to raid-helper.xyz or raid-helper.dev hosts")


def get_json(opener, base_url, path, key, headers=None):
    check_base_url(base_url)
    request = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
    request.add_header("Authorization", key)
    request.add_header("Content-Type", "application/json; charset=utf-8")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise BridgeError(f"Raid-Helper answered HTTP {error.code} for {path}") from None
    except (urllib.error.URLError, OSError) as error:
        raise BridgeError(f"Could not reach Raid-Helper for {path}: {type(error).__name__}") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise BridgeError(f"Response for {path} is larger than {MAX_RESPONSE_BYTES} bytes")
    try:
        return json.loads(body.decode("utf-8"), parse_constant=reject_constant)
    except (UnicodeDecodeError, ValueError):
        raise BridgeError(f"Response for {path} is not valid JSON") from None


def fetch(args, key, opener=None):
    opener = opener or default_opener()
    server = urllib.parse.quote(args.server, safe="")
    entity = urllib.parse.quote(args.entity or args.server, safe="")
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    dkp = get_json(opener, args.base_url, f"/servers/{server}/entities/{entity}/dkp", key)
    (out / "dkp.json").write_text(json.dumps(dkp, indent=2, sort_keys=True), encoding="utf-8")
    pages, current, collected = 1, 1, []
    while current <= min(pages, MAX_PAGES):
        headers = {"Page": str(current), "IncludeSignUps": "true"}
        if args.start:
            headers["StartTimeFilter"] = str(args.start)
        if args.end:
            headers["EndTimeFilter"] = str(args.end)
        page = get_json(opener, args.base_url, f"/servers/{server}/events", key, headers)
        collected.extend(rows_of(page, "postedEvents", "events"))
        pages = page.get("pages", 1) if isinstance(page, dict) and isinstance(page.get("pages"), int) else 1
        current += 1
    (out / "events.json").write_text(json.dumps({"postedEvents": collected}, indent=2, sort_keys=True), encoding="utf-8")
    print(f"Saved dkp.json and events.json ({len(collected)} events) to {out}. Nothing was sent to Raid-Helper.")


def build(args):
    dkp = read_json(args.dkp) if args.dkp else None
    events = read_json(args.events) if args.events else None
    facts = read_json(args.facts) if args.facts else None
    if dkp is None and events is None:
        raise BridgeError("Give --dkp and/or --events")
    generated = args.now if args.now is not None else int(time.time())
    bundle = build_bundle(dkp, events, facts, args.server, generated)
    write_atomic(args.out, render_lua(bundle))
    print(
        f"Wrote {args.out}: {len(bundle['dkp'])} balances, {len(bundle['events'])} events, "
        f"{len(bundle['facts'])} facts. Copy it into the addon folder and /reload. Snapshot only; no live data changed."
    )


def parser():
    root = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = root.add_subparsers(dest="command", required=True)
    fetch_parser = commands.add_parser("fetch", help="GET-only download (needs RAIDHELPER_API_KEY)")
    fetch_parser.add_argument("--server", required=True, help="numeric Discord server ID")
    fetch_parser.add_argument("--entity", help="DKP entity ID; defaults to the server ID")
    fetch_parser.add_argument("--out-dir", required=True)
    fetch_parser.add_argument("--start", type=int, help="earliest event start, unix seconds")
    fetch_parser.add_argument("--end", type=int, help="latest event start, unix seconds")
    fetch_parser.add_argument("--base-url", default=BASE_URL)
    build_parser = commands.add_parser("build", help="offline JSON to RaidHelperData.lua")
    build_parser.add_argument("--dkp")
    build_parser.add_argument("--events")
    build_parser.add_argument("--facts", help="optional curated facts: [{source,time,encounter,text}]")
    build_parser.add_argument("--server", required=True)
    build_parser.add_argument("--out", required=True)
    build_parser.add_argument("--now", type=int, help="snapshot time in unix seconds (default: now)")
    return root


def main(argv=None, opener=None, environ=None):
    environ = os.environ if environ is None else environ
    args = parser().parse_args(argv)
    key = environ.get(KEY_VARIABLE, "")
    try:
        if args.command == "fetch":
            if not key:
                raise BridgeError(f"Set {KEY_VARIABLE} first; the key is never accepted on the command line")
            fetch(args, key, opener)
        else:
            build(args)
    except BridgeError as error:
        message = str(error).replace(key, "***") if key else str(error)
        print(f"ERROR: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
