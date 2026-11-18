# SPDX-License-Identifier: Apache-2.0
"""Original community-data helper for the Peons with Benefits public page.

  fetch  read community.json, then download upcoming Raid-Helper events (sign-up counts by role only)
         and Wowhead names for awarded items. Writes the community data file and updates the item cache.
         With --roster it also keeps the saved public Armory roster up to date: the character list is refreshed
         once it is ROSTER_REFRESH_SECONDS old, each character's spec is read from their public Armory profile
         page, and the role a player declared in a Raid-Helper sign-up is remembered on the roster character
         with the same name.

The only secret, RAIDHELPER_API_KEY, is read from the environment and is never printed or written to a
file. Without it the raid calendar is reported as "unconfigured"; if Raid-Helper fails it is reported as
"error", and the page shows its empty state instead. Raid sign-up names and Discord IDs are never stored:
a sign-up's role and spec are kept only on the roster character whose name matches, for at most
SIGNUP_KEEP_SECONDS after the sign-up was last seen. Requests go only to the hosts in ALLOWED_HOSTS (plus the
Raid-Helper API through capped_dkp_raidhelper and the fixed public recruitment listing through
capped_dkp_recruitment) and never follow a redirect. Recruitment sync is opt-in; its independent
last-good cache survives source failures and the rendered page explicitly labels stale/manual fallback.

The Armory roster comes from the same undocumented query the Armory's own guild page sends, which carries no
specs. A spec is the talent tree with the most points on the character's profile page. That page's data is
undocumented too, so Blizzard can change either without notice; when a refresh fails, the saved roster and specs
are left as they were.

The publisher's offline `render` command loads the files written here with the loaders below.
"""

import argparse
import collections
import http.client
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from pathlib import Path

_TOOLS = str(Path(__file__).resolve().parent)
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import capped_dkp_ledger as store  # noqa: E402
import capped_dkp_raidhelper as raidhelper  # noqa: E402
import capped_dkp_recruitment as recruitment  # noqa: E402
from capped_dkp_ledger import IntegrityError, ToolError  # noqa: E402

CONFIG_FORMAT = "pwb-community-config"
DATA_FORMAT = "pwb-community"
ITEMS_FORMAT = "capped-dkp-item-cache"
GALLERY_FORMAT = "pwb-gallery"
VERSION = 1
CONFIG_BYTES = 64 * 1024
DATA_BYTES = 512 * 1024
ITEMS_BYTES = 4 * 1024 * 1024
GALLERY_BYTES = 64 * 1024
PUBLIC_BYTES = 64 * 1024 * 1024
IMAGE_BYTES = 3 * 1024 * 1024
RESPONSE_BYTES = 2 * 1024 * 1024
GALLERY_LIMIT = 40
RAID_LIMIT = 8
RAID_DAYS = 14
RAID_GRACE_SECONDS = 3 * 3600
ITEM_LIMIT = 20000
ITEM_FETCH_LIMIT = 50
ITEM_DELAY_SECONDS = 0.25
ITEM_RETRY_SECONDS = 7 * 86400
TIMEOUT_SECONDS = 20
USER_AGENT = "PWB-Community/1.0"
ROLES = ("Tank", "Healer", "Melee", "Ranged")
CATEGORIES = ("memory", "event")
STATUSES = ("ok", "error", "unconfigured")
ALLOWED_HOSTS = frozenset({"nether.wowhead.com", "worldofwarcraft.blizzard.com"})
ROSTER_REFRESH_SECONDS = 3 * 3600
ARMORY_REGION = "eu"
ARMORY_GRAPHQL = "https://worldofwarcraft.blizzard.com/en-gb/graphql"
ARMORY_GUILD = "https://worldofwarcraft.blizzard.com/en-gb/forever/eu/armory/guild/peons-with-benefits"
# The Armory guild page's own roster query (a persisted query, so only its hash is sent). The Referer header
# tells the Armory which game the guild belongs to; without it the query fails.
ARMORY_QUERY = {
    "operationName": "GetGuildRoster",
    "variables": {"guild": {"nameSlug": "peons-with-benefits", "region": ARMORY_REGION},
                  "showMessageOfTheDay": False, "showRoles": False},
    "extensions": {"persistedQuery": {
        "version": 1, "sha256Hash": "5f32b85faa248d321a08645d470fb38b82be64b9abd8b5b5fd2816620546794b"}},
}
# The Armory sends rank numbers only. These names were matched against the guild's ranks on 2026-11-15;
# a rank missing here is saved without a name.
ARMORY_RANKS = {0: "Benefits Admin", 1: "Head Peon", 4: "Peon Plus", 5: "Side Hustle", 6: "Fresh Peon"}
PROFILE_SLUG = re.compile(r"(?:[a-z0-9-]|%[0-9A-F]{2})+")
# The spec pass reads at most SPEC_FETCH_LIMIT character pages per run, SPEC_DELAY_SECONDS apart, and each character
# at most once per SPEC_REFRESH_SECONDS. It stops early after SPEC_FAILURE_LIMIT failures in a row. Characters below
# SPEC_MIN_LEVEL have no talents to read. A declared role is forgotten SIGNUP_KEEP_SECONDS after it was last seen.
SPEC_REFRESH_SECONDS = 24 * 3600
SPEC_FETCH_LIMIT = 30
SPEC_DELAY_SECONDS = 0.4
SPEC_FAILURE_LIMIT = 5
SPEC_MIN_LEVEL = 10
SIGNUP_KEEP_SECONDS = 30 * 86400
# The profile page assigns its data to this variable as one JSON object; "specs" lists the talent trees with points.
PROFILE_STATE = re.compile(r"characterProfileInitialState\s*=\s*")
SPEC_LABEL = re.compile(r"[A-Za-z]+(?:[ '-][A-Za-z]+){0,3}")
# Talent trees the Armory names differently from the spec players use.
TREE_NAMES = {"feral combat": "Feral", "elemental combat": "Elemental", "shadow magic": "Shadow"}
# What earlier runs learned about a character, kept when the character list is refreshed.
KEPT_FIELDS = ("spec", "specChecked", "signupRole", "signupSpec", "signupSeen")
SECRET_VARIABLES = ("RAIDHELPER_API_KEY",)
DISCORD_SNOWFLAKE = re.compile(r"[0-9]{17,20}")
DISCORD_MESSAGE = re.compile(r"https://discord\.com/channels/[0-9]{17,20}/[0-9]{17,20}/[0-9]{17,20}")
ITEM_ID = re.compile(r"[1-9][0-9]{0,8}")
ICON = re.compile(r"[a-z0-9_]{1,64}")
IMAGE_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,60}\.(jpg|jpeg|png|webp)")
DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
EXCLUDED_SIGNUPS = frozenset({"absence", "absent", "tentative", "bench", "declined"})
_TAG = re.compile(r"\w{3,32}#[0-9]{3,8}")
_LONG_NUMBER = re.compile(r"[0-9]{17,}")
_MENTION = re.compile(r"<[@#!&:A-Za-z_0-9]*?[0-9]{17,20}>")

