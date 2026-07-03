"""App Models"""

# Django
from django.db import models

# Alliance Auth
from allianceauth.eveonline.models import EveCorporationInfo


class Owner(models.Model):
    """A corporation whose Freelance Jobs are tracked by this app"""

    corporation = models.OneToOneField(
        EveCorporationInfo, on_delete=models.CASCADE, related_name="freelance_tracker_owner"
    )
    is_active = models.BooleanField(
        default=True, help_text="Disabled owners are skipped by the sync task"
    )
    last_update = models.DateTimeField(null=True, blank=True)
    jobs_cursor = models.CharField(
        max_length=255,
        blank=True,
        default="",
        help_text="ESI pagination cursor for the freelance jobs listing, advanced as new/updated jobs are discovered",
    )

    class Meta:
        """Meta definitions"""

        default_permissions = ()

    def __str__(self) -> str:
        return str(self.corporation)

    @classmethod
    def get_esi_scopes(cls) -> list[str]:
        """ESI scopes required to sync this owner's freelance jobs"""

        return ["esi-corporations.read_freelance_jobs.v1"]


class FreelanceJob(models.Model):
    """A single Freelance Job, as reported by ESI for a tracked corporation"""

    class State(models.TextChoices):
        """Job state, mirrors the ESI `state` enum"""

        UNSPECIFIED = "Unspecified"
        ACTIVE = "Active"
        CLOSED = "Closed"
        COMPLETED = "Completed"
        EXPIRED = "Expired"
        DELETED = "Deleted"

    class Career(models.TextChoices):
        """Job career, mirrors the ESI `career` enum"""

        UNSPECIFIED = "Unspecified"
        EXPLORER = "Explorer"
        INDUSTRIALIST = "Industrialist"
        ENFORCER = "Enforcer"
        SOLDIER_OF_FORTUNE = "Soldier of Fortune"

    id = models.UUIDField(primary_key=True)
    owner = models.ForeignKey(Owner, on_delete=models.CASCADE, related_name="jobs")

    name = models.CharField(max_length=255)
    state = models.CharField(max_length=32, choices=State.choices, default=State.UNSPECIFIED)
    career = models.CharField(max_length=32, choices=Career.choices, blank=True)
    description = models.TextField(blank=True)

    creator_character_id = models.PositiveIntegerField(null=True, blank=True)
    created = models.DateTimeField(null=True, blank=True)
    expires = models.DateTimeField(null=True, blank=True)
    finished = models.DateTimeField(null=True, blank=True)

    progress_current = models.BigIntegerField(default=0)
    progress_desired = models.BigIntegerField(default=0)

    reward_initial = models.FloatField(default=0)
    reward_remaining = models.FloatField(default=0)

    # `configuration.parameters` is a `oneOf` of 4 shapes keyed by SDE-defined
    # contribution method (matcher/options/boolean/corporation_item_delivery).
    # Kept raw until the SDE contribution-method data is available to resolve
    # method + parameter keys into human-readable labels.
    configuration_method = models.CharField(max_length=64, blank=True)
    configuration_version = models.PositiveIntegerField(default=1)
    configuration_parameters = models.JSONField(default=dict, blank=True)

    contribution = models.JSONField(default=dict, blank=True)
    access_and_visibility = models.JSONField(default=dict, blank=True)

    last_modified = models.DateTimeField()

    CAREER_ICONS = {
        Career.EXPLORER: "fa-compass",
        Career.INDUSTRIALIST: "fa-industry",
        Career.ENFORCER: "fa-shield-halved",
        Career.SOLDIER_OF_FORTUNE: "fa-crosshairs",
    }

    class Meta:
        default_permissions = ()
        ordering = ["-created"]

    def __str__(self) -> str:
        return f"{self.name} ({self.owner})"

    @property
    def career_icon(self) -> str:
        """Font Awesome icon class representing this job's career"""

        return self.CAREER_ICONS.get(self.career, "fa-circle-question")


class FreelanceJobParticipant(models.Model):
    """A character's participation in a FreelanceJob"""

    class State(models.TextChoices):
        """Participation state, mirrors the ESI `state` enum"""

        UNSPECIFIED = "Unspecified"
        COMMITTED = "Committed"
        KICKED = "Kicked"
        RESIGNED = "Resigned"

    job = models.ForeignKey(FreelanceJob, on_delete=models.CASCADE, related_name="participants")
    character_id = models.PositiveIntegerField()
    character_name = models.CharField(max_length=255)
    state = models.CharField(max_length=32, choices=State.choices, default=State.UNSPECIFIED)
    contributed = models.BigIntegerField(default=0)

    class Meta:
        default_permissions = ()
        unique_together = ("job", "character_id")

    def __str__(self) -> str:
        return f"{self.character_name} - {self.job}"


class General(models.Model):
    """Meta model for app permissions"""

    class Meta:
        """Meta definitions"""

        managed = False
        default_permissions = ()
        permissions = (
            ("basic_access", "Can access this app"),
            ("add_corp_owner", "Can add or update the ESI token for a corporation"),
            ("view_corp", "Can view freelance jobs for their own corporation"),
            ("view_alliance", "Can view freelance jobs for their own alliance"),
            ("view_faction", "Can view freelance jobs for their own faction"),
        )
