"""Tests for the freelance_tracker app"""

# Standard Library
import uuid
from datetime import datetime
from datetime import timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

# Django
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

# Alliance Auth
from allianceauth.eveonline.models import (
    EveAllianceInfo,
    EveCharacter,
    EveCorporationInfo,
    EveFactionInfo,
)
from allianceauth.tests.auth_utils import AuthUtils
from esi.exceptions import HTTPNotModified
from esi.models import Scope, Token

# AA Freelance Tracker
from freelance_tracker.models import FreelanceJob, FreelanceJobParticipant, Owner
from freelance_tracker.tasks import update_corp_freelance_jobs

# Rendering a real page pulls in allianceauth/base-bs5.html, which needs a
# staticfiles manifest from `collectstatic`. Plain filesystem storage avoids
# that requirement for tests that render a full page.
_TEST_STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


def _create_token(
    *,
    user=None,
    character_id,
    character_name,
    scope_name,
    corporation_id=1000000,
    corporation_name="",
    corporation_ticker="",
) -> Token:
    """Create a freshly-issued (therefore non-expired) Token with a given scope.

    Ensures the EveCharacter exists first: AA's `record_character_ownership`
    signal fires create_character() -> a real ESI lookup for unknown
    character IDs, which we don't want in a unit test.
    """

    EveCharacter.objects.get_or_create(
        character_id=character_id,
        defaults={
            "character_name": character_name,
            "corporation_id": corporation_id,
            "corporation_name": corporation_name,
            "corporation_ticker": corporation_ticker,
        },
    )

    scope, _created = Scope.objects.get_or_create(
        name=scope_name, defaults={"help_text": ""}
    )
    token = Token.objects.create(
        user=user,
        character_id=character_id,
        character_name=character_name,
        character_owner_hash=f"hash-{character_id}",
        access_token="access-token",
        refresh_token="refresh-token",
    )
    token.scopes.add(scope)

    return token


def _make_job_detail(job_id: uuid.UUID) -> SimpleNamespace:
    """Build a fake ESI FreelanceJobsDetail-shaped object, as returned by aiopenapi3"""

    details = SimpleNamespace(
        career="Explorer",
        created=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
        creator=SimpleNamespace(
            character=SimpleNamespace(id=1001, name="Creator Guy"),
            corporation=SimpleNamespace(id=2345, name="Test Corp"),
        ),
        description="A test job",
        expires=datetime(2026, 2, 1, tzinfo=dt_timezone.utc),
        finished=None,
    )
    dumped = {
        "configuration": {
            "method": "BoostShield",
            "version": 1,
            "parameters": {"matcher": {"values": []}},
        },
        "contribution": {"max_committed_participants": 10000},
        "access_and_visibility": {"acl_protected": False},
    }

    return SimpleNamespace(
        id=job_id,
        name="Test Job",
        state="Active",
        last_modified=datetime(2026, 1, 15, tzinfo=dt_timezone.utc),
        details=details,
        progress=SimpleNamespace(current=10, desired=100),
        reward=SimpleNamespace(initial=1000.0, remaining=500.0),
        configuration=SimpleNamespace(method="BoostShield", version=1),
        model_dump=lambda mode=None: dumped,
    )