PRIORITIES = {"high": "high", "mid": "mid", "medium": "mid", "low": "low"}
APPLY_LIMIT = 3

DEFAULT_RECRUITMENT = {
    "apply": [
        "https://guildsforever.com/guilds/peons-with-benefits/apply",
        "https://guildsofwow.com/peons-with-benefits",
    ],
    "updated": "2026-11-15",
    "openings": [
        {"class": "Druid", "specs": [{"name": "Guardian", "priority": "low"},
                                     {"name": "Restoration", "priority": "low"}]},
        {"class": "Paladin", "specs": [{"name": "Holy", "priority": "low"},
                                       {"name": "Protection", "priority": "low"}]},
        {"class": "Priest", "specs": [{"name": "Discipline", "priority": "low"},
                                      {"name": "Holy", "priority": "low"}]},
        {"class": "Warrior", "specs": [{"name": "Protection", "priority": "low"}]},
    ],
    "expectations": [
        "Consumables ready before the pull.",
        "Know your role and the mechanics.",
        "No exploits, ever.",
    ],
}


class SourceError(Exception):
    """One community source failed; the others still run."""


# What one spec pass did: pages read, pages that failed, characters still waiting, the last failure, and whether the
# failure breaker stopped the pass.
SpecPass = collections.namedtuple("SpecPass", "read failed waiting problem stopped")


def text(value, limit):
    """Printable, single-line, bounded text with Discord mentions, long IDs and BattleTag-like text removed."""
    if not isinstance(value, str):
        return None
    value = _MENTION.sub(" ", value)
    value = "".join(ch if ch.isprintable() else " " for ch in value)
    value = _TAG.sub("[tag]", _LONG_NUMBER.sub("[id]", " ".join(value.split())))
    if len(value) > limit:
        value = value[: limit - 3].rstrip() + "..."
    return value or None


def whole(value, low=0, high=store.MAX_SAFE_INTEGER):
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def shape(value, required, optional, label):
    if not isinstance(value, dict):
        raise IntegrityError(f"{label} must be a JSON object")
    missing = sorted(set(required) - set(value))
    extra = sorted(set(value) - set(required) - set(optional))
    if missing or extra:
        problems = ([f"missing {', '.join(missing)}"] if missing else []) + (
            [f"unknown {', '.join(extra)}"] if extra else [])
        raise IntegrityError(f"{label}: {'; '.join(problems)}")
    return value


def header(document, kind, label):
    if document.get("format") != kind or document.get("version") != VERSION:
        raise IntegrityError(f"{label} is not a {kind} version {VERSION} file")


def required_text(value, limit, label):
    cleaned = text(value, limit)
    if not cleaned:
        raise IntegrityError(f"{label} needs visible text (at most {limit} characters)")
    return cleaned


def listing(value, limit, label):
    if not isinstance(value, list) or len(value) > limit:
        raise IntegrityError(f"{label} must be a list of at most {limit} entries")
    return value


def https_url(value, label):
    if not isinstance(value, str) or len(value) > 200 or any(ch.isspace() or not ch.isprintable() for ch in value):
        raise IntegrityError(f"{label} must be a plain https:// link")
    parts = urllib.parse.urlsplit(value)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise IntegrityError(f"{label} must be a plain https:// link")
    return value


def calendar_day(value, label):
    if not isinstance(value, str) or not DAY.fullmatch(value):
        raise IntegrityError(f"{label} must be a YYYY-MM-DD date")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise IntegrityError(f"{label} is not a real date") from None
    return value


def check_spec(value, label):
    """A spec is plain text, or {"name": ..., "priority": "high" | "mid" | "low"}."""
    if not isinstance(value, dict):
        return {"name": required_text(value, 24, label)}
    shape(value, ("name",), ("priority",), label)
    spec = {"name": required_text(value["name"], 24, f"{label}.name")}
    if "priority" in value:
        level = value["priority"]
        spec["priority"] = PRIORITIES.get(level.strip().lower()) if isinstance(level, str) else None
        if not spec["priority"]:
            raise IntegrityError(f"{label}.priority must be high, mid or low")
    return spec


def apply_links(value, label):
    """Where people apply: `apply` (1 to 3 links, first one leads) or the older single `source` link."""
    if "apply" in value and "source" in value:
        raise IntegrityError(f"{label}: use apply or source, not both")
    if "source" in value:
        return [https_url(value["source"], f"{label}.source")]
    if "apply" not in value:
        return list(DEFAULT_RECRUITMENT["apply"])
    links = [https_url(link, f"{label}.apply") for link in listing(value["apply"], APPLY_LIMIT, f"{label}.apply")]
    if not links or len(set(links)) != len(links):
        raise IntegrityError(f"{label}.apply needs 1 to {APPLY_LIMIT} different links")
    return links


