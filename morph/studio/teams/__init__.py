"""Morph Teams (preview): GitHub for physical things, self-hosted on the Studio computer.

revisions = commits, branches = variant lines, proposals = pull requests (change requests), stage owners by
discipline = CODEOWNERS, reviews and approvals tied to the exact revision, releases gated by build.config,
comments anchored to a revision, stage, BOM line, parameter or file.

Modules: store (team.json, secret, locks), auth (invites, sessions, tokens), roles (route table), disciplines
(stage ownership and review routing), collab (comments, proposals, reviews, the release gate, branches,
releases), activity (feed, inbox, presence), audit (hash-chained security log), cli (`morph team ...`).
The HTTP side is morph/studio/api_team.py; the UI is static/team.js.
"""

from .store import exists, team_dir  # noqa: F401
