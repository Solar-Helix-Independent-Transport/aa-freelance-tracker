"""App Views"""

# Standard Library
import logging
from collections import defaultdict
from datetime import timedelta

# Django
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.handlers.wsgi import WSGIRequest
from django.db.models import Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

# Alliance Auth
from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from esi.decorators import token_required
from esi.exceptions import HTTPNotModified
from esi.models import Token

# AA Freelance Tracker
from freelance_tracker import app_settings, sde
from freelance_tracker.models import FreelanceJob, FreelanceJobParticipant, Owner
from freelance_tracker.providers import esi
from freelance_tracker.tasks import update_corp_freelance_jobs

logger = logging.getLogger(__name__)

MY_JOBS_SCOPES = ["esi-characters.read_freelance_jobs.v1"]


def _visible_corporation_ids(request) -> set[int]:
    """Corporation IDs the requesting user may see freelance jobs for.

    Determined by the requesting user's main character and whichever of the
    view_corp/view_alliance/view_faction permissions they hold - a user can
    hold any combination of the three, and sees the union of what they grant.
    Superusers see every tracked corporation regardless of main character.
    """

    if request.user.is_superuser:
        return set(Owner.objects.values_list("corporation__corporation_id", flat=True))

    main_character = request.user.profile.main_character
    if not main_character:
        return set()

    corp_ids: set[int] = set()

    if request.user.has_perm("freelance_tracker.view_corp"):
        corp_ids.add(main_character.corporation_id)

    if request.user.has_perm("freelance_tracker.view_alliance") and main_character.alliance_id:
        corp_ids.update(
            Owner.objects.filter(
                corporation__alliance__alliance_id=main_character.alliance_id
            ).values_list("corporation__corporation_id", flat=True)
        )

    if request.user.has_perm("freelance_tracker.view_faction") and main_character.faction_id:
        corp_ids.update(
            Owner.objects.filter(
                corporation__faction__faction_id=main_character.faction_id
            ).values_list("corporation__corporation_id", flat=True)
        )

    return corp_ids


@login_required
@permission_required("freelance_tracker.basic_access")
def index(request: WSGIRequest) -> HttpResponse:
    """Freelance jobs the requesting user is allowed to see"""

    jobs = FreelanceJob.objects.select_related("owner__corporation").filter(
        owner__corporation__corporation_id__in=_visible_corporation_ids(request)
    )

    context = {"jobs": jobs, "job_states": FreelanceJob.State.choices}

    return render(request, "freelance_tracker/job_list.html", context)


def _resolve_identities(character_ids) -> dict[int, dict]:
    """Map participant character_ids to a rollup identity: who to credit and
    what corp/alliance ticker to show for them.

    Alts roll up under their owner's main character (keyed by the main
    character's own id, so multiple alts collapse to one row), using that
    main character's current corp/alliance ticker. A character with no
    linked user (or no main character set) falls back to its own
    EveCharacter record if one exists; otherwise it's simply absent and
    callers should fall back to the raw participant name with no ticker.
    """

    identities: dict[int, dict] = {}

    ownerships = CharacterOwnership.objects.filter(
        character__character_id__in=character_ids,
        user__profile__main_character__isnull=False,
    ).select_related("user__profile__main_character")
    for ownership in ownerships:
        main = ownership.user.profile.main_character
        identities[ownership.character.character_id] = {
            "key": main.character_id,
            "name": main.character_name,
            "corporation_ticker": main.corporation_ticker,
            "alliance_ticker": main.alliance_ticker,
        }

    remaining_ids = set(character_ids) - identities.keys()
    if remaining_ids:
        for char in EveCharacter.objects.filter(character_id__in=remaining_ids):
            identities[char.character_id] = {
                "key": char.character_id,
                "name": char.character_name,
                "corporation_ticker": char.corporation_ticker,
                "alliance_ticker": char.alliance_ticker,
            }

    return identities


@login_required
@permission_required("freelance_tracker.basic_access")
def leaderboard(request: WSGIRequest) -> HttpResponse:
    """Total contributed per main character, grouped by career, over the last N days

    Sums `contributed` across every participant row (regardless of state) on
    jobs created within the window. Note this adds together whatever units
    each job's contribution method uses (HP, m3, ISK, ...), which only
    stays meaningful within a single career if its job types share units -
    otherwise the total is a mix of incomparable magnitudes.
    """

    try:
        days = int(request.GET.get("days", app_settings.LEADERBOARD_DEFAULT_DAYS))
    except (TypeError, ValueError):
        days = app_settings.LEADERBOARD_DEFAULT_DAYS
    days = max(1, min(days, app_settings.LEADERBOARD_MAX_DAYS))

    cutoff = timezone.now() - timedelta(days=days)

    participations = (
        FreelanceJobParticipant.objects.filter(
            job__owner__corporation__corporation_id__in=_visible_corporation_ids(request),
            job__created__gte=cutoff,
        )
        .exclude(job__career__in=["", FreelanceJob.Career.UNSPECIFIED])
        .values("character_id", "character_name", "job__career")
        .annotate(total=Sum("contributed"))
    )

    identities = _resolve_identities({row["character_id"] for row in participations})

    totals: dict[tuple[str, int], int] = defaultdict(int)
    display_by_key: dict[int, dict] = {}
    for row in participations:
        identity = identities.get(row["character_id"])
        if identity:
            key = identity["key"]
            display_by_key[key] = identity
        else:
            key = row["character_id"]
            display_by_key.setdefault(
                key,
                {"name": row["character_name"], "corporation_ticker": "", "alliance_ticker": ""},
            )

        totals[(row["job__career"], key)] += row["total"] or 0

    rows_by_career: dict[str, list[dict]] = defaultdict(list)
    for (career, key), total in totals.items():
        if total <= 0:
            continue

        display = display_by_key[key]
        rows_by_career[career].append(
            {
                "main_character": display["name"],
                "corporation_ticker": display["corporation_ticker"],
                "alliance_ticker": display["alliance_ticker"],
                "total": total,
            }
        )

    boards = [
        {
            "career": career,
            "label": label,
            "rows": sorted(
                rows_by_career.get(career, []), key=lambda r: -r["total"]
            )[: app_settings.LEADERBOARD_SIZE],
        }
        for career, label in FreelanceJob.Career.choices
        if career != FreelanceJob.Career.UNSPECIFIED
    ]

    context = {
        "days": days,
        "default_days": app_settings.LEADERBOARD_DEFAULT_DAYS,
        "boards": boards,
        "can_view_contributions": request.user.has_perm("freelance_tracker.view_participants"),
    }

    return render(request, "freelance_tracker/leaderboard.html", context)


