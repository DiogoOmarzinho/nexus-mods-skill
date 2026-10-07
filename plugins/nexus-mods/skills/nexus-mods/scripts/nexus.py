#!/usr/bin/env python3
"""Nexus Mods command-line helper (standard library only).

Read-only commands use the public GraphQL API and need no account.
Downloads use the official v1 API with the user's own personal API key,
the same way Vortex / Mod Organizer do:
  - Premium members: direct download link.
  - Free members: the site's "Mod manager download" button hands out an
    nxm:// link carrying a one-time key; this tool turns it into the file.

API key lookup order: env NEXUS_API_KEY, then ~/.config/nexus-mods/apikey
(Windows: %APPDATA%\\nexus-mods\\apikey). The key is never printed.
"""
import argparse
import base64
import hashlib
import http.client
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_NAME = "claude-nexus-mods-skill"
APP_VERSION = "1.0.2"
GQL = "https://api.nexusmods.com/v2/graphql"
V1 = "https://api.nexusmods.com/v1"
UA = f"{APP_NAME}/{APP_VERSION}"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- helpers

def die(msg, code=1):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def config_dir():
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home())) / "nexus-mods"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "nexus-mods"


def api_key(required=True):
    key = os.environ.get("NEXUS_API_KEY", "").strip()
    if not key:
        f = config_dir() / "apikey"
        if f.exists():
            key = f.read_text(encoding="utf-8").strip()
    if not key and required:
        die("no Nexus API key found. The user must create a personal API key at "
            "https://www.nexusmods.com/settings/api-keys and save it themselves in the "
            f"NEXUS_API_KEY environment variable or in {config_dir() / 'apikey'}.")
    return key


# Deliberately process-local: no API key or quota state is persisted. REST and
# GraphQL quotas are not assumed to be shared. Other clients remain invisible.
_RATE_STATE = {}


