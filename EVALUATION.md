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
| 18 regression tests | Passed | Local, network/authentication simulated |
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
