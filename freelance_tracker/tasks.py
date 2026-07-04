"""App Tasks"""

# Standard Library
import logging
from datetime import timedelta
from itertools import chain as chain_iterables

# Third Party
from celery import chain as celery_chain
from celery import shared_task

# Django
from django.core.cache import cache
from django.utils import timezone

# Alliance Auth
from allianceauth.eveonline.models import EveCharacter
from esi.decorators import rate_limit_retry_task, rate_limited_task
from esi.exceptions import HTTPNotModified
from esi.models import Token

# AA Freelance Tracker
from freelance_tracker.models import FreelanceJob, FreelanceJobParticipant, Owner
from freelance_tracker.providers import esi

logger = logging.getLogger(__name__)

# Safety-net TTL for the per-owner sync lock: long enough to cover a slow
# sync, but short enough to self-clear before the next hourly periodic run
# if a chain dies without reaching _finalize_corp_sync (e.g. worker crash).
_SYNC_LOCK_TIMEOUT = 60 * 60

# How long past a job's expiry to keep rechecking its participants for
# late-arriving contribution changes, before giving up on it entirely.
_PARTICIPANT_SYNC_GRACE_PERIOD = timedelta(hours=48)


def _sync_lock_key(owner_pk: int) -> str:
    return f"freelance_tracker:sync-lock:{owner_pk}"


def _get_owner_token(owner: Owner) -> Token | None:
    """Find any valid token for the owner's corporation with the required scopes.

    Owner does not pin a specific character - any corp member's token with the
    right scope works, so a director losing their token doesn't break the sync
    as long as another director/manager still has one.
    """

    character_ids = EveCharacter.objects.filter(
        corporation_id=owner.corporation.corporation_id
    ).values_list("character_id", flat=True)

    return (
        Token.objects.filter(character_id__in=character_ids)
        .require_scopes(Owner.get_esi_scopes())
        .require_valid()
        .first()
    )


@shared_task
def update_all_corp_freelance_jobs(force: bool = False):
    """Queue a sync chain for every active tracked corporation

    If `force` is True, it's passed through to each corporation's sync so
    ESI's ETag cache is bypassed and every job ever tracked is re-fetched.
    """

    for owner_pk in Owner.objects.filter(is_active=True).values_list("pk", flat=True):
        update_corp_freelance_jobs(owner_pk, force=force)


def update_corp_freelance_jobs(owner_pk: int, force: bool = False):
    """Queue one corporation's sync: listing, then one task per job in sequence, then a stamp.

    Each corporation's listing fetch happens first, then its jobs are synced
    one task at a time (each hitting ESI once or twice), which is what a
    `task_annotations` rate limit should target. Different corporations'
    syncs are independent and run concurrently of one another.

    If `force` is True, ESI's ETag cache is bypassed and every job the owner
    has ever had tracked (not just currently-active ones) is re-fetched.

    A sync already in progress for this owner (periodic, manual add/refresh,
    or admin force-resync) holds a lock for the duration of its chain, so a
    second trigger for the same owner is a no-op rather than racing the first
    on `jobs_cursor`/`last_update` writes.
    """

    if not cache.add(_sync_lock_key(owner_pk), True, timeout=_SYNC_LOCK_TIMEOUT):
        logger.info("Sync already in progress for owner pk=%s, skipping", owner_pk)
        return None

    return celery_chain(
        _fetch_freelance_job_listing.s(owner_pk, force),
        _dispatch_freelance_job_syncs.s(),
    ).delay()


@shared_task
def _fetch_freelance_job_listing(owner_pk: int, force: bool = False) -> dict:
    """Stage 1: page through the corp's freelance job listing and advance its cursor"""

    owner = Owner.objects.get(pk=owner_pk)
    token = _get_owner_token(owner)

    if token is None:
        logger.warning("No valid freelance-jobs token found for %s", owner.corporation)
        return {"owner_pk": owner_pk, "force": force, "job_ids": [], "skip": True}

    corporation_id = owner.corporation.corporation_id
    after = "0" if force else (owner.jobs_cursor or "0")

    try:
        pages = esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsListing(
            corporation_id=corporation_id, token=token,
        ).results(after=after, force_refresh=force)
    except HTTPNotModified:
        logger.debug("Freelance job listing unchanged for %s", owner.corporation)
        pages = []

    listed_job_ids = {job.id for page in pages for job in page.freelance_jobs}

    # The listing can come back with no (new) jobs either via HTTPNotModified
    # or via a normal response that just reports no changes. Either way, job
    # detail and participants have their own ESI ETags and can still have
    # changed independently, so always recheck jobs we already track as
    # active rather than relying on the listing to surface them. A forced
    # sync rechecks every known job regardless of state.
    known_jobs = FreelanceJob.objects.filter(owner=owner)
    if not force:
        known_jobs = known_jobs.filter(state=FreelanceJob.State.ACTIVE)
    known_job_ids = set(known_jobs.values_list("id", flat=True))

    job_ids = listed_job_ids | known_job_ids

    if pages:
        new_cursor = getattr(getattr(pages[-1], "cursor", None), "after", None)
        if new_cursor:
            owner.jobs_cursor = new_cursor
            owner.save(update_fields=["jobs_cursor"])

    return {
        "owner_pk": owner_pk,
        "force": force,
        "job_ids": [str(job_id) for job_id in job_ids],
        "skip": False,
    }


