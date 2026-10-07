# Security review — evaluation build 1.0.2

Date: 2026-10-07. Scope: the evaluation branch of this standard-library Python CLI,
with emphasis on API credential transport, signed download URLs, local download
writes and reversible Windows protocol registration. This is an author-performed
code review and regression test exercise, not an independent audit, certification,
or claim that no vulnerabilities remain. Nexus registration is still pending.

## Method and tools actually used

- Manual inspection of the complete changed code, surrounding call paths and tests,
  following the locally available `review-agent` skill's defect-first procedure
  (`.codex/skills/.system/review-agent/SKILL.md`). This skill was first applied in
  this follow-up; earlier work used manual review without a security skill.
- No dedicated security skill was found in the exposed catalog or inspected local
  skill locations. Codex Security was not installed or invoked. Bandit, Semgrep,
  pip-audit and Gitleaks executables were not available on PATH. None was installed.
- Python 3.12.3 `unittest` and `py_compile`, plus `git diff --check`, were executed.
  No external security scanner, dependency audit service, penetration test or code
  upload to a third-party review service was performed. Only the authorized GitHub
  review branch is published.

## Concrete findings corrected

| Finding | Correction | Reproducible regression evidence |
|---|---|---|
| Handler backup saved only the previous command, losing overwritten protocol metadata | Versioned backup saves all three modified values, types and absence; unrelated values/subkeys/permissions are not reconstructed or deleted | `test_restore_original_metadata_types_binary_and_unrelated_subkeys` |
| Unregister could overwrite/remove a different application's current association | Exact managed state check; no substring ownership test; missing/legacy/corrupt backup fails closed | `test_owner_change_blocks_register_and_unregister_keeps_backup`, `test_changed_managed_metadata_blocks_restore`, `test_missing_backup_never_deletes_foreign_handler` |
| Partial registry failures could leave an untracked change | Backup written before changes; guarded rollback; retain backup on incomplete recovery; repeat registration does not replace original snapshot | `test_failed_install_rolls_back_and_removes_new_empty_keys`, `test_failed_restore_rolls_back_keeps_backup_and_can_retry`, `test_backup_cleanup_failure_allows_safe_retry` |
| Default urllib redirect behavior could forward an API-key header to a redirected destination | Authenticated redirects refused; public/CDN redirects require HTTPS without userinfo | `test_authenticated_redirects_are_refused_without_second_request`, `test_public_redirects_allow_https_but_not_downgrade_or_userinfo` |
| Decoding a remote basename could introduce path separators/traversal; unsafe schemes were accepted | HTTPS-only initial CDN URL and strict decoded basename validation, including Windows reserved names and control characters | `test_download_rejects_unsafe_filenames_and_schemes_before_io` |
| Existing partial files/symlinks could be opened for truncation | Exclusive creation; never truncate/remove a pre-existing partial path | `test_existing_partial_file_is_not_truncated_or_removed`, `test_partial_symlink_is_not_followed` |
| CDN exceptions could expose signed URLs and leave partial files | Sanitized transfer failure messages and cleanup of this attempt's partial file | `test_cdn_errors_do_not_expose_signed_url_and_cleanup_partial` |

Earlier evaluation fixes also suppress API response bodies/request URLs in errors
and malformed nxm input in diagnostics. The final review found malformed backup
value types could reach restoration; validation now rejects those before any write
(`test_corrupt_backup_value_type_is_rejected_before_restore`).

## Reproduce locally

From the repository root, without keys or network access:

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile plugins/nexus-mods/skills/nexus-mods/scripts/nexus.py
git diff --check
```

Result: **51 tests passed**; compilation and diff checks passed. Network, credentials
and the Windows registry are simulated, with temporary directories for file tests.
Tests use fake credential strings only. One public `games skyrim` request also
passed using the new HTTP transport; it neither reads a key nor downloads a mod.
The earlier eight public calls are recorded separately in EVALUATION.md.

## Remaining limits and required evaluation

- Windows was not run. Native registry access rights, browser invocation, existing
  Windows URL-association overrides and real mod-manager restoration need testing
  on Windows. Three managed values and created empty keys are handled; this is not
  a whole-system association manager. Existing registry ACLs are left untouched.
- Registry checks/writes are not a Windows transaction. The code rechecks before
  each write and refuses observed ownership changes, but cannot eliminate a narrow
  concurrent-writer race or guarantee recovery from process termination/power loss.
  Do not run association-changing applications concurrently. A retained backup
  after partial failure requires inspection; do not blindly delete it and retry.
- Legacy command-only backups cannot reconstruct missing metadata. They remain
  untouched and require manual recovery rather than guessed migration.
- Actual authenticated REST, entitlement checks, signed CDN transfers and free/
  Premium account behavior remain untested. No Nexus key is configured in this
  executor. Authenticated redirects now fail closed; assess compatibility later.
- Partial-file exclusivity does not sandbox a malicious local user controlling the
  destination directory. A successful transfer retains the existing behavior of
  replacing a same-name completed file. Choose a trusted destination directory.
- HTTPS and path checks do not validate that an archive is benign or suitable for
  installation. MD5 is an identification checksum, not authenticity verification.
  No archive is extracted or executed by this tool. CDN host authorization beyond
  HTTPS is based on the Nexus-provided link, not a pinned host allowlist.
- The quota guard remains process-local. Separate executions and other API clients
  are not coordinated. No stress test or deliberate quota exhaustion was performed.
- Python 3.8 and actual Claude plugin installation remain untested; execution here
  used Python 3.12.3 on Linux. The repository had no configured CI workflows.

Next user-dependent steps are unchanged: select an environment with an existing
Nexus key and authorize a single validation request; approve one specific download
and destination; provide a Windows environment for the native handler tests.
Do not share passwords, API keys or temporary nxm secrets in reports or chat.