def check_recruitment(value, label="recruitment"):
    shape(value, ("openings", "expectations"), ("apply", "source", "updated", "sync"), label)
    openings = []
    for index, opening in enumerate(listing(value["openings"], 12, f"{label}.openings"), 1):
        where = f"{label}.openings[{index}]"
        shape(opening, ("class", "specs"), (), where)
        specs = [check_spec(spec, f"{where}.specs") for spec in listing(opening["specs"], 6, f"{where}.specs")]
        if not specs:
            raise IntegrityError(f"{where} needs at least one spec")
        openings.append({"class": required_text(opening["class"], 24, f"{where}.class"), "specs": specs})
    expectations = [required_text(line, 160, f"{label}.expectations")
                    for line in listing(value["expectations"], 8, f"{label}.expectations")]
    result = {"apply": apply_links(value, label), "openings": openings, "expectations": expectations}
    if "updated" in value:
        result["updated"] = calendar_day(value["updated"], f"{label}.updated")
    if "sync" in value:
        if value["sync"] != "guildsofwow":
            raise IntegrityError(f"{label}.sync must be guildsofwow, or omitted for manual openings")
        result["sync"] = value["sync"]
    return result


def empty_config():
    return {"raidHelper": None, "recruitment": check_recruitment(DEFAULT_RECRUITMENT),
            "guild": {}, "reports": [], "profiles": {}}


def logs_url(value, path, label):
    value = https_url(value, label)
    parts = urllib.parse.urlsplit(value)
    if parts.netloc != "forever.warcraftlogs.com" or not re.fullmatch(path, parts.path) or parts.query:
        raise IntegrityError(f"{label} must be a public WoW: Forever Warcraft Logs link")
    return value


def guild_settings(document, config, label):
    """Officer-maintained public facts and explicit logs links, never scraped from private evidence."""
    guild = document.get("guild", {})
    shape(guild, (), ("language", "voice", "raidSize", "attendance", "atmosphere", "source", "updated"),
          f"{label} guild")
    for key, value in guild.items():
        where = f"{label} guild.{key}"
        config["guild"][key] = (https_url(value, where) if key == "source" else calendar_day(value, where)
                                if key == "updated" else required_text(value, 160, where))
    runs = set()
    for entry in listing(document.get("reports", []), 100, f"{label} reports"):
        shape(entry, ("run", "url"), (), f"{label} report")
        run = required_text(entry["run"], 80, f"{label} report.run")
        if run in runs:
            raise IntegrityError(f"{label} reports repeats run {run}")
        runs.add(run)
        config["reports"].append({"run": run, "url": logs_url(entry["url"], r"/reports/[A-Za-z0-9]{16}",
                                                            f"{label} report.url")})
    profiles = document.get("profiles", {})
    if not isinstance(profiles, dict) or len(profiles) > 500:
        raise IntegrityError(f"{label} profiles must be an object with at most 500 characters")
    for slug, entry in profiles.items():
        if not isinstance(slug, str) or not PROFILE_SLUG.fullmatch(slug):
            raise IntegrityError(f"{label} profile key must be the character's Armory URL slug")
        shape(entry, ("warcraftLogs",), (), f"{label} profile")
        config["profiles"][slug] = {
            "warcraftLogs": logs_url(entry["warcraftLogs"], r"/character/(?:id/[0-9]+|[A-Za-z0-9%/_-]+)",
                                    f"{label} profile.warcraftLogs")}


def check_config(document, label="community.json"):
    shape(document, ("format", "version"), ("raidHelper", "recruitment", "guild", "reports", "profiles"), label)
    header(document, CONFIG_FORMAT, label)
    config = empty_config()
    raid = document.get("raidHelper")
    if raid is not None:
        shape(raid, ("serverId",), ("days",), f"{label} raidHelper")
        if not isinstance(raid["serverId"], str) or not DISCORD_SNOWFLAKE.fullmatch(raid["serverId"]):
            raise IntegrityError(f"{label} raidHelper.serverId must be the numeric Discord server ID, as text")
        days = raid.get("days", RAID_DAYS)
        if not whole(days, 1, 60):
            raise IntegrityError(f"{label} raidHelper.days must be a whole number from 1 to 60")
        config["raidHelper"] = {"serverId": raid["serverId"], "days": days}
    if "recruitment" in document:
        config["recruitment"] = check_recruitment(document["recruitment"], f"{label} recruitment")
    guild_settings(document, config, label)
    return config


def load_config(path):
    path = Path(path)
    if not path.is_file():
        raise ToolError(f"Cannot find the community settings file {path.name}")
    return check_config(store.read_json(path, CONFIG_BYTES, path.name), path.name)


def check_item_entry(key, entry, label):
    if not isinstance(key, str) or not ITEM_ID.fullmatch(key):
        raise IntegrityError(f"{label} has an item key that is not an item number")
    where = f"{label} item {key}"
    if isinstance(entry, dict) and "missing" in entry:
        shape(entry, ("missing", "checked"), (), where)
        if entry["missing"] is not True or not whole(entry["checked"]):
            raise IntegrityError(f"{where} is malformed")
        return {"missing": True, "checked": entry["checked"]}
    shape(entry, ("name", "checked"), ("quality", "icon"), where)
    result = {"name": required_text(entry["name"], 100, f"{where}.name"), "checked": entry["checked"]}
    if not whole(entry["checked"]):
        raise IntegrityError(f"{where}.checked must be a unix time")
    if "quality" in entry:
        if not whole(entry["quality"], 0, 7):
            raise IntegrityError(f"{where}.quality must be 0 to 7")
        result["quality"] = entry["quality"]
    if "icon" in entry:
        if not isinstance(entry["icon"], str) or not ICON.fullmatch(entry["icon"]):
            raise IntegrityError(f"{where}.icon must be a Wowhead icon name")
        result["icon"] = entry["icon"]
    return result


