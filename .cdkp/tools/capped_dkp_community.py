# SPDX-License-Identifier: Apache-2.0
"""Original community-data helper for the Peons with Benefits public page.

  fetch  read community.json, then download upcoming Raid-Helper events (sign-up counts by role only),
         which listed guild streamers are live on Twitch or YouTube, and Wowhead names for awarded items.
         Writes the community data file and updates the item cache.

Secrets are read only from the environment and are never printed or written to a file:
RAIDHELPER_API_KEY, TWITCH_CLIENT_ID, TWITCH_CLIENT_SECRET and YOUTUBE_API_KEY. A source without its
secret is reported as "unconfigured"; a source that fails is reported as "error", and the page shows
its empty state instead. Raid sign-up names are never stored. Requests go only to the hosts in
ALLOWED_HOSTS (plus the Raid-Helper API through capped_dkp_raidhelper) and never follow a redirect.

The publisher's offline `render` command loads the files written here with the loaders below.
"""

import argparse
import copy
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

_TOOLS = str(Path(__file__).resolve().parent)
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import capped_dkp_ledger as store  # noqa: E402
import capped_dkp_raidhelper as raidhelper  # noqa: E402
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
STREAMER_LIMIT = 20
RAID_LIMIT = 8
RAID_DAYS = 14
RAID_GRACE_SECONDS = 3 * 3600
ITEM_LIMIT = 20000
ITEM_FETCH_LIMIT = 50
ITEM_DELAY_SECONDS = 0.25
ITEM_RETRY_SECONDS = 7 * 86400
YOUTUBE_RECENT = 5
TIMEOUT_SECONDS = 20
USER_AGENT = "PWB-Community/1.0"
ROLES = ("Tank", "Healer", "Melee", "Ranged")
CATEGORIES = ("memory", "event")
STATUSES = ("ok", "error", "unconfigured")
ALLOWED_HOSTS = frozenset({
    "id.twitch.tv", "api.twitch.tv", "www.youtube.com", "www.googleapis.com", "nether.wowhead.com",
})
SECRET_VARIABLES = ("RAIDHELPER_API_KEY", "TWITCH_CLIENT_ID", "TWITCH_CLIENT_SECRET", "YOUTUBE_API_KEY")
TWITCH_LOGIN = re.compile(r"[A-Za-z0-9_]{3,25}")
YOUTUBE_CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}")
YOUTUBE_VIDEO = re.compile(r"[A-Za-z0-9_-]{11}")
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

DEFAULT_RECRUITMENT = {
    "source": "https://guildsofwow.com/peons-with-benefits/recruitment",
    "updated": "2026-11-15",
    "openings": [
        {"class": "Druid", "specs": ["Guardian", "Restoration"]},
        {"class": "Paladin", "specs": ["Holy", "Protection"]},
        {"class": "Priest", "specs": ["Discipline", "Holy"]},
        {"class": "Warrior", "specs": ["Protection"]},
    ],
    "expectations": [
        "Consumables ready before the pull.",
        "Know your role and the mechanics.",
        "No exploits, ever.",
    ],
}


class SourceError(Exception):
    """One community source failed; the others still run."""


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


def check_recruitment(value, label="recruitment"):
    shape(value, ("openings", "expectations"), ("source", "updated"), label)
    openings = []
    for index, opening in enumerate(listing(value["openings"], 12, f"{label}.openings"), 1):
        where = f"{label}.openings[{index}]"
        shape(opening, ("class", "specs"), (), where)
        specs = [required_text(spec, 24, f"{where}.specs") for spec in listing(opening["specs"], 6, f"{where}.specs")]
        if not specs:
            raise IntegrityError(f"{where} needs at least one spec")
        openings.append({"class": required_text(opening["class"], 24, f"{where}.class"), "specs": specs})
    expectations = [required_text(line, 160, f"{label}.expectations")
                    for line in listing(value["expectations"], 8, f"{label}.expectations")]
    result = {"openings": openings, "expectations": expectations}
    if "source" in value:
        result["source"] = https_url(value["source"], f"{label}.source")
    if "updated" in value:
        result["updated"] = calendar_day(value["updated"], f"{label}.updated")
    return result