class TestUpdateCorpFreelanceJobs(TestCase):
    """Tests for tasks.update_corp_freelance_jobs"""

    @classmethod
    def setUpTestData(cls):
        cls.corporation = EveCorporationInfo.objects.create(
            corporation_id=2345, corporation_name="Test Corp", corporation_ticker="TEST"
        )
        cls.owner = Owner.objects.create(corporation=cls.corporation)
        cls.director_character_id = 1001
        # The token owner just needs to be an EveCharacter in the tracked
        # corporation - the sync task doesn't care who registered it.
        _create_token(
            character_id=cls.director_character_id,
            character_name="Creator Guy",
            scope_name="esi-corporations.read_freelance_jobs.v1",
            corporation_id=cls.corporation.corporation_id,
            corporation_name=cls.corporation.corporation_name,
            corporation_ticker=cls.corporation.corporation_ticker,
        )

    def setUp(self):
        # _sync_freelance_job is rate-limited per owner_pk via the cache, and
        # every test in this class shares the same owner (created once in
        # setUpTestData) - clear it so one test's calls don't burn the next
        # test's rate-limit bucket.
        cache.clear()

    @patch("freelance_tracker.tasks.esi")
    def test_creates_job_and_participants_for_active_job(self, mock_esi):
        job_id = uuid.uuid4()
        detail = _make_job_detail(job_id)

        listing_page = SimpleNamespace(freelance_jobs=[SimpleNamespace(id=job_id)])
        participants_page = SimpleNamespace(
            participants=[
                SimpleNamespace(id=2002, name="Contributor", state="Committed", contributed=50)
            ]
        )

        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsListing.return_value.results.return_value = [
            listing_page
        ]
        mock_esi.client.Freelance_Jobs.GetFreelanceJobsDetail.return_value.result.return_value = detail
        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsParticipants.return_value.results.return_value = [
            participants_page
        ]

        update_corp_freelance_jobs(self.owner.pk)

        job = FreelanceJob.objects.get(pk=job_id)
        self.assertEqual(job.name, "Test Job")
        self.assertEqual(job.state, FreelanceJob.State.ACTIVE)
        self.assertEqual(job.career, "Explorer")
        self.assertEqual(job.progress_current, 10)
        self.assertEqual(job.reward_remaining, 500.0)
        self.assertEqual(job.configuration_method, "BoostShield")
        self.assertEqual(job.configuration_parameters, {"matcher": {"values": []}})

        participant = FreelanceJobParticipant.objects.get(job=job, character_id=2002)
        self.assertEqual(participant.character_name, "Contributor")
        self.assertEqual(participant.contributed, 50)

        self.owner.refresh_from_db()
        self.assertIsNotNone(self.owner.last_update)

    @patch("freelance_tracker.tasks.esi")
    def test_skips_sync_without_a_valid_token(self, mock_esi):
        Token.objects.all().delete()

        update_corp_freelance_jobs(self.owner.pk)

        self.assertFalse(FreelanceJob.objects.exists())
        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsListing.assert_not_called()

    @patch("freelance_tracker.tasks.esi")
    def test_unchanged_listing_does_not_crash_the_task(self, mock_esi):
        """A 304 on the listing call must not abort the sync or skip last_update"""

        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsListing.return_value.results.side_effect = (
            HTTPNotModified(status_code=304, headers={})
        )

        update_corp_freelance_jobs(self.owner.pk)

        mock_esi.client.Freelance_Jobs.GetFreelanceJobsDetail.assert_not_called()
        self.owner.refresh_from_db()
        self.assertIsNotNone(self.owner.last_update)

    @patch("freelance_tracker.tasks.esi")
    def test_unchanged_job_detail_still_syncs_participants_from_stored_state(self, mock_esi):
        """A 304 on one job's detail must not abort the rest of the sync"""

        job_id = uuid.uuid4()
        FreelanceJob.objects.create(
            id=job_id, owner=self.owner, name="Already Known Job",
            state=FreelanceJob.State.ACTIVE,
            last_modified=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
        )

        listing_page = SimpleNamespace(freelance_jobs=[SimpleNamespace(id=job_id)])
        participants_page = SimpleNamespace(
            participants=[
                SimpleNamespace(id=2003, name="Still Contributing", state="Committed", contributed=99)
            ]
        )

        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsListing.return_value.results.return_value = [
            listing_page
        ]
        mock_esi.client.Freelance_Jobs.GetFreelanceJobsDetail.return_value.result.side_effect = (
            HTTPNotModified(status_code=304, headers={})
        )
        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsParticipants.return_value.results.return_value = [
            participants_page
        ]

        update_corp_freelance_jobs(self.owner.pk)

        # The job itself wasn't rewritten (still has its original name)...
        job = FreelanceJob.objects.get(pk=job_id)
        self.assertEqual(job.name, "Already Known Job")
        # ...but since we knew it was Active, participants still synced.
        participant = FreelanceJobParticipant.objects.get(job=job, character_id=2003)
        self.assertEqual(participant.contributed, 99)