def check_items(document, label="item-cache.json"):
    shape(document, ("format", "version", "items"), (), label)
    header(document, ITEMS_FORMAT, label)
    items = document["items"]
    if not isinstance(items, dict) or len(items) > ITEM_LIMIT:
        raise IntegrityError(f"{label} items must be an object of at most {ITEM_LIMIT} entries")
    return {key: check_item_entry(key, entry, label) for key, entry in items.items()}


def load_items(path):
    path = Path(path)
    if not path.exists():
        return {}
    return check_items(store.read_json(path, ITEMS_BYTES, path.name), path.name)


def items_document(items):
    return {"format": ITEMS_FORMAT, "version": VERSION, "items": items}


def image_kind(data):
    if data.startswith(b"\xff\xd8\xff"):
        return ("jpg", "jpeg")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ("png",)
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ("webp",)
    return ()


def load_gallery(folder):
    """Validated gallery entries with their image bytes, in the order listed in gallery.json."""
    folder = Path(folder)
    manifest = folder / "gallery.json"
    if not manifest.exists():
        return []
    document = store.read_json(manifest, GALLERY_BYTES, "gallery.json")
    shape(document, ("format", "version", "items"), (), "gallery.json")
    header(document, GALLERY_FORMAT, "gallery.json")
    entries, seen = [], set()
    for index, item in enumerate(listing(document["items"], GALLERY_LIMIT, "gallery.json items"), 1):
        where = f"gallery.json items[{index}]"
        shape(item, ("image", "caption", "category"), ("date", "alt"), where)
        name = item["image"]
        if not isinstance(name, str) or not IMAGE_NAME.fullmatch(name):
            raise IntegrityError(f"{where}.image must be a lower-case file name ending in .jpg, .jpeg, .png or .webp")
        if name in seen:
            raise IntegrityError(f"{where} repeats {name}")
        seen.add(name)
        if item["category"] not in CATEGORIES:
            raise IntegrityError(f"{where}.category must be memory or event")
        path = folder / name
        if path.is_symlink() or not path.is_file():
            raise IntegrityError(f"gallery/{name} is listed but missing")
        with path.open("rb") as stream:
            data = stream.read(IMAGE_BYTES + 1)
        if len(data) > IMAGE_BYTES:
            raise IntegrityError(f"gallery/{name} is larger than {IMAGE_BYTES // (1024 * 1024)} MB")
        if name.rsplit(".", 1)[1] not in image_kind(data):
            raise IntegrityError(f"gallery/{name} is not the image type its name says")
        caption = required_text(item["caption"], 120, f"{where}.caption")
        entry = {"image": name, "caption": caption, "category": item["category"],
                 "alt": required_text(item["alt"], 160, f"{where}.alt") if "alt" in item else caption, "data": data}
        if "date" in item:
            entry["date"] = calendar_day(item["date"], f"{where}.date")
        entries.append(entry)
    return entries


def check_raid_event(event, where):
    shape(event, ("id", "title", "start", "total", "roles"), ("url",), where)
    if raidhelper.event_id(event["id"]) != event["id"] or not whole(event["start"], 1) or not whole(event["total"], 0, 10000):
        raise IntegrityError(f"{where} is malformed")
    shape(event["roles"], ROLES, (), f"{where}.roles")
    if not all(whole(event["roles"][role], 0, 10000) for role in ROLES):
        raise IntegrityError(f"{where}.roles must be counts")
    result = {"id": event["id"], "title": required_text(event["title"], 80, f"{where}.title"), "start": event["start"],
              "total": event["total"], "roles": {role: event["roles"][role] for role in ROLES}}
    if "url" in event:
        if not isinstance(event["url"], str) or not DISCORD_MESSAGE.fullmatch(event["url"]):
            raise IntegrityError(f"{where}.url must be a Discord message link")
        result["url"] = event["url"]
    return result


def check_data(document, label="community data"):
    shape(document, ("format", "version", "generated", "raidHelper"), ("recruitment",), label)
    header(document, DATA_FORMAT, label)
    if not whole(document["generated"]):
        raise IntegrityError(f"{label} generated must be a unix time")
    raid = shape(document["raidHelper"], ("status", "checked", "events"), (), f"{label} raidHelper")
    if raid["status"] not in STATUSES:
        raise IntegrityError(f"{label} raidHelper.status must be ok, error or unconfigured")
    if raid["checked"] is not None and not whole(raid["checked"]):
        raise IntegrityError(f"{label} raidHelper.checked must be a unix time or null")
    events = [check_raid_event(event, f"{label} raid {index}")
              for index, event in enumerate(listing(raid["events"], RAID_LIMIT, f"{label} raid events"), 1)]
    result = {
        "generated": document["generated"],
        "raidHelper": {"status": raid["status"], "checked": raid["checked"], "events": events},
    }
    if "recruitment" in document:
        result["recruitment"] = recruitment.check_feed(document["recruitment"])
    return result


def resolved_recruitment(config, feed, now=None):
    """Keep officer-owned apply links/expectations; substitute only verified source openings."""
    result = dict(config["recruitment"])
    if result.get("sync") != "guildsofwow":
        return result
    snapshot = (feed or {}).get("recruitment") or recruitment.unavailable(
        int(time.time()) if now is None else now)
    result["snapshot"] = snapshot
    if snapshot["status"] in ("ok", "stale"):
        result["openings"] = snapshot["openings"]
        result.pop("updated", None)
    return result