@shared_task(bind=True)
def _dispatch_freelance_job_syncs(self, listing_result: dict) -> None:
    """Stage 2: chain one task per job, in sequence, then stamp the owner once done"""

    owner_pk = listing_result["owner_pk"]
    job_ids = listing_result["job_ids"]

    if listing_result["skip"] or not job_ids:
        _finalize_corp_sync.delay(owner_pk, listing_result["skip"])
        return

    force = listing_result["force"]
    job_syncs = [_sync_freelance_job.si(owner_pk, job_id, force) for job_id in job_ids]
    return self.replace(celery_chain(*job_syncs, _finalize_corp_sync.si(owner_pk, False)))


@shared_task(bind=True)
@rate_limited_task(rate="150/15m", keys=["owner_pk"]) # total is 900/15m, lets go slow.
@rate_limit_retry_task # if we go over then just retry later anyway.
def _sync_freelance_job(self, owner_pk: int, job_id: str, force: bool = False) -> None:
    """Fetch/update a single job's detail, and recheck its participants.

    Its own task (rather than folded into a loop) so a per-job ESI rate limit
    can be applied.
    """

    owner = Owner.objects.get(pk=owner_pk)
    token = _get_owner_token(owner)

    if token is None:
        logger.warning("No valid freelance-jobs token found for %s", owner.corporation)
        return

    try:
        detail = esi.client.Freelance_Jobs.GetFreelanceJobsDetail(
            job_id=job_id, token=token,
        ).result(force_refresh=force)
    except HTTPNotModified:
        # This job's own detail hasn't changed since our last poll, but
        # contributions can still change independently of job detail/state
        # (and vice versa) - recheck participants below regardless of what
        # state the job is/was in.
        logger.debug("Job %s unchanged", job_id)
        existing = FreelanceJob.objects.filter(pk=job_id).first()
        if existing is None:
            # Nothing local to attach participants to yet (and ESI can't
            # meaningfully 304 a job we've never fetched before) - bail.
            return
        expires = existing.expires
    else:
        dumped = detail.model_dump(mode="json")

        FreelanceJob.objects.update_or_create(
            id=detail.id,
            defaults={
                "owner": owner,
                "name": detail.name,
                "state": detail.state,
                "career": detail.details.career,
                "description": detail.details.description,
                "creator_character_id": detail.details.creator.character.id,
                "created": detail.details.created,
                "expires": detail.details.expires,
                "finished": detail.details.finished,
                "progress_current": detail.progress.current,
                "progress_desired": detail.progress.desired,
                "reward_initial": detail.reward.initial if detail.reward else 0,
                "reward_remaining": detail.reward.remaining if detail.reward else 0,
                "configuration_method": detail.configuration.method,
                "configuration_version": detail.configuration.version,
                "configuration_parameters": dumped["configuration"]["parameters"],
                "contribution": dumped["contribution"] or {},
                "access_and_visibility": dumped["access_and_visibility"] or {},
                "last_modified": detail.last_modified,
            },
        )
        expires = detail.details.expires

    if not force and expires is not None and timezone.now() > expires + _PARTICIPANT_SYNC_GRACE_PERIOD:
        logger.debug(
            "Job %s expired more than %s ago, skipping participant recheck",
            job_id, _PARTICIPANT_SYNC_GRACE_PERIOD,
        )
        return

    _sync_job_participants(owner, job_id, token, force=force)


@shared_task
def _finalize_corp_sync(owner_pk: int, skip: bool = False) -> None:
    """Stage 3: stamp the owner as synced and release its sync lock"""

    cache.delete(_sync_lock_key(owner_pk))

    if skip:
        return

    owner = Owner.objects.get(pk=owner_pk)
    owner.last_update = timezone.now()
    owner.save(update_fields=["last_update"])


def _sync_job_participants(owner: Owner, job_id, token: Token, force: bool = False):
    """Sync the participants of a single active job"""

    corporation_id = owner.corporation.corporation_id
    try:
        pages = esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsParticipants(
            corporation_id=corporation_id, job_id=job_id, token=token,
        ).results(force_refresh=force)
    except HTTPNotModified:
        # No contribution changes for this job since our last poll.
        return

    participants = chain_iterables.from_iterable(page.participants for page in pages)

    for participant in participants:
        FreelanceJobParticipant.objects.update_or_create(
            job_id=job_id,
            character_id=participant.id,
            defaults={
                "character_name": participant.name,
                "state": participant.state,
                "contributed": participant.contributed,
            },
        )
