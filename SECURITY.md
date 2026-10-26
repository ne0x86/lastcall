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
- the secret store, with the GitHub App key, the API keys and the monthly budget of each, and the logins of the
  error tracker and of Loki;
- the local model server, which has no way out;
- the masking of what lastcall reads from production before it reaches a model;
- the reading of logs, from containers and from Loki: only the containers and the sources that the owner gives a
  project, and never the containers of lastcall's steps;
- the protections of the web UI.

A report that breaks one of these is especially welcome.