def reset_epoch(value):
    """Accept epoch seconds, ISO timestamps and HTTP dates from API headers."""
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        date = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        try:
            date = parsedate_to_datetime(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
    if date.tzinfo is None:
        date = date.replace(tzinfo=timezone.utc)
    return date.timestamp()


def quota_exhausted(headers):
    def remaining(name):
        try:
            return int(headers.get("x-rl-" + name + "-remaining"))
        except (TypeError, ValueError):
            return None
    daily, hourly = remaining("daily"), remaining("hourly")
    # Daily exhaustion alone still permits the hourly allowance. With missing
    # headers, conservatively stop if the only known allowance is exhausted.
    return ((daily is not None and daily <= 0 and (hourly is None or hourly <= 0))
            or (hourly is not None and hourly <= 0 and daily is None))


def observe_limits(scope, headers, throttled=False):
    h = {k.lower(): v for k, v in headers.items()}
    if not throttled and not quota_exhausted(h):
        return
    now = time.time()
    retry = h.get("retry-after")
    until = None
    if retry is not None:
        try:
            until = now + max(0, int(retry))
        except ValueError:
            until = reset_epoch(retry)
    if until is None:
        resets = [reset_epoch(h.get("x-rl-" + period + "-reset"))
                  for period in ("hourly", "daily")]
        future = [r for r in resets if r is not None and r > now]
        until = min(future) if future else now + 3600
    _RATE_STATE[scope] = max(now + 1, until)


def check_limits(scope):
    until = _RATE_STATE.get(scope, 0)
    if until > time.time():
        when = datetime.fromtimestamp(until, timezone.utc).isoformat()
        die(f"Nexus API requests paused until {when}; quota exhausted or HTTP 429. "
            "No automatic retry. This guard applies only to this process.")


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        destination = urllib.parse.urlsplit(newurl)
        authenticated = any(k.lower() == "apikey" for k, _ in req.header_items())
        if authenticated or destination.scheme != "https" or destination.username or destination.password:
            raise urllib.error.HTTPError(req.full_url, code, "Redirect refused", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open_url(req, timeout):
    return urllib.request.build_opener(SafeRedirect()).open(req, timeout=timeout)


def http_json(url, data=None, headers=None, method=None):
    scope = "rest" if url.startswith(V1 + "/") else "graphql"
    check_limits(scope)
    h = {"User-Agent": UA, "Accept": "application/json",
         "Application-Name": APP_NAME, "Application-Version": APP_VERSION}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with _open_url(req, timeout=60) as r:
            observe_limits(scope, r.headers)
            limits = {k: v for k, v in r.headers.items() if k.lower().startswith("x-rl-")}
            return json.load(r), limits
    except urllib.error.HTTPError as e:
        # Do not print remote bodies or request URLs: they may echo credentials
        # or contain temporary nxm download tokens in their query strings.
        observe_limits(scope, e.headers or {}, throttled=e.code == 429)
        e.close()
        if e.code == 401:
            die("401 unauthorized from Nexus. The API key is missing, wrong or revoked.")
        if e.code == 403:
            die("403 forbidden from Nexus. For free accounts a download needs a fresh "
                "nxm:// link from the site's 'Mod manager download' button.")
        if e.code == 429:
            check_limits(scope)
        die(f"HTTP {e.code} from Nexus API. No automatic retry.")
    except urllib.error.URLError:
        die("network error talking to Nexus API. No automatic retry.")


def gql(query, variables=None):
    res, _ = http_json(GQL, {"query": query, "variables": variables or {}})
    if res.get("errors"):
        die("GraphQL: " + " | ".join(e.get("message", "?") for e in res["errors"]))
    return res["data"]


def v1(path, params=None):
    url = V1 + path + (("?" + urllib.parse.urlencode(params)) if params else "")
    return http_json(url, headers={"apikey": api_key()})


def fmt_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def fmt_date(v):
    if v is None:
        return "?"
    if isinstance(v, (int, float)) or str(v).isdigit():
        return time.strftime("%Y-%m-%d", time.gmtime(int(v)))
    return str(v)[:10]


def resolve_game(game):
    """Accept a domain name (skyrimspecialedition) or a numeric id; return (id, domain, name)."""
    if str(game).isdigit():
        g = gql("query($id:ID){ game(id:$id){ id name domainName } }", {"id": str(game)})["game"]
    else:
        g = gql("query($d:String){ game(domainName:$d){ id name domainName } }", {"d": game})["game"]
    if not g:
        die(f"game '{game}' not found on Nexus. Use `games <name>` to find its domain name.")
    return int(g["id"]), g["domainName"], g["name"]


def print_table(rows, cols):
    if not rows:
        print("(nothing found)")
        return
    widths = [max(len(str(c)), *(len(str(r.get(c, ""))) for r in rows)) for c in cols]
    widths = [min(w, 70) for w in widths]
    print("  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  ".join(str(r.get(c, ""))[:w].ljust(w) for c, w in zip(cols, widths)))


# ---------------------------------------------------------------- commands

def cmd_games(a):
    d = gql("query($f:GamesSearchFilter){ games(filter:$f, count:25){ nodes{ id name domainName } } }",
            {"f": {"name": [{"value": a.name, "op": "WILDCARD"}]}})
    print_table(d["games"]["nodes"], ["id", "domainName", "name"])


def cmd_search(a):
    gid, domain, gname = resolve_game(a.game)
    f = {"gameDomainName": [{"value": domain, "op": "EQUALS"}]}
    if a.term:
        f["name"] = [{"value": a.term, "op": "WILDCARD"}]
    if a.category:
        f["categoryName"] = [{"value": a.category, "op": "EQUALS"}]
    if a.author:
        f["author"] = [{"value": a.author, "op": "WILDCARD"}]
    if a.updated_after:
        # the API wants a unix timestamp as a string
        ts = int(time.mktime(time.strptime(a.updated_after, "%Y-%m-%d")))
        f["updatedAt"] = [{"value": str(ts), "op": "GTE"}]
    if not a.adult:
        f["adultContent"] = [{"value": False, "op": "EQUALS"}]
    q = """query($f:ModsFilter, $s:[ModsSort!], $n:Int, $o:Int){ mods(filter:$f, sort:$s, count:$n, offset:$o){
             totalCount nodes{ modId name summary endorsements downloads updatedAt adultContent category version author } } }"""
    d = gql(q, {"f": f, "s": [{a.sort: {"direction": "DESC"}}], "n": a.count, "o": a.offset})["mods"]
    print(f"{gname} ({domain}) - {d['totalCount']} matches, showing {len(d['nodes'])} sorted by {a.sort}"
          + ("" if a.adult else " (adult content hidden; --adult to include)"))
    rows = [{"id": m["modId"], "endorse": m["endorsements"], "updated": fmt_date(m["updatedAt"]),
             "18+": "x" if m["adultContent"] else "", "category": m["category"], "name": m["name"],
             "summary": (m["summary"] or "").replace("\n", " ")} for m in d["nodes"]]
    print_table(rows, ["id", "endorse", "updated", "18+", "category", "name", "summary"])


def cmd_mod(a):
    gid, domain, gname = resolve_game(a.game)
    q = """query($m:ID!,$g:ID!){ mod(modId:$m, gameId:$g){ modId name version summary description author
             uploader{ name } category endorsements downloads createdAt updatedAt adultContent status
             modRequirements{ nexusRequirements{ nodes{ modId modName externalRequirement url notes } }
                              dlcRequirements{ gameExpansion{ name } notes } } } }"""
    m = gql(q, {"m": str(a.mod_id), "g": str(gid)})["mod"]
    if not m:
        die(f"mod {a.mod_id} not found for {domain} (it may be hidden, deleted or adult-only).")
    print(f"{m['name']}  v{m['version']}  [{m['category']}]  {'ADULT' if m['adultContent'] else ''}")
    print(f"https://www.nexusmods.com/{domain}/mods/{m['modId']}")
    print(f"by {m['author']} (uploaded by {(m.get('uploader') or {}).get('name')}) | "
          f"{m['endorsements']} endorsements | {m['downloads']} downloads | "
          f"created {fmt_date(m['createdAt'])} | updated {fmt_date(m['updatedAt'])} | status {m['status']}")
    print(f"\nSummary: {m['summary']}")
    reqs = ((m.get("modRequirements") or {}).get("nexusRequirements") or {}).get("nodes") or []
    dlc = (m.get("modRequirements") or {}).get("dlcRequirements") or []
    if reqs or dlc:
        print("\nRequirements:")
        for r in reqs:
            where = r["url"] if r["externalRequirement"] else f"Nexus mod {r['modId']}"
            print(f"  - {r['modName']} ({where}){': ' + r['notes'] if r.get('notes') else ''}")
        for r in dlc:
            print(f"  - DLC {((r.get('gameExpansion') or {}).get('name'))}{': ' + r['notes'] if r.get('notes') else ''}")
    if a.description:
        text = re.sub(r"<br\s*/?>", "\n", m["description"] or "")
        text = re.sub(r"<[^>]+>|\[/?[a-z0-9=#_ ]+\]", " ", text, flags=re.I)
        print("\nDescription:\n" + re.sub(r"[ \t]+", " ", text).strip()[:6000])
    print()
    cmd_files(a, gid=gid, domain=domain)


def cmd_files(a, gid=None, domain=None):
    if gid is None:
        gid, domain, _ = resolve_game(a.game)
    files = gql("""query($m:ID!,$g:ID!){ modFiles(modId:$m, gameId:$g){ fileId name version category
                     sizeInBytes date description } }""", {"m": str(a.mod_id), "g": str(gid)})["modFiles"]
    order = {"MAIN": 0, "UPDATE": 1, "OPTIONAL": 2, "MISCELLANEOUS": 3, "OLD_VERSION": 4, "ARCHIVED": 5}
    if not getattr(a, "all_files", False):
        files = [f for f in files if f["category"] not in ("OLD_VERSION", "ARCHIVED", "DELETED")]
    files.sort(key=lambda f: (order.get(f["category"], 9), -int(f["date"] or 0)))
    rows = [{"file_id": f["fileId"], "category": f["category"], "version": f["version"],
             "size": fmt_size(f["sizeInBytes"]), "date": fmt_date(f["date"]), "name": f["name"],
             "description": re.sub(r"<[^>]+>|\s+", " ", f["description"] or "").strip()} for f in files]
    print("Files" + ("" if getattr(a, "all_files", False) else " (old versions hidden; --all-files to show)") + ":")
    print_table(rows, ["file_id", "category", "version", "size", "date", "name", "description"])
    print(f"\nPage to download from: https://www.nexusmods.com/{domain}/mods/{a.mod_id}?tab=files")


def cmd_whoami(a):
    u, limits = v1("/users/validate.json")
    print(f"Logged in as {u.get('name')} (user id {u.get('user_id')}) | premium: {u.get('is_premium')} | "
          f"supporter: {u.get('is_supporter')}")
    if limits:
        print("Rate limits: " + ", ".join(f"{k}={v}" for k, v in limits.items()))
    print("Downloads: " + ("direct (premium)" if u.get("is_premium") else
                           "need an nxm:// link from the 'Mod manager download' button (free account)"))


def parse_nxm(url):
    p = urllib.parse.urlparse(url)
    if p.scheme != "nxm":
        die("not an nxm:// link")
    m = re.fullmatch(r"/mods/(\d+)/files/(\d+)", p.path)
    if not m or not re.fullmatch(r"[a-zA-Z0-9_-]+", p.netloc):
        die("unrecognised nxm link (collections are not supported)")
    qs = urllib.parse.parse_qs(p.query)
    return {"game": p.netloc, "mod_id": int(m.group(1)), "file_id": int(m.group(2)),
            "key": (qs.get("key") or [None])[0], "expires": (qs.get("expires") or [None])[0]}


def downloads_dir():
    d = Path.home() / "Downloads"
    return d if d.exists() else Path.cwd()


def do_download(game, mod_id, file_id, key=None, expires=None, out=None):
    params = {"key": key, "expires": expires} if key and expires else None
    links, _ = v1(f"/games/{game}/mods/{mod_id}/files/{file_id}/download_link.json", params)
    if not links:
        die("Nexus returned no download mirrors.")
    url = links[0]["URI"]
    destination = urllib.parse.urlsplit(url)
    if destination.scheme != "https" or not destination.hostname or destination.username or destination.password:
        die("Nexus returned an unsupported download URL; HTTPS without embedded credentials is required.")
    name = urllib.parse.unquote(destination.path.rsplit("/", 1)[-1])
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{n}" for p in ("COM", "LPT") for n in range(1, 10)}
    if (not name or name in (".", "..") or any(c in name for c in '/\\<>:"|?*')
            or any(ord(c) < 32 or ord(c) == 127 for c in name) or name.endswith((".", " "))
            or name.split(".")[0].upper() in reserved):
        die("Nexus returned an unsafe download filename; no file written.")
    out_dir = Path(out) if out else downloads_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / name
    part = target.with_name(target.name + ".part")
    print(f"Downloading {name} -> {out_dir}")
    md5 = hashlib.md5()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    created_part = False
    try:
        # Exclusive creation never follows or truncates a pre-existing .part file.
        with open(part, "xb") as fh:
            created_part = True
            with _open_url(req, timeout=120) as r:
                total = int(r.headers.get("Content-Length") or 0)
                done, last = 0, 0
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    fh.write(chunk)
                    md5.update(chunk)
                    done += len(chunk)
                    if time.time() - last > 1:
                        last = time.time()
                        pct = f"{done * 100 // total}%" if total else fmt_size(done)
                        print(f"  {pct} ({fmt_size(done)} / {fmt_size(total)})", flush=True)
        if total and done != total:
            raise OSError("incomplete transfer")
    except (OSError, ValueError, http.client.HTTPException) as error:
        if isinstance(error, urllib.error.HTTPError):
            error.close()
        if created_part:
            part.unlink(missing_ok=True)
        die("Download failed or incomplete; no completed file saved. No automatic retry. "
            "Check connectivity and any existing .part file before trying again.")
    part.replace(target)
    print(f"Saved: {target}\nSize: {fmt_size(done)}\nMD5:  {md5.hexdigest()}")
    log = config_dir() / "downloads.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "game": game, "mod_id": mod_id,
                             "file_id": file_id, "path": str(target), "md5": md5.hexdigest()}) + "\n")
    return target


