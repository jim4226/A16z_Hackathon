# Changes

## 0.2.0b1 — local public-beta foundation

- First-launch project creation/copying, Claude Settings, session-only or system-vault credentials,
  explicit network/sourcing/telemetry controls, and per-user project storage.
- Local session and origin checks, bounded requests/workers/event queues, canonical paths and
  authenticated revision downloads; demo reset is opt-in.
- Complete wheel and source-free setup bundle with hashed dependencies; launchers no longer update
  the checkout or install dependencies on every launch.
- Failed/incomplete reused stages remain invalid; required artifacts are checked. Fallback CAD is
  preview-only and cannot produce manufacturing output.
- SO-101 revolute gripper firmware uses angular units; rack grippers retain linear units.
- Synthetic remote build history no longer changes force targets. Telemetry requires opt-in.
- Revision ZIP export includes stage and firmware-validation status, excluding app settings and keys.

This is not a hosted multi-user release. Python remains a prerequisite. Native installer signing,
macOS validation, actual Arduino-core/library builds and physical hardware validation remain open.