@override_settings(STORAGES=_TEST_STORAGES)
class TestIndexView(TestCase):
    """Tests for views.index and its corp/alliance/faction visibility scoping"""

    @classmethod
    def setUpTestData(cls):
        cls.alliance = EveAllianceInfo.objects.create(
            alliance_id=99000001, alliance_name="Test Alliance", alliance_ticker="TSTA"
        )
        cls.faction = EveFactionInfo.objects.create(faction_id=500001, faction_name="Test Faction")

        # The user's own corp - also in the alliance, not in the faction.
        cls.corporation = EveCorporationInfo.objects.create(
            corporation_id=2345, corporation_name="Test Corp", corporation_ticker="TEST",
            alliance=cls.alliance,
        )
        cls.job = FreelanceJob.objects.create(
            id=uuid.uuid4(), owner=Owner.objects.create(corporation=cls.corporation),
            name="Own Corp Job", state=FreelanceJob.State.ACTIVE,
            last_modified=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
        )

        # A different corp in the same alliance.
        cls.alliance_mate_corporation = EveCorporationInfo.objects.create(
            corporation_id=3456, corporation_name="Alliance Mate Corp", corporation_ticker="MATE",
            alliance=cls.alliance,
        )
        cls.alliance_job = FreelanceJob.objects.create(
            id=uuid.uuid4(), owner=Owner.objects.create(corporation=cls.alliance_mate_corporation),
            name="Alliance Job", state=FreelanceJob.State.ACTIVE,
            last_modified=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
        )

        # A corp in the user's faction, unrelated to the alliance/own corp.
        cls.faction_corporation = EveCorporationInfo.objects.create(
            corporation_id=4567, corporation_name="Faction Corp", corporation_ticker="FACT",
            faction=cls.faction,
        )
        cls.faction_job = FreelanceJob.objects.create(
            id=uuid.uuid4(), owner=Owner.objects.create(corporation=cls.faction_corporation),
            name="Faction Job", state=FreelanceJob.State.ACTIVE,
            last_modified=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
        )

    def setUp(self):
        self.user = AuthUtils.create_member("member")
        main_character = AuthUtils.add_main_character_2(
            self.user, "Main Character", 1002,
            corp_id=self.corporation.corporation_id,
            corp_name=self.corporation.corporation_name,
            corp_ticker=self.corporation.corporation_ticker,
            alliance_id=self.alliance.alliance_id,
            alliance_name=self.alliance.alliance_name,
        )
        main_character.faction_id = self.faction.faction_id
        main_character.save()
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.basic_access", self.user)

    def test_denies_users_without_basic_access(self):
        no_access_user = AuthUtils.create_member("no_access")
        AuthUtils.add_main_character_2(no_access_user, "No Access", 1099)
        self.client.force_login(no_access_user)

        response = self.client.get(reverse("freelance_tracker:index"))

        self.assertEqual(response.status_code, 302)

    def test_shows_nothing_without_any_view_permission(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("freelance_tracker:index"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Own Corp Job")
        self.assertNotContains(response, "Alliance Job")
        self.assertNotContains(response, "Faction Job")

    def test_view_corp_shows_only_own_corporation_job(self):
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.view_corp", self.user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("freelance_tracker:index"))

        self.assertContains(response, "Own Corp Job")
        self.assertNotContains(response, "Alliance Job")
        self.assertNotContains(response, "Faction Job")

    def test_view_alliance_shows_jobs_from_alliance_corporations(self):
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.view_alliance", self.user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("freelance_tracker:index"))

        # Own corp is itself in the alliance, so it's included too.
        self.assertContains(response, "Own Corp Job")
        self.assertContains(response, "Alliance Job")
        self.assertNotContains(response, "Faction Job")

    def test_view_faction_shows_jobs_from_faction_corporations(self):
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.view_faction", self.user)
        self.client.force_login(self.user)

        response = self.client.get(reverse("freelance_tracker:index"))

        self.assertContains(response, "Faction Job")
        self.assertNotContains(response, "Own Corp Job")
        self.assertNotContains(response, "Alliance Job")


@override_settings(STORAGES=_TEST_STORAGES)
class TestJobDetailView(TestCase):
    """Tests for views.job_detail, including SDE-backed method/parameter labels"""

    @classmethod
    def setUpTestData(cls):
        cls.corporation = EveCorporationInfo.objects.create(
            corporation_id=2345, corporation_name="Test Corp", corporation_ticker="TEST"
        )
        cls.owner = Owner.objects.create(corporation=cls.corporation)
        cls.job = FreelanceJob.objects.create(
            id=uuid.uuid4(),
            owner=cls.owner,
            name="Deliver the Goods",
            state=FreelanceJob.State.ACTIVE,
            configuration_method="DeliverItem",
            configuration_parameters={
                "corporation_item_delivery": {
                    "corporation_item_delivery": {
                        "corporation_office_location": {"values": []},
                        "item_type": {"values": []},
                    }
                }
            },
            last_modified=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
        )

    def setUp(self):
        self.user = AuthUtils.create_member("member")
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.basic_access", self.user)
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.view_corp", self.user)
        AuthUtils.add_main_character_2(
            self.user, "Main Character", 1005,
            corp_id=self.corporation.corporation_id,
            corp_name=self.corporation.corporation_name,
            corp_ticker=self.corporation.corporation_ticker,
        )

    def test_shows_sde_labeled_method_and_parameters(self):
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("freelance_tracker:job_detail", args=[self.job.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Deliver")
        self.assertContains(response, "Item Type or Group")

    def test_404s_for_a_job_outside_the_users_visibility(self):
        other_corporation = EveCorporationInfo.objects.create(
            corporation_id=7788, corporation_name="Outside Corp", corporation_ticker="OUT"
        )
        other_job = FreelanceJob.objects.create(
            id=uuid.uuid4(),
            owner=Owner.objects.create(corporation=other_corporation),
            name="Not Your Job",
            state=FreelanceJob.State.ACTIVE,
            last_modified=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
        )
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("freelance_tracker:job_detail", args=[other_job.id])
        )

        self.assertEqual(response.status_code, 404)


class TestAddCorpView(TestCase):
    """Tests for views.add_corp permission gating"""

    def test_denies_users_without_add_corp_owner_permission(self):
        user = AuthUtils.create_member("no_perms")
        self.client.force_login(user)

        response = self.client.get(reverse("freelance_tracker:add_corp"))

        self.assertEqual(response.status_code, 302)


@override_settings(STORAGES=_TEST_STORAGES)
class TestMyJobsView(TestCase):
    """Tests for views.my_jobs"""

    def setUp(self):
        self.user = AuthUtils.create_member("member")
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.basic_access", self.user)
        # Every AA app URL is wrapped in main_character_required by UrlHook,
        # so a user without one gets redirected to the dashboard regardless
        # of app permissions.
        AuthUtils.add_main_character_2(
            self.user, "My Main", 1004, corp_id=2000, corp_name="Some Corp", corp_ticker="SOME"
        )

    def test_shows_empty_state_without_a_linked_character(self):
        self.client.force_login(self.user)

        response = self.client.get(reverse("freelance_tracker:my_jobs"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "haven't linked a character")

    @patch("freelance_tracker.views.esi")
    def test_shows_jobs_for_linked_character(self, mock_esi):
        _create_token(
            user=self.user,
            character_id=1003,
            character_name="My Character",
            scope_name="esi-characters.read_freelance_jobs.v1",
        )
        job_id = uuid.uuid4()
        listing = SimpleNamespace(freelance_jobs=[SimpleNamespace(id=job_id, name="My Active Job")])
        participation = SimpleNamespace(state="Committed", contributed=42)
        detail = _make_job_detail(job_id)

        mock_esi.client.Freelance_Jobs.GetCharactersFreelanceJobsListing.return_value.result.return_value = listing
        mock_esi.client.Freelance_Jobs.GetCharactersFreelanceJobsParticipation.return_value.result.return_value = participation
        mock_esi.client.Freelance_Jobs.GetFreelanceJobsDetail.return_value.result.return_value = detail

        self.client.force_login(self.user)
        response = self.client.get(reverse("freelance_tracker:my_jobs"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "My Active Job")
        self.assertContains(response, "My Character")