def cmd_download(a):
    if a.nxm:
        n = parse_nxm(a.nxm)
        do_download(n["game"], n["mod_id"], n["file_id"], n["key"], n["expires"], a.out)
        return
    if not (a.game and a.mod_id and a.file_id):
        die("give either --nxm <link> or GAME MOD_ID FILE_ID.")
    _, domain, _ = resolve_game(a.game)
    u, _ = v1("/users/validate.json")
    if not u.get("is_premium"):
        die("this account is not Premium, so Nexus only allows the download through the site's "
            f"'Mod manager download' button: https://www.nexusmods.com/{domain}/mods/{a.mod_id}?tab=files&file_id={a.file_id}&nmm=1 "
            "- with the nxm handler registered (`register-nxm`) the file then downloads automatically; "
            "otherwise copy the nxm:// link and run `download --nxm <link>`.")
    do_download(domain, a.mod_id, a.file_id, out=a.out)


def cmd_handle_nxm(a):
    """Entry point for the registered nxm:// protocol handler."""
    try:
        n = parse_nxm(a.url)
        print(f"nxm link: {n['game']} mod {n['mod_id']} file {n['file_id']}")
        do_download(n["game"], n["mod_id"], n["file_id"], n["key"], n["expires"], a.out)
        time.sleep(3)
    except SystemExit:
        input("\nPress Enter to close...")
        raise


