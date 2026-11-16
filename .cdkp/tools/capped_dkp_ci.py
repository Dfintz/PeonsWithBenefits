# SPDX-License-Identifier: Apache-2.0
"""Original GitHub Actions steps for the private Capped DKP ledger repository.

  toolkit  check the vendored tools in .cdkp/ against .cdkp/TOOLKIT.json
  check    verify a ledger change: only the three ledger files, append-only history, addon replay
  gate     after a merge to main, require an approved pull request for every ledger commit
  mirror   push the generated public site to a separate public repository (fast-forward only)
  notify   tell the officers' Discord channel that a run failed (skipped when no webhook is set)

These steps record and publish what officers approved; they are not access control. Anyone with write
access can change the workflows, and a pull_request run uses the pull request's own workflow file.
Standard library only; git is called without a shell. Secrets come only from the environment and are
never printed.
"""

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_TOOLS = str(Path(__file__).resolve().parent)
if _TOOLS not in sys.path:
    sys.path.insert(0, _TOOLS)

import capped_dkp_github as gh  # noqa: E402
import capped_dkp_ledger as store  # noqa: E402
import capped_dkp_publish as publish  # noqa: E402
import capped_dkp_snapshot as snapshot  # noqa: E402
from capped_dkp_ledger import IntegrityError, RemoteError, ToolError  # noqa: E402

TOOLKIT_FORMAT, OFFICERS_FORMAT = "capped-dkp-toolkit", "capped-dkp-officers"
TOOLKIT_FILE, OFFICERS_FILE = "TOOLKIT.json", "officers.json"
TOOLKIT_KEYS = {"format", "version", "addonVersion", "files", "workflows"}
OFFICERS_KEYS = {"format", "version", "requiredApprovals", "officers"}
LEDGER_FILES = (store.JOURNAL, store.ROWS, store.MANIFEST)
SITE_FILES = ("index.html", "standings.json", "history.json", *publish.PAGE_ASSETS)
ZERO_SHA = "0" * 40
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
REVISION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._/-]{0,199}")
LOGIN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
WORKFLOW_PATH = re.compile(r"\.github/workflows/cdkp-[a-z]+\.yml")
HEX64 = re.compile(r"[0-9a-f]{64}")
SERVER = re.compile(r"https://[A-Za-z0-9.-]{1,100}")
DECISIVE = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
GIT_TIMEOUT = 120
MAX_PUSH_COMMITS = 200
MAX_GATED_COMMITS = 20
MAX_HISTORY_WALK = 5000
OFFICERS_BYTES = 64 * 1024
TOOLKIT_BYTES = 1024 * 1024
SITE_BYTES = 32 * 1024 * 1024
SUMMARY_CHANGES = 25
SUMMARY_EVENTS = 50
SECRET_VARIABLES = ("GH_TOKEN", "GITHUB_TOKEN", "CDKP_PUBLIC_REPO_TOKEN", "DISCORD_OFFICER_WEBHOOK_URL",
                    publish.WEBHOOK_VARIABLE)
NOTIFY_TEXT = {
    "validate": "CappedDKP: the check of a ledger pull request failed. Do not merge it until the run passes.",
    "publish": "CappedDKP: publishing after a merge to main failed. The public standings may be out of date.",
}


def say(text):
    publish.say(text)


def safe(text, limit=300):
    """Untrusted text (paths, logins, Git output) on one printable line, so it cannot start a workflow command."""
    return store.clip(str(text), limit)


def md(text, limit=120):
    """Untrusted text for the Markdown step summary: cleaned, HTML-escaped and with Markdown syntax escaped."""
    cleaned = publish.clean(text, limit)
    return html.escape(re.sub(r"([\\`*_{}\[\]()#+!|~>-])", r"\\\1", cleaned), quote=False)


# Git -----------------------------------------------------------------------------------------------