@login_required
@permission_required("freelance_tracker.basic_access")
def job_detail(request: WSGIRequest, job_id) -> HttpResponse:
    """Job description/config plus a participants table"""

    job = get_object_or_404(
        FreelanceJob.objects.filter(
            owner__corporation__corporation_id__in=_visible_corporation_ids(request)
        ),
        pk=job_id,
    )

    can_view_participants = request.user.has_perm("freelance_tracker.view_participants")

    context = {
        "job": job,
        "can_view_participants": can_view_participants,
        "participants": (
            job.participants.all().order_by("-contributed") if can_view_participants else None
        ),
        "method_title": sde.get_method_title(job.configuration_method),
        "method_description": sde.get_method_description(job.configuration_method),
        "parameter_rows": sde.describe_parameters(
            job.configuration_method, job.configuration_parameters
        ),
    }

    return render(request, "freelance_tracker/job_detail.html", context)


@login_required
@permission_required("freelance_tracker.add_corp_owner")
@token_required(scopes=Owner.get_esi_scopes())
def add_corp(request: WSGIRequest, token) -> HttpResponse:
    """Director-only view to link a corporation for Freelance Job tracking"""

    character = EveCharacter.objects.get_character_by_id(
        token.character_id
    ) or EveCharacter.objects.create_character(token.character_id)

    try:
        corporation = character.corporation
    except EveCorporationInfo.DoesNotExist:
        corporation = EveCorporationInfo.objects.create_corporation(
            corporation_id=character.corporation_id
        )

    owner, created = Owner.objects.get_or_create(
        corporation=corporation, defaults={"is_active": True}
    )
    if not created and not owner.is_active:
        owner.is_active = True
        owner.save(update_fields=["is_active"])

    update_corp_freelance_jobs(owner.pk)

    messages.success(
        request, _("Linked %(corporation)s for Freelance Job tracking.") % {"corporation": corporation}
    )

    return redirect("freelance_tracker:index")


@login_required
@token_required(scopes=MY_JOBS_SCOPES, new=True)
def add_character(request: WSGIRequest, token) -> HttpResponse:
    """Link a character (via a fresh SSO token) for the 'My Jobs' screen"""

    messages.success(
        request, _("%(character)s linked for My Jobs.") % {"character": token.character_name}
    )

    return redirect("freelance_tracker:my_jobs")


@login_required
@permission_required("freelance_tracker.basic_access")
def my_jobs(request: WSGIRequest) -> HttpResponse:
    """Live, per-character view of jobs the requesting user is participating in"""

    tokens = (
        Token.objects.filter(user=request.user)
        .require_scopes(MY_JOBS_SCOPES)
        .require_valid()
    )
    seen_character_ids = set()
    rows = []

    for token in tokens:
        if token.character_id in seen_character_ids:
            continue
        seen_character_ids.add(token.character_id)

        try:
            listing = esi.client.Freelance_Jobs.GetCharactersFreelanceJobsListing(
                character_id=token.character_id, token=token,
            ).result()
        except HTTPNotModified:
            # Nothing changed since our last poll, but this view has no
            # persisted copy of the listing to fall back on, so there's
            # nothing to show for this character this request.
            logger.debug("Freelance jobs listing unchanged for %s", token.character_name)
            continue

        for job in listing.freelance_jobs:
            try:
                participation = esi.client.Freelance_Jobs.GetCharactersFreelanceJobsParticipation(
                    character_id=token.character_id, job_id=job.id, token=token,
                ).result()
                detail = esi.client.Freelance_Jobs.GetFreelanceJobsDetail(
                    job_id=job.id,
                ).result()
            except HTTPNotModified:
                logger.debug("Job %s unchanged, skipping row for %s", job.id, token.character_name)
                continue

            rows.append(
                {
                    "character_name": token.character_name,
                    "job": job,
                    "detail": detail,
                    "participation": participation,
                }
            )

    context = {
        "rows": rows,
        "has_linked_character": bool(seen_character_ids),
    }

    return render(request, "freelance_tracker/my_jobs.html", context)
