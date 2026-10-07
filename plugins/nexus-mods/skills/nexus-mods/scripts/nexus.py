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
import hashlib
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
APP_VERSION = "1.0.1"
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
        with urllib.request.urlopen(req, timeout=60) as r:
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
    m = re.match(r"^/mods/(\d+)/files/(\d+)", p.path)
    if not m:
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
    name = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    out_dir = Path(out) if out else downloads_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / name
    part = target.with_name(target.name + ".part")
    print(f"Downloading {name} from {links[0].get('name', 'Nexus CDN')} -> {out_dir}")
    md5 = hashlib.md5()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=120) as r, open(part, "wb") as fh:
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
        part.unlink(missing_ok=True)
        die(f"download incomplete ({done} of {total} bytes).")
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


def cmd_register_nxm(a):
    winreg = _reg()
    backup = config_dir() / "nxm_handler_backup.json"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\nxm\shell\open\command") as k:
            previous = winreg.QueryValueEx(k, "")[0]
    except OSError:
        previous = None
    if previous and "handle-nxm" not in previous and not backup.exists():
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(json.dumps({"command": previous}), encoding="utf-8")
        print(f"Existing nxm handler saved to {backup}:\n  {previous}")
    cmd = f'"{sys.executable}" "{Path(__file__).resolve()}" handle-nxm "%1"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\nxm") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Nexus Mods Link")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\nxm\shell\open\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, cmd)
    print(f"nxm:// links now open with:\n  {cmd}\nUndo with `unregister-nxm`.")


def cmd_unregister_nxm(a):
    winreg = _reg()
    backup = config_dir() / "nxm_handler_backup.json"
    if backup.exists():
        previous = json.loads(backup.read_text(encoding="utf-8"))["command"]
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\nxm\shell\open\command") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, previous)
        backup.unlink()
        print(f"Restored previous nxm handler:\n  {previous}")
        return
    for sub in (r"Software\Classes\nxm\shell\open\command", r"Software\Classes\nxm\shell\open",
                r"Software\Classes\nxm\shell", r"Software\Classes\nxm"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            pass
    print("nxm handler removed (there was no previous handler to restore).")


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