class Git:
    """Read-only Git queries against one repository; every argument is passed as a list, never via a shell."""

    def __init__(self, root):
        self.root = Path(root)

    def run(self, *args, allow_fail=False):
        command = ["git", "-c", "core.quotepath=off", "-C", str(self.root), *args]
        environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
        try:
            done = subprocess.run(command, capture_output=True, timeout=GIT_TIMEOUT, check=False, env=environment)
        except subprocess.TimeoutExpired:
            raise ToolError(f"git {args[0]} did not finish within {GIT_TIMEOUT} seconds") from None
        except OSError as error:
            raise ToolError(f"Cannot run git ({type(error).__name__}); install Git and retry") from None
        if done.returncode != 0:
            if allow_fail:
                return None
            raise ToolError(f"git {args[0]} failed: " + safe(done.stderr.decode("utf-8", "replace")))
        return done.stdout

    def resolve(self, revision, label):
        """Commit SHA for a revision, or None for the all-zero SHA GitHub sends when there was no previous commit."""
        text = (revision or "").strip()
        if text == ZERO_SHA:
            return None
        if not REVISION.fullmatch(text) or ".." in text or text.endswith((".", "/", ".lock")):
            raise ToolError(f"--{label} {safe(text, 80)!r} is not a plain commit SHA or branch name")
        found = self.run("rev-parse", "--verify", "--quiet", "--end-of-options", text + "^{commit}",
                         allow_fail=True)
        sha = (found or b"").decode("ascii", "replace").strip()
        if not gh.SHA_PATTERN.fullmatch(sha):
            raise ToolError(f"--{label} {safe(text, 80)} is not a commit in this clone "
                            "(the workflow checkout needs fetch-depth: 0)")
        return sha

    def merge_base(self, first, second):
        found = self.run("merge-base", first, second, allow_fail=True)
        sha = (found or b"").decode("ascii", "replace").strip()
        if not gh.SHA_PATTERN.fullmatch(sha):
            raise IntegrityError("The pull request shares no history with its base branch")
        return sha

    def names(self, output):
        return [name for name in output.decode("utf-8", "replace").split("\0") if name]

    def changed(self, base, head):
        return self.names(self.run("diff", "--name-only", "-z", "--no-renames", base or EMPTY_TREE, head, "--"))

    def files(self, revision, folder):
        if revision is None:
            return []
        return self.names(self.run("ls-tree", "-r", "-z", "--name-only", revision, "--", folder + "/"))

    def blob(self, revision, path):
        """File bytes at a revision, or None when the file does not exist there."""
        return self.run("cat-file", "blob", f"{revision}:{path}", allow_fail=True)

    def first_parent(self, sha):
        parts = self.run("rev-list", "--parents", "--max-count=1", sha).decode("ascii", "replace").split()
        return parts[1] if len(parts) > 1 else None

    def first_parents(self, head, stop, limit):
        args = ["rev-list", "--first-parent", f"--max-count={limit}", head]
        if stop:
            args.append("^" + stop)
        return self.run(*args).decode("ascii", "replace").split()

    def touches(self, sha, folder):
        parent = self.first_parent(sha)
        if parent is None:
            output = self.run("diff-tree", "-r", "-z", "--name-only", "--no-commit-id", "--root", sha, "--", folder)
        else:
            output = self.run("diff-tree", "-r", "-z", "--name-only", "--no-commit-id", parent, sha, "--", folder)
        return bool(self.names(output))


# Actions plumbing ----------------------------------------------------------------------------------

