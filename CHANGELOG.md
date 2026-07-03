# Change Log

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).

## [In Development] - Unreleased

### Added

- Initial version: corp-wide Freelance Job tracking via ESI, plus a
  character-scoped "My Jobs" screen.
- Status filter dropdown and sorting (by expiry or ISK remaining) on the job
  board.
- Admin action to force a full resync of a corporation's jobs, bypassing
  ESI's ETag cache.
- `force` option on `update_all_corp_freelance_jobs` and
  `update_corp_freelance_jobs` to trigger the above from code/Celery beat.

### Fixed

- The job listing sync now persists and advances an ESI pagination cursor
  per corporation, and always rechecks jobs already known to be `Active`, so
  a listing response with no changes no longer silently skips updating every
  job's detail and participants.
- Job detail responses with no `reward` (e.g. `Deleted` jobs) no longer crash
  the sync task.

### Changed

- Corp syncing now runs as a chain of Celery tasks (listing, then one task
  per job in sequence, then a final stamp) instead of one large task, so an
  ESI rate limit can be applied to the per-job sync task.
