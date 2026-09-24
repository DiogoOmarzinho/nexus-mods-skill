---
name: nexus-mods
description: Search Nexus Mods for any game, inspect mods (versions, files, requirements, update dates, adult flag) and download files the official way — through the Nexus API with the user's own API key, like Vortex/Mod Organizer do — instead of scraping the website or clicking through it with a browser. Use whenever the user mentions Nexus Mods or nexusmods.com, pastes a Nexus link or nxm:// link, asks to find/recommend/compare mods for a game, asks what a mod needs or whether it was updated, wants to download a mod, or asks which mod a local archive came from.
---

# Nexus Mods

Everything goes through `scripts/nexus.py` (Python 3, standard library only). Run it as `python <skill-dir>/scripts/nexus.py <command> ...`.

Why a script instead of the website: Nexus pages block plain HTTP fetchers (403), browsing them costs a lot of tokens, and clicking "Slow download" through a browser is automation the site doesn't welcome. The APIs are public, fast and sanctioned. Looking things up needs no account at all.

## Looking things up (no account needed)

| Goal | Command |
|---|---|
| Find a game's domain name | `games "baldur"` → `baldursgate3` |
| Search mods | `search <game> [term] [--category "Weapons"] [--author X] [--updated-after 2026-08-14] [--sort endorsements\|downloads\|updatedAt\|createdAt] [--count 25] [--offset 0] [--adult]` |
| Everything about one mod | `mod <game> <mod_id> [--description] [--all-files]` — version, author, dates, requirements (other mods, external tools, DLC), current files with ids and sizes |
| Just the files | `files <game> <mod_id> [--all-files]` |

`<game>` is the domain name from the URL (`nexusmods.com/<game>/mods/<id>`) or the numeric game id.

Pointers for good answers:
- Adult mods are hidden by default. Add `--adult` only when the user wants them, and label them when you show them.
- When the user's game was patched recently, compare each mod's `updated` date with the patch date and say which mods may be outdated. Game updates break mods all the time.
- Always read the requirements from `mod` before recommending or downloading: a mod that needs a script extender, a framework or another mod won't work alone, and the user should hear that up front.
- Category names are per game. If `--category` returns nothing, run a plain `search` and read the `category` column to find the exact spelling.
- The API returns a mod's own summary. Paraphrase it for the user; don't pad it.

## Downloading, the official way

Downloads need the user's **personal API key**. The user creates it at https://www.nexusmods.com/settings/api-keys (the "Personal API Key" at the bottom of the page) and stores it **themselves**, in either:
- the environment variable `NEXUS_API_KEY`, or
- a file named `apikey` in `%APPDATA%\nexus-mods\` (Windows) or `~/.config/nexus-mods/` (Linux/macOS).

Never ask the user to paste the key into the chat, and never type it anywhere yourself. It is a password-equivalent. The script reads it and never prints it. `whoami` checks that it works and shows whether the account is Premium.

Before any download, tell the user the file name, the mod page and the size (from `files`), and get a yes.

**Premium accounts:** `download <game> <mod_id> <file_id> [--out DIR]`. The file goes to the Downloads folder by default.

**Free accounts:** Nexus only lets free members download through the website's **"Mod manager download"** button, so that a person clicks it and sees the page. The button produces an `nxm://…?key=…&expires=…` link that is valid for a short time. Two ways to use it:
1. **Handler (smoothest, Windows):** `register-nxm` makes this script the program that opens nxm:// links. After that, the user clicks "Mod manager download" → "Slow download" on the site and the file lands in Downloads by itself. This changes a per-user Windows setting and replaces the current nxm handler (for example Vortex or Mod Organizer), so explain that and ask first. The previous handler is saved, and `unregister-nxm` restores it. On Linux, point a `x-scheme-handler/nxm` .desktop entry at `nexus.py handle-nxm %u` instead.
2. **Manual:** the user copies the nxm:// link (for example from the browser's "open with" prompt) and gives it to you; run `download --nxm "<link>"`.

Give the user the exact button link: `https://www.nexusmods.com/<game>/mods/<mod_id>?tab=files&file_id=<file_id>&nmm=1`.

Every download is logged, with its MD5, in `downloads.log` in the same config folder as the key.

## Other commands (need the key)

- `md5 <game> <archive>`: identifies which mod and file a local archive is, useful for "what is this zip?" and for checking a download is intact.
- `updated <game> [mod_id ...] [--period 1d|1w|1m]`: which mods changed recently. Pass the ids of the user's installed mods to check them for updates.

## Limits and etiquette

- Personal keys are rate limited (about 2,500 requests per day, then 100 per hour). `whoami` shows what is left. Don't loop over hundreds of mods; searches that return many results in one call are cheap.
- Collections (`nxm://…/collections/…`) aren't supported. Point the user to Vortex for those.
- Don't endorse, comment, track or do anything else on the user's account. This skill is for reading and downloading only.
- Nexus supports mod authors through downloads and endorsements. When you download a mod, remind the user they can endorse it on the site if they like it.
- Installing the downloaded file depends on the game (Bethesda games, RE Engine games, Cyberpunk and others all differ). If the user has a game-specific skill or a mod manager, hand off to it. Otherwise, read the mod's description (`mod --description`) for its install instructions.