def append_file(environ, variable, text):
    target = environ.get(variable)
    if not target:
        return False
    with open(target, "a", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
    return True


def write_outputs(environ, values):
    text = "".join(f"{key}={value}\n" for key, value in values.items())
    if not append_file(environ, "GITHUB_OUTPUT", text):
        for line in text.splitlines():
            say("output " + line)


def write_summary(environ, lines):
    text = "\n".join(lines) + "\n"
    if not append_file(environ, "GITHUB_STEP_SUMMARY", text):
        say(text.rstrip("\n"))


def warn(environ, message):
    if environ.get("GITHUB_ACTIONS") == "true":
        say("::warning title=CappedDKP::" + command_text(message))
    else:
        say("Warning: " + message)


def command_text(message):
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


# toolkit -------------------------------------------------------------------------------------------

def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def toolkit_hashes(root):
    """sha256 of every file below root, keyed by POSIX relative path; skips TOOLKIT.json and __pycache__."""
    root = Path(root)
    found = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or relative.as_posix() == TOOLKIT_FILE or not path.is_file():
            continue
        found[relative.as_posix()] = file_digest(path)
    return found


def read_toolkit(root):
    document = store.read_json(Path(root) / TOOLKIT_FILE, TOOLKIT_BYTES, TOOLKIT_FILE)
    if not isinstance(document, dict) or set(document) != TOOLKIT_KEYS or document["format"] != TOOLKIT_FORMAT \
            or document["version"] != 1 or not isinstance(document["addonVersion"], str):
        raise IntegrityError(f"{TOOLKIT_FILE} is not a CappedDKP toolkit manifest; run the scaffold with --update")
    for key in ("files", "workflows"):
        table = document[key]
        if not isinstance(table, dict) or not all(isinstance(value, str) and HEX64.fullmatch(value)
                                                  for value in table.values()):
            raise IntegrityError(f"{TOOLKIT_FILE} has an invalid {key} table")
    if not all(WORKFLOW_PATH.fullmatch(path) for path in document["workflows"]):
        raise IntegrityError(f"{TOOLKIT_FILE} lists a workflow outside .github/workflows/cdkp-*.yml")
    return document


def run_toolkit(args, environ, opener):
    root = Path(args.root) if args.root else Path(__file__).resolve().parents[1]
    if not (root / TOOLKIT_FILE).is_file():
        raise ToolError(f"{root} has no {TOOLKIT_FILE}; run the copy of this tool inside the ledger repository's .cdkp")
    document = read_toolkit(root)
    expected, actual = document["files"], toolkit_hashes(root)
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    changed = sorted(path for path in set(expected) & set(actual) if expected[path] != actual[path])
    problems = [f"{label}: {', '.join(safe(path, 80) for path in paths[:5])}"
                for label, paths in (("missing", missing), ("changed", changed), ("unexpected", extra)) if paths]
    if problems:
        raise IntegrityError("The vendored toolkit in .cdkp does not match TOOLKIT.json (" + "; ".join(problems)
                             + "). Restore it from Git or refresh it with the scaffold's --update")
    repository = root.parent
    for path, digest in sorted(document["workflows"].items()):
        target = repository / path
        if not target.is_file():
            warn(environ, f"{path} is missing; the CappedDKP workflows may not run")
        elif file_digest(target) != digest:
            warn(environ, f"{path} differs from the scaffolded copy; review the change before trusting this run")
    say(f"Toolkit verified: {len(expected)} files for CappedDKP {safe(document['addonVersion'], 20)}.")


# check ---------------------------------------------------------------------------------------------

def extract(git, revision, folder, target, label):
    """Copy the ledger files at a revision into target; None when the revision has no ledger."""
    listed = set(git.files(revision, folder))
    present = [name for name in LEDGER_FILES if f"{folder}/{name}" in listed]
    if not present:
        return None
    if len(present) != len(LEDGER_FILES):
        raise IntegrityError(f"The {label} ledger is incomplete (has only {', '.join(present)})")
    target.mkdir(parents=True)
    for name in LEDGER_FILES:
        (target / name).write_bytes(git.run("cat-file", "blob", f"{revision}:{folder}/{name}"))
    return target


def accounts(rows):
    found = {}
    for row in rows:
        found.setdefault(row["account"], row)
    return found


def balance_lines(previous, ledger):
    before = accounts(previous.rows) if previous else {}
    after = accounts(ledger.rows)
    changes = []
    for name in sorted(set(before) | set(after), key=lambda text: (text.lower(), text)):
        old, new = before.get(name), after.get(name)
        old_balance = old["balance"] if old else None
        new_balance = new["balance"] if new else None
        if old_balance != new_balance or (old and new and old["departed"] != new["departed"]):
            changes.append((name, old_balance, new_balance, bool(new and new["departed"])))
    if not changes:
        return ["No account balance changed."]
    lines = ["| Account | Before | After | Change |", "| --- | ---: | ---: | ---: |"]
    for name, old, new, departed in changes[:SUMMARY_CHANGES]:
        delta = publish.signed(new - old) if old is not None and new is not None else ""
        shown = md(name) + (" (departed)" if departed else "")
        lines.append(f"| {shown} | {'' if old is None else publish.number_text(old)} "
                     f"| {'' if new is None else publish.number_text(new)} | {delta} |")
    if len(changes) > SUMMARY_CHANGES:
        lines.append(f"\n{len(changes) - SUMMARY_CHANGES} more accounts changed; see characters.json in the diff.")
    return lines


def event_lines(ledger, first):
    events, _ = publish.project_events(ledger)
    fresh = [event for event in events if event["id"] >= first]
    if not fresh:
        return ["No public events were added (private records are not listed)."]
    lines = [f"- Event {event['id']}: {md(event['summary'], publish.SUMMARY_LIMIT)}"
             for event in fresh[:SUMMARY_EVENTS]]
    if len(fresh) > SUMMARY_EVENTS:
        lines.append(f"- ... and {len(fresh) - SUMMARY_EVENTS} more")
    return lines


def run_check(args, environ, opener):
    git = Git(args.repo)
    folder = gh.check_path(args.ledger, "ledger folder")
    head = git.resolve(args.head, "head")
    base = git.resolve(args.base, "base")
    if base and args.merge_base:
        base = git.merge_base(base, head)
    changed = git.changed(base, head)
    inside = [path for path in changed if path.startswith(folder + "/")]
    other = [path for path in changed if not path.startswith(folder + "/")]
    allowed = {f"{folder}/{name}" for name in LEDGER_FILES}
    extra = sorted(set(git.files(head, folder)) - allowed)
    if extra:
        raise IntegrityError(f"The {folder} folder may only hold {', '.join(LEDGER_FILES)}; it also has "
                             + ", ".join(safe(path, 80) for path in extra[:5]))
    if inside and other:
        raise IntegrityError("A ledger update must change only the ledger files; this change also touches "
                             + ", ".join(safe(path, 80) for path in other[:5])
                             + ". Split it into separate pull requests")
    outputs = {"ledger_changed": "false", "previous_events": 0, "events": 0, "added": 0, "first_new": 0,
               "announce": "false"}
    if not inside:
        write_outputs(environ, outputs)
        write_summary(environ, ["## Capped DKP ledger check", "",
                                f"No file in {md(folder)}/ changed, so there was nothing to verify."])
        say(f"No ledger change ({len(changed)} other files changed).")
        return
    replay = store.replay_from_args(args)
    with tempfile.TemporaryDirectory(prefix="cdkp-ci-", ignore_cleanup_errors=True) as scratch:
        new_folder = extract(git, head, folder, Path(scratch) / "new", "proposed")
        if new_folder is None:
            raise IntegrityError("This change deletes the ledger; the approved history must never be removed")
        old_folder = extract(git, base, folder, Path(scratch) / "old", "previous")
        ledger = store.load_ledger(new_folder)
        previous = store.load_ledger(old_folder) if old_folder else None
        if previous is not None:
            store.check_extends(previous, ledger)
        store.check_replay(ledger, replay)
    before = previous.count if previous else 0
    added = ledger.count - before
    outputs.update({"ledger_changed": "true", "previous_events": before, "events": ledger.count, "added": added,
                    "first_new": before + 1 if added else 0,
                    "announce": "true" if previous is not None and added > 0 else "false"})
    write_outputs(environ, outputs)
    if previous is None:
        events_line = (f"- Events: {ledger.count} (first ledger; this baseline is published but not announced "
                       "in Discord)")
    elif added:
        events_line = f"- Events: {before} → {ledger.count} ({added} new: {before + 1}–{ledger.count})"
    else:
        events_line = f"- Events: {ledger.count} (no new events)"
    lines = ["## Capped DKP ledger check", "", "- Result: passed", events_line,
             f"- Chain head: `{ledger.heads[-1]}`", "- " + publish.replay_note(replay.enabled), "",
             "### Balance changes", ""] + balance_lines(previous, ledger)
    if added:
        lines += ["", "### New events", ""] + event_lines(ledger, before + 1)
    write_summary(environ, lines)
    say(f"Ledger check passed: {ledger.count} events ({added} new), chain head {ledger.heads[-1]}.")
    say(publish.replay_note(replay.enabled))


# gate ----------------------------------------------------------------------------------------------

def parse_officers(content, where):
    try:
        if len(content) > OFFICERS_BYTES:
            raise ValueError("file too large")
        text = content.decode("utf-8-sig")
        document = json.loads(text, object_pairs_hook=snapshot.unique_object,
                              parse_constant=snapshot.invalid_number)
    except (ValueError, UnicodeError, RecursionError) as error:
        raise IntegrityError(f"{OFFICERS_FILE} at {where} is not valid JSON: {safe(error, 120)}") from None
    if not isinstance(document, dict) or set(document) != OFFICERS_KEYS or document["format"] != OFFICERS_FORMAT \
            or document["version"] != 1:
        raise IntegrityError(f"{OFFICERS_FILE} at {where} is not a CappedDKP officers file")
    required, officers = document["requiredApprovals"], document["officers"]
    if isinstance(required, bool) or not isinstance(required, int) or not 1 <= required <= 10:
        raise IntegrityError(f"{OFFICERS_FILE} at {where}: requiredApprovals must be a whole number from 1 to 10")
    if not isinstance(officers, list) or not all(isinstance(login, str) and LOGIN.fullmatch(login)
                                                 for login in officers):
        raise IntegrityError(f"{OFFICERS_FILE} at {where}: officers must be a list of GitHub usernames")
    lowered = [login.lower() for login in officers]
    if len(set(lowered)) != len(lowered):
        raise IntegrityError(f"{OFFICERS_FILE} at {where} lists an officer twice")
    if len(officers) < required + 1:
        raise IntegrityError(f"{OFFICERS_FILE} at {where} lists {len(officers)} officers; at least "
                             f"{required + 1} are needed so an officer's own submission can still be approved")
    return required, set(lowered)


def officers_at(git, revision):
    where = revision[:12] if revision else "the first commit"
    content = git.blob(revision, OFFICERS_FILE) if revision else None
    if content is None:
        raise IntegrityError(f"There is no {OFFICERS_FILE} at {where}, so nobody can approve ledger changes. "
                             "Add it through its own pull request first")
    return parse_officers(content, where)


def login_of(item):
    user = item.get("user") if isinstance(item, dict) else None
    login = user.get("login") if isinstance(user, dict) else None
    return login if isinstance(login, str) and LOGIN.fullmatch(login) else None


def gate_commit(git, github, sha, branch):
    """Require that sha is the merge of exactly one pull request into branch, approved per officers.json."""
    required, officers = officers_at(git, git.first_parent(sha))
    pulls = [pull for pull in github.paginate(f"commits/{sha}/pulls")
             if isinstance(pull, dict) and pull.get("merge_commit_sha") == sha and pull.get("merged_at")
             and isinstance(pull.get("base"), dict) and pull["base"].get("ref") == branch]
    if len(pulls) != 1:
        raise IntegrityError(f"Commit {sha[:12]} changed the ledger on {branch} without being the merge of one "
                             "pull request; ledger changes must be merged through an approved pull request")
    pull = pulls[0]
    number = pull.get("number")
    author = login_of(pull)
    head = pull.get("head", {}).get("sha") if isinstance(pull.get("head"), dict) else None
    if isinstance(number, bool) or not isinstance(number, int) or author is None \
            or not isinstance(head, str) or not gh.SHA_PATTERN.fullmatch(head):
        raise RemoteError("GitHub returned a pull request without a number, author or head commit")
    latest = {}
    for review in github.paginate(f"pulls/{number}/reviews"):
        login = login_of(review)
        if login and isinstance(review, dict) and review.get("state") in DECISIVE:
            latest[login.lower()] = (login, review["state"], review.get("commit_id"))
    blocking = sorted(login for login, state, _ in latest.values() if state == "CHANGES_REQUESTED")
    if blocking:
        raise IntegrityError(f"Pull request #{number} was merged while {', '.join(blocking)} still requested "
                             "changes; nothing was published")
    approvers = sorted(login for key, (login, state, commit) in latest.items()
                       if state == "APPROVED" and key in officers and key != author.lower() and commit == head)
    if len(approvers) < required:
        raise IntegrityError(
            f"Pull request #{number} by {author} needs {required} approval{'s' if required > 1 else ''} from "
            f"officers other than its author on its final commit {head[:12]}, but has {len(approvers)}. "
            "Approvals of an earlier commit do not count; nothing was published")
    return number, author, approvers


def run_gate(args, environ, opener):
    git = Git(args.repo)
    folder = gh.check_path(args.ledger, "ledger folder")
    branch = gh.check_branch(args.branch)
    sha = git.resolve(args.sha, "sha")
    before = git.resolve(args.before, "before")
    token = (environ.get("GH_TOKEN") or environ.get("GITHUB_TOKEN") or "").strip()
    if not token:
        raise ToolError("Set GH_TOKEN (the workflow's GITHUB_TOKEN with pull-requests: read) to check approvals")
    github = gh.GitHub(token, args.repository or environ.get("GITHUB_REPOSITORY", ""), opener)
    pushed = git.first_parents(sha, before, MAX_PUSH_COMMITS + 1)
    if len(pushed) > MAX_PUSH_COMMITS:
        raise IntegrityError(f"More than {MAX_PUSH_COMMITS} commits arrived in one push; merge ledger changes "
                             "through pull requests")
    targets = [commit for commit in pushed if git.touches(commit, folder)]
    if not targets:
        raise IntegrityError("The ledger differs from the previous main, but no commit in this push changed it; "
                             "history was rewritten (force push). Nothing was published")
    if len(targets) > MAX_GATED_COMMITS:
        raise IntegrityError(f"{len(targets)} ledger commits arrived in one push; merge each through a pull request")
    if before:
        # The run for the previous ledger change may have been skipped (a newer run replaces a pending one),
        # so its approval is re-checked before its content is published again.
        for commit in git.first_parents(before, None, MAX_HISTORY_WALK):
            if git.touches(commit, folder):
                targets.append(commit)
                break
    lines = ["## Capped DKP approval gate", ""]
    for commit in reversed(targets):
        number, author, approvers = gate_commit(git, github, commit, branch)
        lines.append(f"- {commit[:12]}: pull request #{number} by {md(author)}, approved by "
                     + ", ".join(md(login) for login in approvers))
        say(f"Commit {commit[:12]}: pull request #{number} by {author} approved by {', '.join(approvers)}.")
    write_summary(environ, lines)
    say("Approval gate passed.")


# mirror --------------------------------------------------------------------------------------------

def read_site(folder):
    files = {}
    for name in SITE_FILES:
        path = Path(folder) / name
        try:
            files[name] = snapshot.bounded_read(path, SITE_BYTES)
        except (OSError, ValueError) as error:
            raise ToolError(f"Cannot read {name} from the site folder: {error}") from None
    return files


def history_marker(content, label):
    try:
        document = json.loads(content.decode("utf-8"))
        last, head = document["lastEventId"], document["chainHead"]
        if document.get("format") != publish.HISTORY_FORMAT or isinstance(last, bool) or not isinstance(last, int) \
                or last < 0 or not isinstance(head, str) or not HEX64.fullmatch(head):
            raise ValueError
    except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
        raise IntegrityError(f"{label} is not a CappedDKP history.json; nothing was pushed") from None
    return last, head


def run_mirror(args, environ, opener):
    token = environ.get("CDKP_PUBLIC_REPO_TOKEN", "").strip()
    if not token:
        raise ToolError("Set CDKP_PUBLIC_REPO_TOKEN to a fine-grained token with Contents read/write on the "
                        "public repository only")
    repository = gh.check_repository(args.repository)
    if repository.lower() == environ.get("GITHUB_REPOSITORY", "").strip().lower():
        raise ToolError("The public mirror must be a different repository from the private ledger")
    branch = gh.check_branch(args.branch)
    prefix = gh.check_path(args.path, "mirror path") + "/" if args.path else ""
    ledger = store.load_ledger(args.ledger)
    files = read_site(args.site)
    last, head = history_marker(files["history.json"], "The generated site")
    if last != ledger.count or head != ledger.heads[-1]:
        raise IntegrityError("The generated site does not match the ledger; build it again from this ledger")
    github = gh.GitHub(token, repository, opener)
    tip = github.branch_head(branch)
    if tip is None:
        raise ToolError(f"{repository} has no branch {branch}; create the repository with a README first")
    commit = github.commit(tip)
    current = github.file_at(prefix + "history.json", tip)
    if current is not None:
        shown, shown_head = history_marker(current, f"{prefix}history.json in {repository}")
        if shown > ledger.count:
            raise IntegrityError(f"{repository} already shows event {shown}, newer than this ledger's "
                                 f"{ledger.count}; a later run published first. Nothing was pushed")
        if ledger.heads[shown] != shown_head:
            raise IntegrityError(f"{repository} shows a history that this ledger does not extend; nothing was pushed")
    entries = [{"path": prefix + name, "mode": "100644", "type": "blob", "sha": github.create_blob(content)}
               for name, content in files.items()]
    tree = github.create_tree(entries, commit["tree"])
    if tree == commit["tree"]:
        say(f"{repository} already shows event {ledger.count}; nothing to push.")
        return
    message = f"Publish CappedDKP standings through event {ledger.count}\n\nChain head {ledger.heads[-1]}\n"
    sha = github.create_commit(message, tree, [tip])
    github.move_branch(branch, sha, exists=True, force=False)
    say(f"Mirrored the public site to {repository}@{branch} (event {ledger.count}, commit {sha[:12]}).")


# notify --------------------------------------------------------------------------------------------

def run_link(environ):
    server = environ.get("GITHUB_SERVER_URL", "").strip()
    repository = environ.get("GITHUB_REPOSITORY", "").strip()
    run = environ.get("GITHUB_RUN_ID", "").strip()
    if not SERVER.fullmatch(server) or not run.isdigit() or len(run) > 20:
        return None
    try:
        repository = gh.check_repository(repository)
    except ToolError:
        return None
    return f"{server}/{repository}/actions/runs/{run}"


def run_notify(args, environ, opener):
    url = environ.get("DISCORD_OFFICER_WEBHOOK_URL", "").strip()
    if not url:
        say("Officer notification skipped: DISCORD_OFFICER_WEBHOOK_URL is not set.")
        return
    if not publish.WEBHOOK_PATTERN.fullmatch(url):
        raise ToolError("DISCORD_OFFICER_WEBHOOK_URL is not a Discord webhook URL")
    lines = [NOTIFY_TEXT[args.what]]
    commit = environ.get("GITHUB_SHA", "").strip()
    if gh.SHA_PATTERN.fullmatch(commit):
        lines.append(f"Commit {commit[:12]}")
    link = run_link(environ)
    lines.append(link or "Open the repository's Actions tab for details.")
    publish.post_webhook(opener or publish.default_opener(), url, "\n".join(lines), publish.DEFAULT_USERNAME)
    say("Officers notified.")


# CLI -----------------------------------------------------------------------------------------------

def build_parser():
    root = publish.Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = root.add_subparsers(dest="command", required=True)

    toolkit = commands.add_parser("toolkit", help="check .cdkp against TOOLKIT.json")
    toolkit.add_argument("--root", metavar="DIR", help="toolkit folder (default: the .cdkp folder holding this tool)")

    check = commands.add_parser("check", help="verify a ledger change between two commits")
    check.add_argument("--repo", default=".", metavar="DIR", help="Git repository (default: current folder)")
    check.add_argument("--base", required=True, metavar="REV", help="previous commit; all zeros means none")
    check.add_argument("--head", default="HEAD", metavar="REV")
    check.add_argument("--merge-base", action="store_true", help="compare against the merge base of --base and --head")
    check.add_argument("--ledger", default="ledger", metavar="PATH", help="ledger folder in the repository")
    store.add_replay_arguments(check)

    gate = commands.add_parser("gate", help="require approved pull requests for the ledger commits of a push")
    gate.add_argument("--repo", default=".", metavar="DIR")
    gate.add_argument("--sha", required=True, metavar="SHA", help="the pushed commit (github.sha)")
    gate.add_argument("--before", required=True, metavar="SHA", help="the previous tip (github.event.before)")
    gate.add_argument("--branch", default="main")
    gate.add_argument("--ledger", default="ledger", metavar="PATH")
    gate.add_argument("--repository", metavar="OWNER/NAME", help="default: GITHUB_REPOSITORY")

    mirror = commands.add_parser("mirror", help="push the public site to a separate public repository")
    mirror.add_argument("--site", required=True, metavar="DIR", help="folder written by capped_dkp_publish.py site")
    mirror.add_argument("--ledger", default="ledger", metavar="DIR")
    mirror.add_argument("--repository", required=True, metavar="OWNER/NAME")
    mirror.add_argument("--branch", default="main")
    mirror.add_argument("--path", metavar="PATH", help="folder inside the public repository (default: its root)")

    notify = commands.add_parser("notify", help="post a failure notice to the officers' Discord webhook")
    notify.add_argument("--what", required=True, choices=sorted(NOTIFY_TEXT))
    return root


def secret_values(environ):
    secrets = []
    for name in SECRET_VARIABLES:
        value = environ.get(name, "").strip()
        if value:
            secrets += [value, value.rsplit("/", 1)[-1]]
    return [secret for secret in secrets if len(secret) >= 8]


def fail(message, code, environ):
    message = gh.mask(message, *secret_values(environ))
    publish.say(f"ERROR: {message}", sys.stderr)
    if environ.get("GITHUB_ACTIONS") == "true":
        say("::error title=CappedDKP::" + command_text(message))
    return code


def main(argv=None, opener=None, environ=None):
    environ = os.environ if environ is None else environ
    handlers = {"toolkit": run_toolkit, "check": run_check, "gate": run_gate, "mirror": run_mirror,
                "notify": run_notify}
    try:
        args = build_parser().parse_args(sys.argv[1:] if argv is None else list(argv))
        handlers[args.command](args, environ, opener)
    except ToolError as error:
        return fail(str(error), error.code, environ)
    except OSError as error:
        return fail(f"File operation failed: {type(error).__name__}: {error.strerror or error}", 1, environ)
    return 0


if __name__ == "__main__":
    sys.exit(main())