def cmd_md5(a):
    _, domain, _ = resolve_game(a.game)
    h = hashlib.md5()
    with open(a.file, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    print(f"MD5 {digest}")
    res, _ = v1(f"/games/{domain}/mods/md5_search/{digest}.json")
    for r in res:
        m, f = r.get("mod", {}), r.get("file_details", {})
        print(f"  mod {m.get('mod_id')} '{m.get('name')}' v{m.get('version')} | file {f.get('file_id')} "
              f"'{f.get('name')}' v{f.get('version')} ({f.get('category_name')})")


def cmd_updated(a):
    _, domain, _ = resolve_game(a.game)
    res, _ = v1(f"/games/{domain}/mods/updated.json", {"period": a.period})
    ids = set(int(x) for x in a.mod_ids) if a.mod_ids else None
    rows = [{"mod_id": r["mod_id"], "latest_file_update": fmt_date(r["latest_file_update"]),
             "latest_mod_activity": fmt_date(r["latest_mod_activity"])}
            for r in res if ids is None or r["mod_id"] in ids]
    print(f"Mods updated in the last {a.period} for {domain}" + (" (filtered)" if ids else "") + ":")
    print_table(rows, ["mod_id", "latest_file_update", "latest_mod_activity"])


# ------------------------------------------------ nxm handler (Windows)

def _reg():
    if os.name != "nt":
        die("register-nxm is Windows-only. On Linux, create a .desktop file with "
            "MimeType=x-scheme-handler/nxm; that runs `nexus.py handle-nxm %u`.")
    import winreg
    return winreg


NXM_ROOT = r"Software\Classes\nxm"
NXM_KEYS = [NXM_ROOT, NXM_ROOT + r"\shell", NXM_ROOT + r"\shell\open",
            NXM_ROOT + r"\shell\open\command"]
NXM_SLOTS = [(NXM_ROOT, ""), (NXM_ROOT, "URL Protocol"), (NXM_KEYS[-1], "")]


def _nxm_command():
    return f'"{sys.executable}" "{Path(__file__).resolve()}" handle-nxm "%1"'


def _nxm_read(reg, path, name):
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, path) as key:
            value, kind = reg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    return {"type": kind, "value": (base64.b64encode(value).decode("ascii")
            if isinstance(value, bytes) else value), "binary": isinstance(value, bytes)}


