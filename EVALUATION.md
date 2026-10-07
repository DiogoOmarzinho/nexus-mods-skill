# Support evaluation build 1.0.1

Local testing prototype for ticket 266840, reviewed on 2026-10-07. Registration is
not approved, and support's confirmation of REST/personal-key evaluation versus
OAuth/PKCE is still pending. No OAuth client or persistent credentials are installed.
This document is a test recipe, not a declaration of full policy compliance.

## Run from source on Linux

Requires Python 3.8+ and only its standard library. No plugin installation is
needed to evaluate the CLI. From the repository root:

```bash
python3 -m unittest discover -s tests -v
python3 plugins/nexus-mods/skills/nexus-mods/scripts/nexus.py --help
```

Tests use only mocks and temporary directories. They never load a real API key,
contact Nexus, register a handler or download a real mod. The fake credential
strings in the tests are deliberately not usable credentials.

Optional public checks (six API calls total, no API key lookup or authentication):

```bash
python3 plugins/nexus-mods/skills/nexus-mods/scripts/nexus.py games skyrim
python3 plugins/nexus-mods/skills/nexus-mods/scripts/nexus.py search skyrimspecialedition SkyUI --count 1
python3 plugins/nexus-mods/skills/nexus-mods/scripts/nexus.py mod skyrimspecialedition 12604
```

Run once, not in a polling loop. Results and counts can change. If throttled,
respect the indicated UTC time before making a new invocation.

Authenticated evaluation is deferred pending support's reply. The mocked suite
covers REST identity/auth headers, Premium dispatch, rejection of direct downloads
for a free account, temporary nxm parameters, simulated file delivery and safe log
contents. Actual `whoami`, `updated`, `md5`, download entitlement checks, signed
links, CDN delivery and Windows handler behavior still need controlled evaluation
later. No personal key should be sent in chat, tickets, logs or test artifacts.
Personal keys are only suitable for selected development testers or personal use;
a broader public release requires registration and its agreed authentication flow.

## Changes and limits

- Stable application name `claude-nexus-mods-skill`; version `1.0.1` agrees with
  the plugin manifest. REST and GraphQL both send application name/version.
- GraphQL now uses the documented `https://api.nexusmods.com/v2/graphql`.
  Before changing it, an identical public `game(domainName:...)` query returned
  HTTP 200 and the same Skyrim Special Edition record (id 1704) on both this URL
  and `https://api-router.nexusmods.com/graphql`. The old endpoint is not known
  to be broken. No fallback request is added.
- Current published REST allowance: 20,000/day, then 500/hour. The response
  headers are used instead of a locally invented daily request counter.
- Exhausted reported allowances stop subsequent requests within the same process.
  Daily exhaustion with positive hourly allowance remains usable. A lone exhausted
  allowance is handled conservatively. HTTP 429 always stops; no automatic retry
  or background task is created. Retry-After (seconds or HTTP date) takes precedence,
  followed by future quota reset timestamps, then a one-hour fallback. The fallback
  is a local precaution, not a server guarantee of availability.
- REST and GraphQL quota state are separate. No GraphQL numeric allowance is
  assumed. Missing/malformed quota headers cannot provide preventive protection;
  HTTP 429 is still handled. State is memory-only and cannot coordinate independent
  CLI processes, keys, devices or other API clients. Do not automate repeated runs.
- HTTP API errors omit remote bodies and URLs so temporary tokens are not echoed.
  Malformed nxm errors also omit the supplied URL. This is not a comprehensive
  security audit of all download/CDN failure paths.

## Verification record

Environment: Linux, Python 3.12.3. Base commit:
`3ffcae00e3240b60e0601765fc5f42af60330506`.

| Check | Result | Kind |
|---|---|---|
| 29 regression tests | Passed | Local, network/authentication/Windows registry simulated |
| CLI help | Passed | Local, real execution |
| git diff --check | Passed | Local whitespace validation |
| Old/new GraphQL game lookup | HTTP 200, same game | Two real public requests |
| games skyrim | Returned four games | One real public request |
| search, count 1 | Returned SkyUI, id 12604 | Two real public requests |
| mod 12604 | Details, SKSE64 requirement and current file metadata returned | Three real public requests |
| Authenticated REST / actual downloads | Not run | Deferred |
| Windows handler / Claude plugin installation | Not run | Deferred |
| Python 3.8 runtime | Not run | Only Python 3.12.3 available for this evaluation |

Eight public API calls were made in total. No API keys were read or transmitted.
The tests verify behavior with simulated headers; actual quota exhaustion was not
induced. Successful public queries verify the exercised query shapes, not the
entire GraphQL schema or all filters.

## Sources and unresolved decision

- [Acceptable use and testing/registration](https://help.nexusmods.com/article/114-api-acceptable-use-policy)
- [Current limits and UTC resets](https://help.nexusmods.com/article/105-i-have-reached-a-daily-or-hourly-limit-api-requests-have-been-consumed-rate-limit-exceeded-what-does-this-mean)
- [GraphQL v2 reference and development status](https://graphql.nexusmods.com/)
- [OAuth/PKCE guide supplied for follow-up](https://modding.wiki/en/api/oauth2-guide)

Support must confirm the accepted authentication approach for evaluation before
any OAuth implementation or credential setup. The AUP describes testing builds
using personal keys, while GraphQL's current reference describes optional OAuth
for protected fields and a developing API; neither constitutes approval of this
application. The OAuth guide did not expose substantive page text to the browser
reader during this review and was not used to infer implementation requirements.

## Follow-up feasibility check (2026-10-07)

Only configuration presence was inspected: `NEXUS_API_KEY` is absent from the
executor environment and no `apikey` file exists at the configured Linux lookup
location. No key contents were read and no new Nexus credentials were created.
GitHub authentication does not provide Nexus API access.

Eleven additional offline tests cover fake key lookup precedence, `whoami` output,
`updated` filtering, archive MD5 lookup, empty mirrors, incomplete-download cleanup,
handler dispatch/error acknowledgment, simulated Windows registration/backup/
restoration and Linux rejection of real registry commands. The complete suite has
29 passing tests. All registry operations are in-memory fakes; no Windows execution
or OS protocol association was tested.

Static handler limitations remain: backup/restoration stores only the previous
open command, not the full registry metadata; unregister does not verify that
this tool still owns the association before restoring/deleting it. The successful
mock round trip does not cover a different application taking ownership between
registration and removal. These behaviors need review before a real handler trial.

Smallest next steps, not performed by this evaluation:

1. REST: identify a user-controlled environment where a Nexus key is already
   configured, then obtain explicit permission for one `whoami` call. That call
   transmits the key only to `https://api.nexusmods.com/v1/users/validate.json` and
   displays account/Premium status and rate headers. If no such environment exists,
   user-side key setup is required in a separate authorized step; do not send the
   key in chat. Support's authentication decision remains pending.
2. Download: select one exact mod/file and destination, show page/name/size, and
   obtain permission to download it. Premium direct download requires a confirmed
   Premium account; a free-account trial requires the user's fresh website-issued
   nxm link. The link contains a temporary secret and must not be published in
   logs or test reports. The actual flow sends the API key to Nexus REST and uses
   a signed URL for the CDN transfer. No arbitrary file was selected or downloaded.
3. Windows: provide a real Windows test environment with Python and an approved
   protocol-association trial, after addressing the handler ownership limitations.
   Linux mocks cannot validate browser launch, native registry behavior, quoting
   under Windows or real restoration of another mod manager.
