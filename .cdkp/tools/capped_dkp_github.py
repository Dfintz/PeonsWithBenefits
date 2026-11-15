# SPDX-License-Identifier: Apache-2.0
"""Original minimal GitHub REST client shared by the upload companion and the ledger-repository CI.

Only https://api.github.com is contacted; redirects are refused, responses are size-limited, and error
messages name the status and the request but never the token. Standard library only.
"""

import base64
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_TOOLS = str(Path(__file__).resolve().parent)
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

from capped_dkp_ledger import RemoteError, ToolError  # noqa: E402

API = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "CappedDKP-Tools/1.0"
TIMEOUT_SECONDS = 30
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
PAGE_SIZE, MAX_PAGES = 100, 30
TOKEN_PATTERN = re.compile(r"gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255}")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
_BRANCH_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")


def check_repository(value: str) -> str:
    text = (value or "").strip()
    if not REPOSITORY_PATTERN.fullmatch(text) or text.endswith((".", ".git")) or "/." in text:
        raise ToolError("The repository must look like owner/name (for example my-guild/capped-dkp-ledger)")
    return text


def check_branch(value: str, label="branch") -> str:
    text = (value or "").strip()
    parts = text.split("/")
    if not text or len(text) > 200 or ".." in text or not all(_BRANCH_PART.fullmatch(part) for part in parts) \
            or any(part.endswith((".", ".lock")) for part in parts):
        raise ToolError(f"The {label} name {text!r} is not a simple Git branch name")
    return text


def check_path(value: str, label="path") -> str:
    text = (value or "").strip().strip("/")
    parts = text.split("/")
    if not text or len(text) > 200 or any(part in ("", ".", "..") or not _BRANCH_PART.fullmatch(part)
                                          for part in parts):
        raise ToolError(f"The {label} {value!r} must be a relative folder such as ledger")
    return text


def check_token(value: str) -> str:
    text = (value or "").strip()
    if not TOKEN_PATTERN.fullmatch(text):
        raise ToolError("That does not look like a GitHub token (fine-grained tokens start with github_pat_)")
    return text


def mask(text: str, *secrets) -> str:
    text = TOKEN_PATTERN.sub("***", str(text))
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, "***")
    return text


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def default_opener():
    return urllib.request.build_opener(NoRedirect)


def _short(path: str) -> str:
    return path.split("?", 1)[0][:120]


def _api_message(error) -> str:
    try:
        body = error.read(65536) if error.fp is not None else b""
        message = json.loads(body.decode("utf-8")).get("message", "")
    except (ValueError, AttributeError, OSError, UnicodeError):
        return ""
    return " ".join(str(message).split())[:200]


def _reset_text(headers) -> str:
    value = str(headers.get("x-ratelimit-reset", "") if headers else "").strip()
    if not value.isdigit():
        return ""
    return " It resets at " + time.strftime("%H:%M UTC", time.gmtime(int(value))) + "."


def describe(status: int, method: str, path: str, message: str, headers) -> str:
    where = f"GitHub answered HTTP {status} to {method} {_short(path)}"
    detail = f" ({message})" if message else ""
    remaining = str(headers.get("x-ratelimit-remaining", "") if headers else "")
    if status == 401:
        hint = " The token is invalid or expired; create a new fine-grained token and store it again."
    elif status == 429 or (status == 403 and remaining == "0"):
        hint = " GitHub is rate limiting this token." + _reset_text(headers)
    elif status == 403:
        hint = (" The token lacks access: it needs Contents and Pull requests read/write on this repository only"
                " (or the organization has not approved the token yet).")
    elif status == 404:
        hint = " Not found, or the token cannot see this repository."
    elif status == 409:
        hint = " Conflict: an empty repository must get a first commit (for example a README) before uploads."
    else:
        hint = ""
    return where + detail + "." + hint