def _nxm_state(reg):
    return [_nxm_read(reg, path, name) for path, name in NXM_SLOTS]


def _nxm_write(reg, slot, value):
    path, name = NXM_SLOTS[slot]
    if value is None:
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_SET_VALUE) as key:
                reg.DeleteValue(key, name)
        except FileNotFoundError:
            pass
    else:
        data = base64.b64decode(value["value"], validate=True) if value["binary"] else value["value"]
        with reg.CreateKey(reg.HKEY_CURRENT_USER, path) as key:
            reg.SetValueEx(key, name, 0, value["type"], data)


def _nxm_transition(reg, expected, target):
    # Guard every value we own; never treat a substring in a command as ownership.
    if _nxm_state(reg) != expected:
        die("nxm association changed; refusing to overwrite another application's settings. Backup kept.")
    applied = []
    try:
        for i, value in enumerate(target):
            if value != expected[i]:
                # Recheck immediately before each write (registry updates are not atomic).
                if _nxm_state(reg) != [target[j] if j in applied else expected[j] for j in range(3)]:
                    raise OSError("association changed during update")
                _nxm_write(reg, i, value)
                applied.append(i)
    except OSError:
        # Best-effort rollback only while the managed values still match our writes.
        # If another app intervenes, preserve it and leave the backup for review.
        for i in reversed(applied):
            if _nxm_state(reg) != [target[j] if j in applied else expected[j] for j in range(3)]:
                break
            try:
                _nxm_write(reg, i, expected[i])
                applied.remove(i)
            except OSError:
                break
        raise


