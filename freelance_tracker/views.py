"""App Views"""

# Django
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.handlers.wsgi import WSGIRequest
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext_lazy as _

# Alliance Auth
from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from esi.decorators import token_required
from esi.models import Token

# AA Freelance Tracker
from freelance_tracker import sde
from freelance_tracker.models import FreelanceJob, Owner
from freelance_tracker.providers import esi
from freelance_tracker.tasks import update_corp_freelance_jobs

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

    context = {
        "job": job,
        "participants": job.participants.all().order_by("-contributed"),
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

    owner, _created = Owner.objects.get_or_create(corporation=corporation)
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

        listing = esi.client.Freelance_Jobs.GetCharactersFreelanceJobsListing(
            character_id=token.character_id, token=token,
        ).result()

        for job in listing.freelance_jobs:
            participation = esi.client.Freelance_Jobs.GetCharactersFreelanceJobsParticipation(
                character_id=token.character_id, job_id=job.id, token=token,
            ).result()
            detail = esi.client.Freelance_Jobs.GetFreelanceJobsDetail(
                job_id=job.id, token=token,
            ).result()

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
