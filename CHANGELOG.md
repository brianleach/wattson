# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] - 2026-10-19
### Added
- Last-poll age beside Updated: the dashboard now shows a relative age
  (e.g. "5 min ago") next to the absolute timestamp, refreshing each minute
  without polling the vehicle. Contributed by @shanselman.
- Savings transparency: the savings section now shows which unpriced DC
  sessions are excluded from the total and states the MPG and fallback
  gas-price assumptions. Contributed by @shanselman.
### Fixed
- Mobile charging-cost entry: decimal keyboard hint and 44px tap targets for
  cost actions on phones. Contributed by @shanselman.
- Idle-drain tables on narrow screens: wrapped in a horizontal scroll
  container so wide content is not clipped. Contributed by @shanselman.
- Section navigation on phones: section links now show in a horizontally
  scrollable mobile row, with scrollspy kept working and the fixed header
  measured dynamically when jumping to a section. Contributed by @shanselman.

## [0.4.0] - 2026-10-15
### Added
- Drive-mode trip cards: each trip now shows the drive mode beside the
  temperature, e.g. "Everyday + Offroad" when a trip used more than one mode.
- Per-session paid display: DC fast-charge rows now show the session's total
  paid next to the saved/lost figure.

## [0.3.0] - 2026-10-14
### Added
- Phantom drain rework: idle drain is now measured between actual drives
  (not a fixed overnight window), with watts as the primary metric. Periods
  with no fresh data show as "no data" instead of fake zeros.
### Fixed
- Stale charging sessions: charging sessions now end at the last SoC increase
  when the API reports stale charging state, killing phantom sessions.

## [0.2.0] - 2026-10-11
### Added
- Optional dashboard login: set `WATTSON_DASHBOARD_TOKEN` to password-protect
  your dashboard (handy if you're exposing it through a tunnel). Unset by
  default, everything works as before. Contributed by @brianleach.
### Fixed
- Rivian authentication response handling. Contributed by Scott Hanselman.

## [0.1.0] - 2026-10-04
### Added
- Initial public release of Wattson, the open-source Rivian energy tracker.
- Fuel savings vs gas: lifetime and per-session savings against a gas SUV,
  priced at each charge day's AAA gas price. Home sessions count
  automatically; DC fast-charge costs are entered by hand.
- Clickable hero cards: the top stats jump to their detail sections.
- Savings breakdown table: every session with AC/DC type, kWh, EV cost,
  and gas equivalent.
- Section navigation: sticky nav with scrollspy that follows you down
  the page.
- Demo mode: synthetic 10-day driving history generator with a "Demo data"
  banner, so you can explore the dashboard without a car.
- PII protection: pre-commit hook plus a GitHub Actions scan on every push,
  so personal data never lands in the public repo.