def empty_config():
    return {"raidHelper": None, "streamers": [], "recruitment": copy.deepcopy(DEFAULT_RECRUITMENT)}


def check_config(document, label="community.json"):
    shape(document, ("format", "version"), ("raidHelper", "streamers", "recruitment"), label)
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
    seen = set()
    for index, streamer in enumerate(listing(document.get("streamers", []), STREAMER_LIMIT, f"{label} streamers"), 1):
        where = f"{label} streamers[{index}]"
        shape(streamer, ("name",), ("twitch", "youtube"), where)
        entry = {"name": required_text(streamer["name"], 40, f"{where}.name")}
        if "twitch" in streamer:
            if not isinstance(streamer["twitch"], str) or not TWITCH_LOGIN.fullmatch(streamer["twitch"]):
                raise IntegrityError(f"{where}.twitch must be a Twitch login name")
            entry["twitch"] = streamer["twitch"].lower()
        if "youtube" in streamer:
            if not isinstance(streamer["youtube"], str) or not YOUTUBE_CHANNEL.fullmatch(streamer["youtube"]):
                raise IntegrityError(f"{where}.youtube must be a YouTube channel ID (UC followed by 22 characters)")
            entry["youtube"] = streamer["youtube"]
        if len(entry) == 1:
            raise IntegrityError(f"{where} needs a twitch or youtube channel")
        for key in ("name", "twitch", "youtube"):
            if key in entry:
                marker = (key, entry[key].lower())
                if marker in seen:
                    raise IntegrityError(f"{where} repeats {key} {entry[key]}")
                seen.add(marker)
        config["streamers"].append(entry)
    if "recruitment" in document:
        config["recruitment"] = check_recruitment(document["recruitment"], f"{label} recruitment")
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


def check_stream(stream, where):
    shape(stream, ("name", "platform", "channel", "title"), ("video",), where)
    platform, channel = stream["platform"], stream["channel"]
    pattern = {"twitch": TWITCH_LOGIN, "youtube": YOUTUBE_CHANNEL}.get(platform)
    if pattern is None or not isinstance(channel, str) or not pattern.fullmatch(channel):
        raise IntegrityError(f"{where} has an unknown platform or channel")
    result = {"name": required_text(stream["name"], 40, f"{where}.name"), "platform": platform, "channel": channel,
              "title": required_text(stream["title"], 140, f"{where}.title")}
    if "video" in stream:
        if platform != "youtube" or not isinstance(stream["video"], str) or not YOUTUBE_VIDEO.fullmatch(stream["video"]):
            raise IntegrityError(f"{where}.video must be a YouTube video ID")
        result["video"] = stream["video"]
    return result


def check_data(document, label="community data"):
    shape(document, ("format", "version", "generated", "raidHelper", "streams"), (), label)
    header(document, DATA_FORMAT, label)
    if not whole(document["generated"]):
        raise IntegrityError(f"{label} generated must be a unix time")
    raid = shape(document["raidHelper"], ("status", "checked", "events"), (), f"{label} raidHelper")
    streams = shape(document["streams"], ("checked", "twitch", "youtube", "live"), (), f"{label} streams")
    for value, where in ((raid["status"], "raidHelper.status"), (streams["twitch"], "streams.twitch"),
                         (streams["youtube"], "streams.youtube")):
        if value not in STATUSES:
            raise IntegrityError(f"{label} {where} must be ok, error or unconfigured")
    for value, where in ((raid["checked"], "raidHelper.checked"), (streams["checked"], "streams.checked")):
        if value is not None and not whole(value):
            raise IntegrityError(f"{label} {where} must be a unix time or null")
    events = [check_raid_event(event, f"{label} raid {index}")
              for index, event in enumerate(listing(raid["events"], RAID_LIMIT, f"{label} raid events"), 1)]
    live = [check_stream(stream, f"{label} stream {index}")
            for index, stream in enumerate(listing(streams["live"], 2 * STREAMER_LIMIT, f"{label} live"), 1)]
    return {
        "generated": document["generated"],
        "raidHelper": {"status": raid["status"], "checked": raid["checked"], "events": events},
        "streams": {"checked": streams["checked"], "twitch": streams["twitch"], "youtube": streams["youtube"],
                    "live": live},
    }


