# Publishing a local beta

The current branch builds an installable preview; it does not automatically publish a GitHub Release.

- [ ] Review and merge the release PR; require the exact commit's Windows/Linux CI matrix to pass.
- [ ] Run `python tools/run_offline_tests.py -q` with no credentials.
- [ ] Run `node tools/browser_smoke.cjs` with Playwright 1.62.1 and Chromium installed. This starts an
  isolated, offline local instance and uses fake session credentials, never the OS credential store.
- [ ] Build with `python tools/build_release.py`; install the wheel outside the checkout and run
  `python -I tools/package_smoke.py`.
- [ ] Extract the setup ZIP on a clean Windows machine with Python 3.11; create, edit, export and
  reopen a project. Confirm ordinary relaunch does not install dependencies or pull code.
- [ ] Verify system credential storage and deletion on each advertised operating system, including
  a locked/unavailable store. Session-only tests alone do not validate every OS backend.
- [ ] Verify upgrade/rollback using a copy of existing user projects. Keep a backup before testing
  older code against new project formats. Do not claim rollback validation before this is exercised.
- [ ] Check artifact hashes and bundled licenses/notices. Publish versioned assets and release notes
  that state the Python prerequisite and hardware-validation limits.
- [ ] Keep a previous release available; withdraw the new asset if setup, project integrity or key
  privacy fails. A patch release should replace an asset with a new version, never overwrite its hash.

Before advertising generated firmware as ready for hardware, build with actual pinned board cores
and libraries and perform recorded bench tests for every supported configuration. Current endpoint
and stub compilation tests do not substitute for this.

A public website may show the labeled fixture preview and download instructions. It must not expose
the local Studio backend. Hosted accounts, isolated projects/jobs and collaborative approvals need a
separate design and review.