class GitHub:
    """Calls for one repository. Every method raises RemoteError on network or API failure."""

    def __init__(self, token: str, repository: str, opener=None):
        self.token = check_token(token)
        self.repository = check_repository(repository)
        self.owner = self.repository.split("/", 1)[0]
        self.opener = opener or default_opener()

    def request(self, method, path, body=None, allow=(), raw=False):
        url = API + path
        data = None if body is None else json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/vnd.github.raw+json" if raw else "application/vnd.github+json")
        request.add_header("Authorization", "Bearer " + self.token)
        request.add_header("X-GitHub-Api-Version", API_VERSION)
        request.add_header("User-Agent", USER_AGENT)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                status = getattr(response, "status", None) or getattr(response, "code", 200)
                content = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code in allow:
                return error.code, None
            raise RemoteError(mask(describe(error.code, method, path, _api_message(error), error.headers),
                                   self.token)) from None
        except (urllib.error.URLError, OSError) as error:
            raise RemoteError(f"Could not reach GitHub ({type(error).__name__}); check the connection and retry") \
                from None
        if not 200 <= status < 300:
            raise RemoteError(f"GitHub answered HTTP {status} to {method} {_short(path)}")
        if len(content) > MAX_RESPONSE_BYTES:
            raise RemoteError(f"GitHub's answer to {method} {_short(path)} is larger than expected")
        if raw:
            return status, content
        if not content:
            return status, None
        try:
            return status, json.loads(content.decode("utf-8"))
        except (ValueError, UnicodeError):
            raise RemoteError(f"GitHub's answer to {method} {_short(path)} is not JSON") from None

    def repo(self, suffix: str) -> str:
        return f"/repos/{self.repository}/{suffix}"

    def get(self, suffix, allow=()):
        return self.request("GET", self.repo(suffix), allow=allow)[1]

    def post(self, suffix, body):
        return self.request("POST", self.repo(suffix), body)[1]

    def patch(self, suffix, body):
        return self.request("PATCH", self.repo(suffix), body)[1]

    def paginate(self, suffix):
        items, joiner = [], "&" if "?" in suffix else "?"
        for page in range(1, MAX_PAGES + 1):
            chunk = self.get(f"{suffix}{joiner}per_page={PAGE_SIZE}&page={page}")
            if not isinstance(chunk, list):
                raise RemoteError(f"GitHub's answer to GET {_short(suffix)} is not a list")
            items += chunk
            if len(chunk) < PAGE_SIZE:
                return items
        raise RemoteError(f"GET {_short(suffix)} returned more than {PAGE_SIZE * MAX_PAGES} items")

    @staticmethod
    def _sha(document, *keys):
        value = document
        for key in keys:
            value = value.get(key) if isinstance(value, dict) else None
        if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
            raise RemoteError("GitHub returned an object without a valid SHA")
        return value

    def branch_head(self, branch):
        """Commit SHA the branch points to, or None when the branch does not exist."""
        document = self.get("git/ref/heads/" + urllib.parse.quote(branch, safe="/"), allow=(404,))
        return None if document is None else self._sha(document, "object", "sha")

    def commit(self, sha):
        document = self.get("git/commits/" + sha)
        parents = [self._sha(parent, "sha") for parent in document.get("parents") or []]
        return {"sha": self._sha(document, "sha"), "tree": self._sha(document, "tree", "sha"), "parents": parents}

    def tree(self, sha):
        document = self.get("git/trees/" + sha)
        entries = document.get("tree") if isinstance(document, dict) else None
        if not isinstance(entries, list) or document.get("truncated"):
            raise RemoteError("GitHub returned an incomplete tree")
        return entries

    def subtree(self, tree_sha, path):
        """SHA of the folder at path inside tree_sha, or None when it does not exist."""
        current = tree_sha
        for part in path.split("/"):
            match = [entry for entry in self.tree(current) if entry.get("path") == part]
            if not match or match[0].get("type") != "tree":
                return None
            current = self._sha(match[0], "sha")
        return current

    def file_at(self, path, ref):
        """Raw file bytes at a commit, or None when the file does not exist there."""
        quoted = urllib.parse.quote(path, safe="/")
        status, content = self.request("GET", self.repo(f"contents/{quoted}?ref={ref}"), allow=(404,), raw=True)
        return None if status == 404 else content

    def create_blob(self, content: bytes):
        document = self.post("git/blobs", {"content": base64.b64encode(content).decode("ascii"),
                                           "encoding": "base64"})
        return self._sha(document, "sha")

    def create_tree(self, entries, base_tree=None):
        body = {"tree": entries}
        if base_tree:
            body["base_tree"] = base_tree
        return self._sha(self.post("git/trees", body), "sha")

    def create_commit(self, message, tree, parents):
        return self._sha(self.post("git/commits", {"message": message, "tree": tree, "parents": parents}), "sha")

    def move_branch(self, branch, sha, exists, force=False):
        if exists:
            self.patch("git/refs/heads/" + urllib.parse.quote(branch, safe="/"), {"sha": sha, "force": force})
        else:
            self.post("git/refs", {"ref": "refs/heads/" + branch, "sha": sha})

    def open_pull(self, branch, base):
        query = urllib.parse.urlencode({"head": f"{self.owner}:{branch}", "base": base, "state": "open"})
        pulls = self.get("pulls?" + query)
        if not isinstance(pulls, list):
            raise RemoteError("GitHub's pull request list is not a list")
        return pulls[0] if pulls else None

    def create_pull(self, title, body, branch, base):
        return self.post("pulls", {"title": title, "body": body, "head": branch, "base": base})

    def update_pull(self, number, title, body):
        return self.patch(f"pulls/{int(number)}", {"title": title, "body": body})