def load_data(path):
    path = Path(path)
    if not path.exists():
        return None
    return check_data(store.read_json(path, DATA_BYTES, path.name), path.name)


def request(opener, url, label, *, data=None, headers=None, missing_ok=False):
    """GET (or POST when data is given) one allowlisted https URL; return the bounded body, or None on an allowed 404."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or parts.username or parts.password:
        raise SourceError(f"{label}: refused a host outside the allowlist")
    outgoing = urllib.request.Request(url, data=data, method="GET" if data is None else "POST")
    outgoing.add_header("User-Agent", USER_AGENT)
    for name, value in (headers or {}).items():
        outgoing.add_header(name, value)
    try:
        with opener.open(outgoing, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        if missing_ok and error.code == 404:
            return None
        raise SourceError(f"{label} answered HTTP {error.code}") from None
    except (urllib.error.URLError, OSError) as error:
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


def raid_summaries(rows, server, start, end):
    """Upcoming events with sign-up counts by role. Names and Discord IDs are read but never kept."""
    events = []
    for entry in rows:
        if not isinstance(entry, dict):
            continue
        identifier = raidhelper.event_id(entry.get("id", entry.get("eventId")))
        begins = raidhelper.unix_seconds(entry.get("startTime", entry.get("startTimeUnix", entry.get("start"))))
        if identifier is None or begins is None or not start <= begins <= end:
            continue
        roles, total = dict.fromkeys(ROLES, 0), 0
        signups = entry.get("signUps", entry.get("signups"))
        for signup in signups if isinstance(signups, list) else []:
            if not isinstance(signup, dict):
                continue
            labels = {signup.get(key).strip().lower() for key in ("className", "roleName", "status")
                      if isinstance(signup.get(key), str)}
            if labels & EXCLUDED_SIGNUPS:
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
    events.sort(key=lambda row: (row["start"], row["id"]))
    return events[:RAID_LIMIT]


def fetch_raids(opener, config, key, now):
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
    return {"status": "ok", "checked": now, "events": raid_summaries(collected, server, start, end)}


def fetch_twitch(opener, streamers, client_id, client_secret):
    """Live Twitch channels among the listed streamers. Returns (live entries, extra secrets to redact)."""
    by_login = {entry["twitch"]: entry for entry in streamers if "twitch" in entry}
    form = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret, "grant_type": "client_credentials"}).encode("ascii")
    answer = decode(request(opener, "https://id.twitch.tv/oauth2/token", "Twitch sign-in", data=form,
                            headers={"Content-Type": "application/x-www-form-urlencoded"}), "Twitch sign-in")
    token = answer.get("access_token")
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._-]{8,512}", token):
        raise SourceError("Twitch sign-in did not return a token")
    query = urllib.parse.urlencode([("user_login", login) for login in by_login] + [("first", "100")])
    answer = decode(request(opener, "https://api.twitch.tv/helix/streams?" + query, "Twitch", headers={
        "Client-Id": client_id, "Authorization": "Bearer " + token}), "Twitch")
    live = []
    for stream in answer.get("data") if isinstance(answer.get("data"), list) else []:
        login = stream.get("user_login") if isinstance(stream, dict) else None
        login = login.lower() if isinstance(login, str) else None
        if login in by_login and stream.get("type") == "live":
            person = by_login.pop(login)
            live.append({"name": person["name"], "platform": "twitch", "channel": person["twitch"],
                         "title": text(stream.get("title"), 140) or "Live on Twitch"})
    return live, [token]


def fetch_youtube(opener, streamers, key):
    """Live YouTube broadcasts: each channel's newest feed entries, then one videos call per 50 IDs."""
    candidates = []
    for person in (entry for entry in streamers if "youtube" in entry):
        feed = request(opener, "https://www.youtube.com/feeds/videos.xml?channel_id=" + person["youtube"],
                       "YouTube feed", missing_ok=True)
        if feed is None:
            continue
        found = re.findall(r"<yt:videoId>([A-Za-z0-9_-]{11})</yt:videoId>", feed.decode("utf-8", "replace"))
        candidates += [(video, person) for video in dict.fromkeys(found[:YOUTUBE_RECENT])]
    live, shown = [], set()
    for start in range(0, len(candidates), 50):
        chunk = candidates[start:start + 50]
        query = urllib.parse.urlencode({"part": "snippet", "id": ",".join(video for video, _ in chunk), "key": key})
        answer = decode(request(opener, "https://www.googleapis.com/youtube/v3/videos?" + query, "YouTube"), "YouTube")
        details = {item["id"]: item.get("snippet") for item in answer.get("items") or []
                   if isinstance(item, dict) and isinstance(item.get("id"), str)}
        for video, person in chunk:
            snippet = details.get(video)
            if (isinstance(snippet, dict) and snippet.get("liveBroadcastContent") == "live"
                    and snippet.get("channelId") == person["youtube"] and person["name"] not in shown):
                shown.add(person["name"])
                live.append({"name": person["name"], "platform": "youtube", "channel": person["youtube"],
                             "video": video, "title": text(snippet.get("title"), 140) or "Live on YouTube"})
    return live


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

    try:
        raid = fetch_raids(opener, config, key, now)
        if raid["status"] == "ok":
            say(f"Raid sign-ups: {len(raid['events'])} upcoming events saved as counts by role (no names).")
        else:
            say("Raid sign-ups: not configured (needs raidHelper.serverId and the RAIDHELPER_API_KEY secret).")
    except (raidhelper.BridgeError, SourceError) as error:
        raid = {"status": "error", "checked": now, "events": []}
        say("Raid sign-ups: could not update. " + redact(str(error), secrets))

    live, statuses = [], {}
    client_id, client_secret = environ.get("TWITCH_CLIENT_ID", ""), environ.get("TWITCH_CLIENT_SECRET", "")
    if not any("twitch" in entry for entry in config["streamers"]) or not client_id or not client_secret:
        statuses["twitch"] = "unconfigured"
    else:
        try:
            found, extra = fetch_twitch(opener, config["streamers"], client_id, client_secret)
            secrets += extra
            live += found
            statuses["twitch"] = "ok"
        except SourceError as error:
            statuses["twitch"] = "error"
            say("Twitch: could not check. " + redact(str(error), secrets))
    youtube_key = environ.get("YOUTUBE_API_KEY", "")
    if not any("youtube" in entry for entry in config["streamers"]) or not youtube_key:
        statuses["youtube"] = "unconfigured"
    else:
        try:
            live += fetch_youtube(opener, config["streamers"], youtube_key)
            statuses["youtube"] = "ok"
        except SourceError as error:
            statuses["youtube"] = "error"
            say("YouTube: could not check. " + redact(str(error), secrets))
    checked = now if any(status != "unconfigured" for status in statuses.values()) else None
    say(f"Live streams: {len(live)} live now (Twitch {statuses['twitch']}, YouTube {statuses['youtube']}).")

    try:
        fetched, waiting = fetch_items(opener, cache, wanted, now, sleep)
        say(f"Wowhead items: {fetched} looked up" + (f", {waiting} left for the next run." if waiting else "."))
    except SourceError as error:
        say("Wowhead items: stopped early. " + redact(str(error), secrets))

    document = {"format": DATA_FORMAT, "version": VERSION, "generated": now, "raidHelper": raid,
                "streams": {"checked": checked, "twitch": statuses["twitch"], "youtube": statuses["youtube"],
                            "live": live[: 2 * STREAMER_LIMIT]}}
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
    fetch = commands.add_parser("fetch", help="download raid sign-up counts, live streams and item names")
    fetch.add_argument("--config", required=True, metavar="FILE", help="community.json")
    fetch.add_argument("--data", metavar="DIR", help="folder with the published standings.json and history.json")
    fetch.add_argument("--items", metavar="FILE", help="item-cache.json to read and update")
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
