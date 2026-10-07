# Nexus Mods — a Claude skill

A [Claude Code](https://claude.com/claude-code) skill that lets Claude **search Nexus Mods for any game and download mods the official way**: through the Nexus API with your own API key, the same way Vortex and Mod Organizer do. No page scraping, no browser automation, no captchas.

Ask things like:

- *"find me the most endorsed UI mods for Skyrim SE updated this year"*
- *"what does mod 4720 need to work?"*
- *"is there a newer version of the mods I have installed?"*
- *"download the main file of this mod"* (paste a Nexus link)
- *"which mod is this zip from?"*

## What it does

| | No account needed | Needs your API key |
|---|---|---|
| Find a game's Nexus name | ✅ | |
| Search mods by name, category, author or update date; sort by endorsements, downloads or date; adult content hidden unless asked | ✅ | |
| Mod details: version, author, dates, **requirements** (other mods, tools, DLC), files with sizes | ✅ | |
| Download files | | ✅ |
| Identify a local archive by MD5 | | ✅ |
| Check which mods were updated recently | | ✅ |

**How downloads work:**
- **Premium members** can download directly by mod and file id.
- **Free members** click the site's "Mod manager download" button, as Nexus requires. The skill can register itself as the `nxm://` link handler on Windows, so the file then downloads automatically. The previous handler (Vortex, MO2…) is backed up and restored with one command.

It's a single Python script using only the standard library. No pip install.

## Install

In Claude Code:

```
/plugin marketplace add DiogoOmarzinho/nexus-mods-skill
/plugin install nexus-mods@nexus-mods-skill
```

Or copy `plugins/nexus-mods/skills/nexus-mods` into `~/.claude/skills/`.

Requires Python 3.8+.

### Your API key (authenticated commands; testing/personal use only)

This is an unregistered testing prototype. Personal API keys are for development testing with a select group of testers or personal use only. They are not a substitute for registered application keys in a public-facing release. Support has not approved registration or confirmed the authentication approach for this build. See [evaluation instructions](EVALUATION.md).

1. Open https://www.nexusmods.com/settings/api-keys and copy the **Personal API Key** at the bottom of the page.
2. Save it yourself. Don't paste it into the chat. Use either:
   - an environment variable: `setx NEXUS_API_KEY "your-key"` (Windows) or `export NEXUS_API_KEY=...` (Linux/macOS), or
   - a file named `apikey` in `%APPDATA%\nexus-mods\` (Windows) or `~/.config/nexus-mods/` (Linux/macOS).
3. Ask Claude to run `whoami` to check it.

## Safety and etiquette

- Claude asks before every download and shows the file name, page and size.
- Your key is read from your own environment or file and never printed. Claude is told never to ask for it in chat.
- Read and download only: no endorsing, commenting or tracking on your account.
- Reads API quota headers and stops on HTTP 429 without automatic retries. Preventive blocking lasts only within one process; separate CLI runs and other clients are not coordinated. See [limitations](EVALUATION.md).
- Mod authors live on downloads and endorsements. If you like a mod, endorse it on Nexus.

## Using the script directly

```
python plugins/nexus-mods/skills/nexus-mods/scripts/nexus.py --help
python .../nexus.py search skyrimspecialedition --category "User Interface" --count 10
python .../nexus.py mod baldursgate3 141
python .../nexus.py download --nxm "nxm://..."
```

This is an unofficial community project, not affiliated with Nexus Mods or Anthropic. It uses Nexus's public APIs as documented for mod managers.

## License

MIT
