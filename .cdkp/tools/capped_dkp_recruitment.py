# SPDX-License-Identifier: Apache-2.0
"""Public Guilds of WoW openings only; no API, credentials, applications or script execution.

The listing's HTML contract was inspected on 2026-11-18. Unknown markup fails closed:
only a completely validated snapshot replaces the last-good cache.
"""

import http.client
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

import capped_dkp_ledger as store

URL = "https://guildsofwow.com/peons-with-benefits"
GUILD_ID = "8003f5c8-af13-433e-b8d2-33fc1129ec7f"
GUILD_NAME = "Peons with Benefits"
FORMAT = "pwb-recruitment"
LIMIT = 2 * 1024 * 1024
CACHE_LIMIT = 32 * 1024
EMPTY = "This guild hasn't listed any open positions for recruitment."
SPECS = {
    "Druid": ("Balance", "Feral", "Guardian", "Restoration"),
    "Hunter": ("Beast Mastery", "Marksmanship", "Survival"),
    "Mage": ("Arcane", "Fire", "Frost"),
    "Paladin": ("Holy", "Protection", "Retribution"),
    "Priest": ("Discipline", "Holy", "Shadow"),
    "Rogue": ("Assassination", "Combat", "Subtlety"),
    "Shaman": ("Elemental", "Enhancement", "Restoration"),
    "Warlock": ("Affliction", "Demonology", "Destruction"),
    "Warrior": ("Arms", "Fury", "Protection"),
}
ERRORS = {
    "fetch": "Public listing could not be fetched (HTTP, redirect or network failure).",
    "markup": "Public listing was challenged, incomplete or its recruitment markup changed.",
    "cache": "Saved recruitment cache is invalid; it was not used.",
    "missing": "Recruitment sync has no persistent cache path or fetched data.",
}


class SourceError(Exception):
    pass


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    text: list = field(default_factory=list)
    closed: bool = False

    def find(self, *, tag=None, attr=None, value=None, cls=None):
        found = []
        for child in self.children:
            if ((tag is None or child.tag == tag) and
                    (attr is None or child.attrs.get(attr) == value) and
                    (cls is None or cls in child.attrs.get("class", "").split())):
                found.append(child)
            found.extend(child.find(tag=tag, attr=attr, value=value, cls=cls))
        return found

    def content(self):
        return " ".join(" ".join(self.text + [child.content() for child in self.children]).split())


class Markup(HTMLParser):
    VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.stack = [self.root]
        self.count = 0

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 30000 or len(self.stack) > 100:
            raise SourceError("markup")
        node = Node(tag, dict(attrs), closed=tag in self.VOID)
        self.stack[-1].children.append(node)
        if not node.closed:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack[index].closed = True
                del self.stack[index:]
                return

    def handle_data(self, data):
        self.stack[-1].text.append(data)


def one(nodes):
    if len(nodes) != 1 or not nodes[0].closed:
        raise SourceError("markup")
    return nodes[0]


def openings_checked(value):
    if not isinstance(value, list) or len(value) > len(SPECS):
        raise store.IntegrityError("Recruitment openings must be a bounded list")
    seen = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {"class", "specs"}:
            raise store.IntegrityError("Recruitment opening has unexpected fields")
        klass, specs = row["class"], row["specs"]
        if not isinstance(klass, str) or klass not in SPECS or klass in seen:
            raise store.IntegrityError("Recruitment class is unknown or repeated")
        seen.add(klass)
        if not isinstance(specs, list) or not 1 <= len(specs) <= len(SPECS[klass]):
            raise store.IntegrityError("Recruitment specs must be a nonempty bounded list")
        names = set()
        for spec in specs:
            if not isinstance(spec, dict) or set(spec) != {"name", "priority"}:
                raise store.IntegrityError("Recruitment spec has unexpected fields")
            name, priority = spec["name"], spec["priority"]
            if not isinstance(name, str) or name not in SPECS[klass] or name in names:
                raise store.IntegrityError("Recruitment spec is unknown, mismatched or repeated")
            if priority not in ("high", "mid", "low"):
                raise store.IntegrityError("Recruitment priority is unknown")
            names.add(name)
    return value


