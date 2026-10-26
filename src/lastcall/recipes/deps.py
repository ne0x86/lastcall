"""The deps recipe (DESIGN.md, section 9): the audit's shape, find → judge → fix → verify → deliver, aimed at the
dependency upgrades that need more than a new version number. The pack carries the known vulnerabilities and the
direct dependencies behind their latest versions (`lastcall.scanners.dependencies`). One reviewer chooses the
upgrades worth a pull request, the judge keeps what the project's checks can verify, and the upgrader changes the
manifest, the lock file and the code that the new version breaks, one pull request each.
"""

from __future__ import annotations

from lastcall.recipes.audit import Recipe, Reviewer

UPGRADES = Reviewer("deps", "D", "balanced", "")  # its whole prompt is deps_reviewer.md
DEPS = Recipe("deps", "deps_reviewer.md", "deps_judge.md", "upgrader.md", "pr.deps.footer")