def _nxm_prune(reg, created):
    for path in reversed(created):
        try:
            with reg.OpenKey(reg.HKEY_CURRENT_USER, path) as key:
                subkeys, values, _ = reg.QueryInfoKey(key)
            if not subkeys and not values:
                reg.DeleteKey(reg.HKEY_CURRENT_USER, path)
        except FileNotFoundError:
            pass


def _nxm_validate_value(value):
    if value is None:
        return
    kind, data, binary = value["type"], value["value"], value["binary"]
    if type(kind) is not int or type(binary) is not bool:
        raise ValueError("invalid registry type")
    if binary:
        if kind not in (0, 3, 6, 8, 9, 10):
            raise ValueError("invalid binary type")
        base64.b64decode(data, validate=True)
    elif kind in (1, 2) and isinstance(data, str):
        pass
    elif kind == 7 and isinstance(data, list) and all(isinstance(v, str) for v in data):
        pass
    elif kind in (4, 5, 11) and type(data) is int and 0 <= data < 2 ** (64 if kind == 11 else 32):
        pass
    else:
        raise ValueError("unsupported registry value")


def _nxm_load(backup):
    try:
        record = json.loads(backup.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("schema") != 2:
            raise ValueError("legacy backup")
        for field in ("before", "installed"):
            if not isinstance(record[field], list) or len(record[field]) != 3:
                raise ValueError("invalid state")
            for value in record[field]:
                _nxm_validate_value(value)
        if record["created"] != [p for p in NXM_KEYS if p in record["created"]]:
            raise ValueError("invalid key paths")
        if not isinstance(record["command"], str) or record["installed"] != [
                {"type": 1, "value": v, "binary": False}
                for v in ("URL:Nexus Mods Link", "", record["command"])]:
            raise ValueError("invalid ownership record")
        return record
    except (OSError, ValueError, KeyError, TypeError):
        die("Invalid or legacy nxm backup. No registry changes made; keep the backup for manual recovery.")


def cmd_register_nxm(a):
    reg = _reg()
    backup = config_dir() / "nxm_handler_backup.json"
    try:
        if backup.exists():
            record = _nxm_load(backup)
            if record["command"] == _nxm_command() and _nxm_state(reg) == record["installed"]:
                print("nxm handler already registered; original backup preserved.")
                return
            die("Existing nxm backup or changed ownership; unregister/review it before registering again. No changes made.")
        before = _nxm_state(reg)
        try:
            for value in before:
                _nxm_validate_value(value)
        except (ValueError, TypeError, KeyError):
            die("Unsupported existing registry values; no changes made.")
        if before[-1] and before[-1]["value"] == _nxm_command():
            die("Existing handler has no recovery backup; refusing to replace its original state.")
        created = []
        for path in NXM_KEYS:
            try:
                with reg.OpenKey(reg.HKEY_CURRENT_USER, path):
                    pass
            except FileNotFoundError:
                created.append(path)
        installed = [{"type": reg.REG_SZ, "value": v, "binary": False}
                     for v in ("URL:Nexus Mods Link", "", _nxm_command())]
        record = {"schema": 2, "command": _nxm_command(), "before": before,
                  "installed": installed, "created": created}
        backup.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents replacing another run's original snapshot.
        with backup.open("x", encoding="utf-8") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            _nxm_transition(reg, before, installed)
        except OSError:
            if _nxm_state(reg) == before:
                _nxm_prune(reg, created)
                backup.unlink()
            raise
        print("nxm handler registered. Previous values/types preserved; undo with unregister-nxm.")
    except OSError:
        die("Could not register nxm handler. No further writes attempted; inspect any retained recovery backup.")


def cmd_unregister_nxm(a):
    reg = _reg()
    backup = config_dir() / "nxm_handler_backup.json"
    if not backup.exists():
        die("No nxm recovery backup; refusing to remove an unverified association.")
    record = _nxm_load(backup)
    try:
        current = _nxm_state(reg)
        if current == record["before"]:
            # A prior restore completed but cleanup failed; no association writes.
            _nxm_prune(reg, record["created"])
        else:
            _nxm_transition(reg, record["installed"], record["before"])
            _nxm_prune(reg, record["created"])
        backup.unlink()
        print("Previous nxm values restored; unrelated metadata and subkeys preserved.")
    except OSError:
        die("Could not restore nxm handler. Recovery backup kept; inspect before retrying.")


# ---------------------------------------------------------------- main

def main():
    p = argparse.ArgumentParser(description="Nexus Mods helper")
    sp = p.add_subparsers(dest="cmd", required=True)

    s = sp.add_parser("games", help="find a game's domain name");  s.add_argument("name"); s.set_defaults(fn=cmd_games)

    s = sp.add_parser("search", help="search mods of a game")
    s.add_argument("game"); s.add_argument("term", nargs="?")
    s.add_argument("--category"); s.add_argument("--author")
    s.add_argument("--updated-after", help="YYYY-MM-DD")
    s.add_argument("--sort", default="endorsements",
                   choices=["endorsements", "downloads", "uniqueDownloads", "updatedAt", "createdAt", "relevance", "name"])
    s.add_argument("--count", type=int, default=25); s.add_argument("--offset", type=int, default=0)
    s.add_argument("--adult", action="store_true", help="include adult content")
    s.set_defaults(fn=cmd_search)

    s = sp.add_parser("mod", help="details, requirements and files of a mod")
    s.add_argument("game"); s.add_argument("mod_id", type=int)
    s.add_argument("--description", action="store_true"); s.add_argument("--all-files", action="store_true")
    s.set_defaults(fn=cmd_mod)

    s = sp.add_parser("files", help="list a mod's files")
    s.add_argument("game"); s.add_argument("mod_id", type=int); s.add_argument("--all-files", action="store_true")
    s.set_defaults(fn=cmd_files)

    s = sp.add_parser("whoami", help="check the API key, premium status and rate limits"); s.set_defaults(fn=cmd_whoami)

    s = sp.add_parser("download", help="download a file (premium: by id; free: --nxm link)")
    s.add_argument("game", nargs="?"); s.add_argument("mod_id", nargs="?", type=int); s.add_argument("file_id", nargs="?", type=int)
    s.add_argument("--nxm"); s.add_argument("--out")
    s.set_defaults(fn=cmd_download)

    s = sp.add_parser("handle-nxm", help="(used by the protocol handler)")
    s.add_argument("url"); s.add_argument("--out"); s.set_defaults(fn=cmd_handle_nxm)

    s = sp.add_parser("md5", help="identify a local archive on Nexus by its MD5")
    s.add_argument("game"); s.add_argument("file"); s.set_defaults(fn=cmd_md5)

    s = sp.add_parser("updated", help="mods updated recently (optionally only the given ids)")
    s.add_argument("game"); s.add_argument("mod_ids", nargs="*")
    s.add_argument("--period", default="1w", choices=["1d", "1w", "1m"]); s.set_defaults(fn=cmd_updated)

    s = sp.add_parser("register-nxm", help="Windows: make nxm:// links download through this tool"); s.set_defaults(fn=cmd_register_nxm)
    s = sp.add_parser("unregister-nxm", help="Windows: undo register-nxm"); s.set_defaults(fn=cmd_unregister_nxm)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
