# AA Freelance Tracker

An [Alliance Auth](https://gitlab.com/allianceauth/allianceauth) plugin that tracks
corporation "Freelance Jobs" from the EVE Online ESI.

## Features

- A searchable, sortable board of every Freelance Job tracked across your linked
  corporations, with a status filter (Active, Closed, Completed, Expired, ...) and
  sorting by expiry or ISK remaining.
- A job detail page with progress, reward, configuration, and a participants list.
- A "My Jobs" screen showing the Freelance Jobs the logged-in user's own characters
  are personally participating in, live from ESI.
- Visibility scoped to what a user is permitted to see: their own corporation,
  their alliance, and/or their faction, in any combination.
- Corp-wide syncing runs as a chain of Celery tasks per corporation (see
  [How syncing works](#how-syncing-works) below), so a slow or failing job
  doesn't block the rest of that corp's sync.

![Job board with status filter, sorting, and search](imgs/list.png)

![Job detail page with progress, reward, configuration, and participants](imgs/details.png)

## Installation

1. Install the app:

   ```bash
   pip install aa-freelance-tracker
   ```

1. Add `"freelance_tracker"` to `INSTALLED_APPS` in your Auth project's
   `settings/local.py`.

1. Run migrations and collect static files:

   ```bash
   python manage.py migrate
   python manage.py collectstatic
   ```

1. Restart your Auth (gunicorn, Celery worker, and Celery beat).

1. Run the [post-install setup](#post-install-setup) command below to schedule
   the periodic sync.

## ESI Scopes

- `esi-corporations.read_freelance_jobs.v1` - required from a director/manager
  of a corporation before it can be linked for corp-wide tracking (requested via
  the "Add / Refresh Corp Token" button).
- `esi-characters.read_freelance_jobs.v1` - required from a character before it
  shows up on that user's "My Jobs" screen (requested via "Add / Refresh
  Character" on that page).

## Permissions

| Permission       | Description                                                                                                |
| ---------------- | ---------------------------------------------------------------------------------------------------------- |
| `basic_access`   | Can access the app at all.                                                                                 |
| `add_corp_owner` | Can link (or refresh the ESI token for) a corporation.                                                     |
| `view_corp`      | Can see Freelance Jobs for their own corporation.                                                          |
| `view_alliance`  | Can see Freelance Jobs for every corporation in their alliance (their own corp is included automatically). |
| `view_faction`   | Can see Freelance Jobs for every corporation in their faction.                                             |

A user can hold any combination of `view_corp`/`view_alliance`/`view_faction` and
sees the union of what they grant. `basic_access` is required in addition to at
least one of the `view_*` permissions to see anything on the job board; without
it, `my_jobs` (which only shows the user's own participation) still requires
`basic_access` too.

## Usage

- **Link a corporation**: a director or manager with `add_corp_owner` clicks
  "Add / Refresh Corp Token" and authorizes with the required ESI scope. This
  immediately queues a sync for that corporation and also re-authorizes an
  already-linked corporation if its token has expired or lost the scope.
- **Browse jobs**: the main job board supports free-text search (name,
  corporation, career, status), a status dropdown (defaults to Active), and
  sorting by soonest expiry or highest ISK remaining.
- **My Jobs**: any user can link a character (via "Add / Refresh Character")
  to see the Freelance Jobs that character is personally contributing to,
  fetched live from ESI on page load.
- **Force a full resync**: from Django admin (`/admin/freelance_tracker/owner/`),
  select one or more corporations and use the "Force a full resync from ESI
  (bypasses cache)" action. This re-fetches every job the corporation has ever
  had tracked, ignoring ESI's ETag cache.

## Post-install setup

Linking a corporation (via the "Add / Refresh Corp Token" button) syncs its jobs
once immediately, but nothing refreshes them after that unless a periodic task
is scheduled. Run this once after installing/upgrading:

```bash
python manage.py freelance_tracker_setup
```

This registers an hourly `PeriodicTask` (via `django-celery-beat`) that calls
`freelance_tracker.tasks.update_all_corp_freelance_jobs` for every tracked
corporation. It's safe to run again after upgrades - it just updates the
existing schedule rather than duplicating it.

## How syncing works

Each corporation's sync is a small chain of Celery tasks, not one big task:

1. **Listing** (`_fetch_freelance_job_listing`) pages through the corp's
   Freelance Job listing from ESI using cursor-based pagination, advancing and
   persisting the corp's cursor (`Owner.jobs_cursor`) so the next sync only
   looks at what's new. Jobs already tracked as `Active` are always rechecked
   too, since a job's detail and its participants have their own ESI caching
   independent of the listing.
1. **Dispatch** (`_dispatch_freelance_job_syncs`) chains one task per job to
   sync, in sequence.
1. **Per-job sync** (`_sync_freelance_job`) fetches that job's detail, updates
   it in the database, and syncs its participants if it's `Active`. This is
   the task an ESI rate limit is applied to, since it's the one making the
   per-job ESI calls.
1. **Finalize** (`_finalize_corp_sync`) stamps `Owner.last_update` once every
   job in that corp's sync has finished.

`update_all_corp_freelance_jobs` (the periodic task) queues one such chain per
active corporation; different corporations' chains run independently and
concurrently of one another. Both `update_all_corp_freelance_jobs` and
`update_corp_freelance_jobs` accept a `force=True` option that bypasses ESI's
ETag cache and re-fetches every job ever tracked, rather than just what
changed - this is what the admin's "Force a full resync" action uses.