def load_data(path):
    path = Path(path)
    if not path.exists():
        return None
    return check_data(store.read_json(path, DATA_BYTES, path.name), path.name)


def request(opener, url, label, *, missing_ok=False, payload=None, headers=()):
    """GET one allowlisted https URL, or POST a JSON payload to it; return the bounded body, or None on an
    allowed 404."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or parts.username or parts.password:
        raise SourceError(f"{label}: refused a host outside the allowlist")
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    outgoing = urllib.request.Request(url, data=data, method="GET" if data is None else "POST")
    outgoing.add_header("User-Agent", USER_AGENT)
    if data is not None:
        outgoing.add_header("Content-Type", "application/json")
    for name, value in headers:
        outgoing.add_header(name, value)
    try:
        with opener.open(outgoing, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        raise SourceError(f"{label} answered HTTP {error.code}") from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as error:
        raise SourceError(f"Could not reach {label}: {type(error).__name__}") from None
    if len(body) > RESPONSE_BYTES:
        raise SourceError(f"{label} sent more than {RESPONSE_BYTES} bytes")
    return body


def decode(body, label):
    try:
        value = json.loads(body.decode("utf-8"), parse_constant=raidhelper.reject_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise SourceError(f"{label} did not send valid JSON") from None
    if not isinstance(value, dict):
        raise SourceError(f"{label} sent an unexpected answer")
    return value


def signup_role(signup):
    for key in ("roleName", "className"):
        word = signup.get(key)
        word = word.strip().lower() if isinstance(word, str) else ""
        for prefix, role in (("tank", "Tank"), ("heal", "Healer"), ("melee", "Melee"), ("range", "Ranged")):
            if word.startswith(prefix):
                return role
    return None


def counted(signup):
    """False for a sign-up that is an absence, tentative, benched or declined."""
    labels = {signup.get(key).strip().lower() for key in ("className", "roleName", "status")
              if isinstance(signup.get(key), str)}
    return not labels & EXCLUDED_SIGNUPS


def signups_of(entry):
    signups = entry.get("signUps", entry.get("signups"))
    return [signup for signup in signups if isinstance(signup, dict)] if isinstance(signups, list) else []


def upcoming(rows, start, end):
    """(start, ID, event) for each event that begins inside the window, soonest first."""
    found = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        identifier = raidhelper.event_id(entry.get("id", entry.get("eventId")))
        begins = raidhelper.unix_seconds(entry.get("startTime", entry.get("startTimeUnix", entry.get("start"))))
        if identifier is not None and begins is not None and start <= begins <= end:
            found.append((begins, identifier, entry))
    found.sort(key=lambda item: (item[0], item[1]))
    return found


def raid_summaries(rows, server, start, end):
    """Upcoming events with sign-up counts by role. Names and Discord IDs are read but never kept."""
    events = []
    for begins, identifier, entry in upcoming(rows, start, end):
        roles, total = dict.fromkeys(ROLES, 0), 0
        for signup in signups_of(entry):
            if not counted(signup):
                continue
            total += 1
            role = signup_role(signup)
            if role:
                roles[role] += 1
        row = {"id": identifier, "title": text(entry.get("title"), 80) or "Raid", "start": begins,
               "total": total, "roles": roles}
        channel = entry.get("channelId", entry.get("channelID"))
        channel = str(channel) if isinstance(channel, (str, int)) and not isinstance(channel, bool) else ""
        if DISCORD_SNOWFLAKE.fullmatch(channel) and DISCORD_SNOWFLAKE.fullmatch(identifier):
            row["url"] = f"https://discord.com/channels/{server}/{channel}/{identifier}"
        events.append(row)
    return events[:RAID_LIMIT]


def spec_label(name):
    """A talent tree or sign-up spec as the label saved and shown, or None when it is not a plain spec name."""
    label = " ".join(name.split())
    label = TREE_NAMES.get(label.casefold(), label)
    return label if len(label) <= 40 and SPEC_LABEL.fullmatch(label) else None


def raid_declarations(rows, start, end):
    """The role, spec and class each counted sign-up declared, soonest event first. They stay in memory:
    apply_signups keeps only the role and spec of the roster character whose name matches."""
    import capped_dkp_publish as publish
    found = []
    for _, _, entry in upcoming(rows, start, end):
        for signup in signups_of(entry):
            name, role = text(signup.get("name"), 60), signup_role(signup)
            if not name or not role or not counted(signup):
                continue
            klass, spec = signup.get("className"), signup.get("specName")
            klass = klass.strip().title() if isinstance(klass, str) else None
            # Raid-Helper numbers a spec that repeats within a template: "Holy1".
            spec = spec_label(spec.rstrip("0123456789")) if isinstance(spec, str) else None
            found.append({"name": name, "role": role, "spec": spec, "class": klass if klass in publish.CLASSES else None})
    return found


def fetch_raids(opener, config, key, now, declared=None):
    """Upcoming events as counts by role. When a list is given, the roles players declared in their sign-ups are
    added to it, in memory only, once every page was read."""
    settings = config["raidHelper"]
    if not settings or not key:
        return {"status": "unconfigured", "checked": None, "events": []}
    server = settings["serverId"]
    start, end = now - RAID_GRACE_SECONDS, now + settings["days"] * 86400
    collected, pages, current = [], 1, 1
    while current <= min(pages, raidhelper.MAX_PAGES):
        page = raidhelper.get_json(opener, raidhelper.BASE_URL, f"/servers/{server}/events", key, {
            "Page": str(current), "IncludeSignUps": "true",
            "StartTimeFilter": str(start), "EndTimeFilter": str(end)})
        collected.extend(raidhelper.rows_of(page, "postedEvents", "events"))
        pages = page.get("pages", 1) if isinstance(page, dict) and whole(page.get("pages"), 1) else 1
        current += 1
    events = raid_summaries(collected, server, start, end)
    if declared is not None:
        declared.extend(raid_declarations(collected, start, end))
    return {"status": "ok", "checked": now, "events": events}


def fetch_items(opener, cache, wanted, now, sleep):
    """Add Wowhead names for new item IDs (and recheck missing ones weekly). Returns (fetched, waiting)."""
    due = [item for item in sorted(wanted) if str(item) not in cache
           or cache[str(item)].get("missing") and now - cache[str(item)]["checked"] >= ITEM_RETRY_SECONDS]
    fetched = 0
    for index, item in enumerate(due[:ITEM_FETCH_LIMIT]):
        if index:
            sleep(ITEM_DELAY_SECONDS)
        body = request(opener, f"https://nether.wowhead.com/forever/tooltip/item/{item}", "Wowhead", missing_ok=True)
        if body is None:
            cache[str(item)] = {"missing": True, "checked": now}
        else:
            answer = decode(body, "Wowhead")
            name = text(answer.get("name"), 100)
            if not name:
                raise SourceError(f"Wowhead sent item {item} without a name")
            entry = {"name": name, "checked": now}
            if whole(answer.get("quality"), 0, 7):
                entry["quality"] = answer["quality"]
            icon = answer.get("icon")
            if isinstance(icon, str) and ICON.fullmatch(icon.lower()):
                entry["icon"] = icon.lower()
            cache[str(item)] = entry
        fetched += 1
    return fetched, len(due) - fetched


def armory_members(rows):
    """Armory roster rows as public snapshot members: name, profile link, and level, class and rank when known.
    Rows without a usable character ID, and repeats, are skipped."""
    import capped_dkp_publish as publish
    members, seen = [], set()
    for row in rows if isinstance(rows, list) else []:
        character = row.get("character") if isinstance(row, dict) else None
        identifier = character.get("_id") if isinstance(character, dict) else None
        if not isinstance(identifier, str) or not identifier.startswith(ARMORY_REGION + "-"):
            continue
        slug = urllib.parse.quote(identifier[len(ARMORY_REGION) + 1:].lower(), safe="-")
        name = " ".join(part for part in (text(character.get("name"), 50), text(character.get("surname"), 50))
                        if part)
        if not name or not PROFILE_SLUG.fullmatch(slug) or slug in seen:
            continue
        seen.add(slug)
        member = {"name": name, "url": publish.PROFILE_PREFIX + slug}
        if whole(character.get("level"), 1, publish.LEVEL_MAX):
            member["level"] = character["level"]
        klass = character.get("class")
        if isinstance(klass, dict) and klass.get("name") in publish.CLASSES:
            member["class"] = klass["name"]
        rank = row.get("rank")
        rank = rank.get("rankId") if isinstance(rank, dict) else None
        if whole(rank) and rank in ARMORY_RANKS:
            member["rank"] = ARMORY_RANKS[rank]
        members.append(member)
    members.sort(key=lambda member: (member["name"].casefold(), member["url"]))
    return members


def fetch_roster(opener, now):
    """The guild roster from the public Armory, as a document the publisher's roster check accepts."""
    import capped_dkp_publish as publish
    body = request(opener, ARMORY_GRAPHQL, "The Armory", payload=ARMORY_QUERY,
                   headers=(("Accept", "application/json"), ("Referer", ARMORY_GUILD)))
    answer = decode(body, "The Armory")
    errors, data = answer.get("errors"), answer.get("data")
    guild = data.get("Guild") if isinstance(data, dict) else None
    if errors or not isinstance(guild, dict):
        first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
        raise SourceError("The Armory did not send the roster: " + (text(first.get("message"), 80) or "no guild"))
    members = armory_members(guild.get("roster"))
    if not members:
        raise SourceError("The Armory sent no characters")
    document = {"source": ARMORY_GUILD, "retrievedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                "members": members}
    try:
        _, _, hidden = publish.armory_roster(document)
    except ToolError as error:
        raise SourceError(str(error)) from None
    return document, hidden


def saved_roster(path):
    """The saved roster as a document the publisher accepts, or None when it is missing, unreadable or invalid."""
    import capped_dkp_publish as publish
    path = Path(path)
    if not path.is_file():
        return None
    try:
        document = store.read_json(path, publish.ROSTER_BYTES, path.name)
        publish.armory_roster(document)
    except ToolError:
        return None
    return document


def roster_age(document, now):
    """Seconds since the roster's character list was refreshed; negative when its clock is ahead of ours."""
    return now - datetime.fromisoformat(document["retrievedAt"].replace("Z", "+00:00")).timestamp()


def parse_profile_spec(body, klass):
    """The spec on a character's profile page: the talent tree with the most points spent, or None when no tree leads
    (no talents yet, or a tie). Raises SourceError when the page is not in the expected shape, so that a change to the
    page is reported instead of silently erasing the saved specs."""
    try:
        page = body.decode("utf-8")
        marker = PROFILE_STATE.search(page)
        if not marker:
            raise ValueError("no character data")
        state, _ = json.JSONDecoder(parse_constant=raidhelper.reject_constant).raw_decode(page, marker.end())
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise SourceError("The Armory profile page has no readable character data") from None
    character = state.get("character") if isinstance(state, dict) else None
    if not isinstance(character, dict):
        raise SourceError("The Armory profile page has no character")
    kind = character.get("class")
    if not isinstance(kind, dict) or kind.get("name") != klass:
        raise SourceError("The Armory profile page is not for a " + klass)
    trees = character.get("specs")
    if not isinstance(trees, list):
        raise SourceError("The Armory profile page lists no talent trees")
    shaped = [(tree["spentPoints"], tree["name"], tree.get("active") is not False) for tree in trees
              if isinstance(tree, dict) and isinstance(tree.get("name"), str) and whole(tree.get("spentPoints"), 0, 1000)]
    if trees and not shaped:
        raise SourceError("The Armory profile page's talent trees are not in the expected form")
    ranked = sorted(((points, name) for points, name, active in shaped if active), reverse=True)
    if not ranked or ranked[0][0] == 0 or len(ranked) > 1 and ranked[1][0] == ranked[0][0]:
        return None
    label = spec_label(ranked[0][1])
    if label is None:
        raise SourceError("The Armory profile page named a talent tree in an unexpected form")
    return label


def profile_spec(opener, member):
    return parse_profile_spec(request(opener, member["url"], "The Armory profile"), member["class"])


def spec_due(member, now):
    """True when the character's page should be read now: its class is known, it is at least SPEC_MIN_LEVEL (or its
    level is unknown), and it was never read or last read SPEC_REFRESH_SECONDS ago. Hidden ranks are never read."""
    import capped_dkp_publish as publish
    if "class" not in member or member.get("rank") in publish.HIDDEN_RANKS:
        return False
    if member.get("level", SPEC_MIN_LEVEL) < SPEC_MIN_LEVEL:
        return False
    checked = member.get("specChecked")
    return checked is None or not 0 <= now - checked < SPEC_REFRESH_SECONDS


def refresh_specs(opener, members, now, sleep):
    """Read the profile page of each character that is due, never-read ones first and then the longest ago, at most
    SPEC_FETCH_LIMIT per run. Updates the members in place. A character is marked as read whether or not its page
    could be, so one that keeps failing cannot starve the rest; it keeps its earlier spec and is tried again after
    SPEC_REFRESH_SECONDS. The pass stops early after SPEC_FAILURE_LIMIT failures in a row."""
    due = sorted((member for member in members if spec_due(member, now)),
                 key=lambda member: (member.get("specChecked", 0), member["name"].casefold(), member["url"]))
    read = failed = streak = 0
    problem, stopped = None, False
    for member in due[:SPEC_FETCH_LIMIT]:
        if read + failed:
            sleep(SPEC_DELAY_SECONDS)
        member["specChecked"] = now
        try:
            spec = profile_spec(opener, member)
        except SourceError as error:
            failed, streak, problem = failed + 1, streak + 1, error
            if streak >= SPEC_FAILURE_LIMIT:
                stopped = True
                break
            continue
        read, streak = read + 1, 0
        if spec:
            member["spec"] = spec
        else:
            member.pop("spec", None)
    return SpecPass(read, failed, len(due) - read - failed, problem, stopped)


def carry_over(fresh, saved):
    """Copy what earlier runs learned about each character (its spec, when it was read, and any declared role) from the
    saved roster onto the freshly fetched members. A character whose class changed starts over."""
    earlier = {member["url"]: member for member in saved["members"]}
    for member in fresh:
        old = earlier.get(member["url"])
        if old is not None and old.get("class") == member.get("class"):
            member.update({field: old[field] for field in KEPT_FIELDS if field in old})


def fold(name):
    """A character name for comparing: accents, case and spacing ignored."""
    plain = "".join(ch for ch in unicodedata.normalize("NFKD", name) if not unicodedata.combining(ch))
    return " ".join(plain.casefold().split())


def apply_signups(members, declared, now):
    """Remember, on each roster character whose full name matches a sign-up, the role and spec its player declared;
    the soonest event decides. A name shared by several characters, a class that disagrees with the roster's, and
    hidden ranks are skipped. What is not declared again is forgotten after SIGNUP_KEEP_SECONDS. Sign-up names are
    never stored. Returns how many characters matched."""
    import capped_dkp_publish as publish
    names = {}
    for member in members:
        if member.get("rank") not in publish.HIDDEN_RANKS:
            names.setdefault(fold(member["name"]), []).append(member)
    seen, matched = now - now % 86400, set()
    for item in declared:
        found = names.get(fold(item["name"]), [])
        if len(found) != 1 or found[0]["url"] in matched:
            continue
        member = found[0]
        if item["class"] and member.get("class") and item["class"] != member["class"]:
            continue
        matched.add(member["url"])
        member.pop("signupSpec", None)
        member["signupRole"], member["signupSeen"] = item["role"], seen
        if item["spec"]:
            member["signupSpec"] = item["spec"]
    for member in members:
        age = now - member["signupSeen"] if "signupSeen" in member else None
        if member["url"] not in matched and (age is None or not 0 <= age <= SIGNUP_KEEP_SECONDS):
            for field in ("signupRole", "signupSpec", "signupSeen"):
                member.pop(field, None)
    return len(matched)


def update_roster(path, opener, now, sleep, declared, say, secrets):
    """Keep the saved roster current: refresh the character list when it is old, read the specs that are due and
    remember the declared sign-up roles. The file is written only when something changed."""
    path = Path(path)
    roster = saved = saved_roster(path)
    before = json.dumps(saved, sort_keys=True)
    hours = ROSTER_REFRESH_SECONDS // 3600
    if saved is not None and 0 <= roster_age(saved, now) < ROSTER_REFRESH_SECONDS:
        say(f"Armory roster: the saved copy is less than {hours} hours old, so the character list was not checked.")
    else:
        try:
            fresh, hidden = fetch_roster(opener, now)
        except SourceError as error:
            say("Armory roster: could not update; the saved roster was left as it was. " + redact(str(error), secrets))
        else:
            if saved is not None:
                carry_over(fresh["members"], saved)
            roster = fresh
            say(f"Armory roster: {len(fresh['members'])} characters ({hidden} at hidden ranks).")
    if roster is None:
        return
    members = roster["members"]
    outcome = refresh_specs(opener, members, now, sleep)
    if outcome.read or outcome.failed:
        line = (f"Armory specs: {outcome.read} read, {outcome.failed} failed, {outcome.waiting} left for a later run; "
                f"characters with a spec: {sum('spec' in member for member in members)}.")
        if outcome.failed:
            line += f" Last problem: {redact(str(outcome.problem), secrets)}."
        if outcome.stopped:
            line += f" Stopped after {SPEC_FAILURE_LIMIT} failures in a row; the profile pages may have changed."
        say(line)
    else:
        say("Armory specs: nothing to read; every character was read within the last "
            f"{SPEC_REFRESH_SECONDS // 3600} hours or has no talents to read.")
    matched = apply_signups(members, declared, now)
    if declared:
        say(f"Raid sign-up roles: roster characters matched by name: {matched}."
            + ("" if matched else " A sign-up's name has to equal the character's name."))
    if json.dumps(roster, sort_keys=True) != before:
        store.write_atomic(path, store.indented(roster))
        say(f"Updated {path.name}.")


def wanted_items(folder):
    """Item IDs named in published history and standings; malformed entries are skipped (render rejects them)."""
    folder, wanted = Path(folder), set()
    history, standings = folder / "history.json", folder / "standings.json"
    if history.exists():
        document = store.read_json(history, PUBLIC_BYTES, "history.json")
        for event in document.get("events", []) if isinstance(document, dict) else []:
            fields = event.get("fields") if isinstance(event, dict) else None
            if isinstance(fields, dict) and whole(fields.get("item"), 1):
                wanted.add(fields["item"])
    if standings.exists():
        document = store.read_json(standings, PUBLIC_BYTES, "standings.json")
        for player in document.get("players", []) if isinstance(document, dict) else []:
            for loot in player.get("loot", []) if isinstance(player, dict) else []:
                if isinstance(loot, dict) and whole(loot.get("item"), 1):
                    wanted.add(loot["item"])
    return wanted


def redact(message, secrets):
    for secret in sorted(secrets, key=len, reverse=True):
        if secret and len(secret) >= 6:
            message = message.replace(secret, "***")
    return message


def run_fetch(args, opener, environ, now, sleep, say):
    config = load_config(args.config)
    cache = load_items(args.items) if args.items else {}
    before = json.dumps(cache, sort_keys=True)
    wanted = wanted_items(args.data) if args.data else set()
    opener = opener or raidhelper.default_opener()
    secrets = [environ.get(name, "") for name in SECRET_VARIABLES]
    key = environ.get("RAIDHELPER_API_KEY", "")
    declared = []

    try:
        raid = fetch_raids(opener, config, key, now, declared)
        if raid["status"] == "ok":
            say(f"Raid sign-ups: {len(raid['events'])} upcoming events saved as counts by role (no names).")
        else:
            say("Raid sign-ups: not configured (needs raidHelper.serverId and the RAIDHELPER_API_KEY secret).")
    except (raidhelper.BridgeError, SourceError) as error:
        raid = {"status": "error", "checked": now, "events": []}
        say("Raid sign-ups: could not update. " + redact(str(error), secrets))

    try:
        fetched, waiting = fetch_items(opener, cache, wanted, now, sleep)
        say(f"Wowhead items: {fetched} looked up" + (f", {waiting} left for the next run." if waiting else "."))
    except SourceError as error:
        say("Wowhead items: stopped early. " + redact(str(error), secrets))

    if args.roster:
        update_roster(args.roster, opener, now, sleep, declared, say, secrets)

    document = {"format": DATA_FORMAT, "version": VERSION, "generated": now, "raidHelper": raid}
    if config["recruitment"].get("sync") == "guildsofwow":
        document["recruitment"] = recruitment.refresh(args.recruitment, opener, now, say)
    check_data(document)
    store.write_atomic(Path(args.out), store.indented(document))
    if args.items and json.dumps(cache, sort_keys=True) != before:
        store.write_atomic(Path(args.items), store.indented(items_document(cache)))
        say(f"Updated {Path(args.items).name}.")
    say(f"Wrote {Path(args.out).name}. Nothing was sent anywhere except the read requests above.")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ToolError(message)


def build_parser():
    root = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = root.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="download raid sign-up counts, item names and the Armory roster")
    fetch.add_argument("--config", required=True, metavar="FILE", help="community.json")
    fetch.add_argument("--data", metavar="DIR", help="folder with the published standings.json and history.json")
    fetch.add_argument("--items", metavar="FILE", help="item-cache.json to read and update")
    fetch.add_argument("--roster", metavar="FILE",
                       help="armory-roster.json to keep up to date: the character list once it is 3 hours old, "
                            "each character's spec from its Armory profile page, and declared sign-up roles")
    fetch.add_argument("--recruitment", metavar="FILE",
                       help="last-good public openings cache; enabled by recruitment.sync= guildsofwow")
    fetch.add_argument("--out", required=True, metavar="FILE", help="community data file to write")
    return root


def main(argv=None, opener=None, environ=None, now=None, sleep=time.sleep):
    environ = os.environ if environ is None else environ
    argv = sys.argv[1:] if argv is None else list(argv)
    secrets = [environ.get(name, "") for name in SECRET_VARIABLES]

    def say(message, stream=None):
        stream = stream or sys.stdout
        encoding = getattr(stream, "encoding", None) or "ascii"
        line = redact(message, secrets)
        print(line.encode(encoding, "backslashreplace").decode(encoding), file=stream)

    try:
        args = build_parser().parse_args(argv)
        run_fetch(args, opener, environ, int(time.time()) if now is None else now, sleep, say)
    except ToolError as error:
        say(f"ERROR: {error}", sys.stderr)
        return error.code
    except OSError as error:
        say(f"ERROR: File operation failed: {type(error).__name__}: {error.strerror or error}", sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
