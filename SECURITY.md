# Security policy

## Reporting a vulnerability

Please do not open a public issue. Report it privately on GitHub:
[Security › Report a vulnerability](https://github.com/ne0x86/lastcall/security/advisories/new).

Say what you found, how to reproduce it, and what an attacker could do with it. The maintainer will answer as soon
as possible, and will credit you in the fix's release notes unless you prefer otherwise.

## Supported versions

Security fixes go into the latest release.

## What lastcall protects

[DESIGN.md, section 12](DESIGN.md#12-security-model) describes the threat model:
- the sandbox that every agent step runs in;
- the egress allowlist;
- the scope of the GitHub App;
- the secret store;
- the protections of the web UI.

A report that breaks one of these is especially welcome.