def parse(body):
    if len(body) > LIMIT:
        raise SourceError("markup")
    try:
        markup = Markup()
        markup.feed(body.decode("utf-8"))
        markup.close()
        root = markup.root
        orgs = [json.loads(node.content()) for node in root.find(tag="script", attr="type",
                                                                value="application/ld+json")]
        if sum(isinstance(org, dict) and org.get("@type") == "Organization" and
               org.get("name") == GUILD_NAME and org.get("url") == URL for org in orgs) != 1:
            raise SourceError("markup")
        header = one(root.find(tag="header", attr="id", value="guild-header"))
        if not header.find(attr="data-guild-id", value=GUILD_ID):
            raise SourceError("markup")
        if one(header.find(tag="h1")).content() != GUILD_NAME:
            raise SourceError("markup")
        if "PvP \u2022 EU \u2022 Forever" not in one(header.find(cls="sub-description")).content():
            raise SourceError("markup")
        section = one(root.find(tag="section", attr="id", value="recruitment"))
        if one(section.find(tag="h2")).content() != "Recruitment":
            raise SourceError("markup")
        links = section.find(tag="a", attr="data-guild", value="peons-with-benefits")
        if not links or any(link.attrs.get("href") != URL + "/recruitment" for link in links):
            raise SourceError("markup")
        rows = section.find(cls="recruitment-class-item")
        empty = [node for node in section.find(tag="nhea-formatted-text") if node.content() == EMPTY]
        if not rows:
            if len(empty) != 1 or not empty[0].closed or section.find(cls="spec-item") \
                    or section.find(cls="recruitment-classes"):
                raise SourceError("markup")
            return []
        if empty:
            raise SourceError("markup")
        container = one(section.find(cls="recruitment-classes"))
        if container.find(cls="recruitment-class-item") != rows:
            raise SourceError("markup")
        result, count = [], 0
        for row in rows:
            if not row.closed:
                raise SourceError("markup")
            klass = one(row.find(tag="img", cls="recruitment-class-image")).attrs.get("alt")
            specs = []
            for item in row.find(cls="spec-item"):
                image = one(item.find(tag="img"))
                name = image.attrs.get("alt", "").removeprefix(str(klass) + " ")
                if image.attrs.get("alt") != f"{klass} {name}" or not item.closed:
                    raise SourceError("markup")
                tooltip = item.attrs.get("data-tooltip", "")
                match = re.fullmatch(re.escape(name) + r"\s*<nhea-badge\b[^<>]*>"
                                     r"(High|Mid|Low) Priority</nhea-badge>", tooltip)
                if not match:
                    raise SourceError("markup")
                specs.append({"name": name, "priority": match[1].lower()})
                count += 1
            result.append({"class": klass, "specs": specs})
        if count != len(section.find(cls="spec-item")) or len(container.find(tag="img")) != count + len(rows):
            raise SourceError("markup")
        return openings_checked(result)
    except (UnicodeDecodeError, ValueError, RecursionError, store.IntegrityError):
        raise SourceError("markup") from None


def timestamp(value):
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= store.MAX_SAFE_INTEGER


def check_feed(value):
    required = {"source", "guildId", "status", "checked", "updated", "openings"}
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"error"}:
        raise store.IntegrityError("Recruitment feed has unexpected fields")
    if value["source"] != URL or value["guildId"] != GUILD_ID or not timestamp(value["checked"]):
        raise store.IntegrityError("Recruitment source identity or check time is invalid")
    status, updated = value["status"], value["updated"]
    openings_checked(value["openings"])
    if status not in ("ok", "stale", "error"):
        raise store.IntegrityError("Recruitment status must be ok, stale or error")
    if status == "error":
        if updated is not None or value["openings"]:
            raise store.IntegrityError("Unavailable recruitment cannot contain a successful snapshot")
    elif not timestamp(updated) or updated > value["checked"]:
        raise store.IntegrityError("Recruitment success time is invalid")
    if status == "ok":
        if "error" in value or updated != value["checked"]:
            raise store.IntegrityError("Successful recruitment has inconsistent state")
    elif not isinstance(value.get("error"), str) or value["error"] not in ERRORS:
        raise store.IntegrityError("Recruitment failure needs a known error code")
    return value


def unavailable(now, error="missing"):
    return {"source": URL, "guildId": GUILD_ID, "status": "error", "checked": now,
            "updated": None, "openings": [], "error": error}


def refresh(path, opener, now, say):
    previous, cache_error = None, False
    if not path:
        say("Recruitment: " + ERRORS["missing"])
        return unavailable(now)
    path = Path(path)
    if path.exists():
        try:
            document = store.read_json(path, CACHE_LIMIT, "recruitment cache")
            if not isinstance(document, dict) or set(document) != {"format", "version", "recruitment"} \
                    or document["format"] != FORMAT or document["version"] != 1:
                raise store.IntegrityError("Recruitment cache header is invalid")
            previous = check_feed(document["recruitment"])
            if previous["status"] != "ok" or previous["checked"] > now:
                raise store.IntegrityError("Recruitment cache is not a past successful snapshot")
        except (store.ToolError, OSError):
            previous, cache_error = None, True
            say("Recruitment: " + ERRORS["cache"])
    try:
        request = urllib.request.Request(URL, headers={"User-Agent": "CappedDKP-PublicSite/1.0",
                                                       "Accept": "text/html"})
        try:
            with opener.open(request, timeout=20) as response:
                if response.status != 200 or response.geturl() != URL:
                    raise SourceError("fetch")
                if response.headers.get_content_type() != "text/html":
                    raise SourceError("markup")
                body = response.read(LIMIT + 1)
        except (urllib.error.URLError, OSError, http.client.HTTPException):
            raise SourceError("fetch") from None
        feed = {"source": URL, "guildId": GUILD_ID, "status": "ok", "checked": now, "updated": now,
                "openings": parse(body)}
        check_feed(feed)
    except SourceError as error:
        code = str(error)
        feed = ({**previous, "status": "stale", "checked": now, "error": code} if previous
                else unavailable(now, "cache" if cache_error else code))
        say("Recruitment: " + ERRORS[code] + (" Keeping last-known-good openings." if previous else
                                             " No verified source snapshot is available."))
        return feed
    store.write_atomic(path, store.indented({"format": FORMAT, "version": 1, "recruitment": feed}))
    say(f"Recruitment: verified {sum(len(row['specs']) for row in feed['openings'])} public spec openings.")
    return feed
