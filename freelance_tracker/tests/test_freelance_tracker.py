"""Tests for the freelance_tracker app"""

# Standard Library
import inspect
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

# Django
from django.contrib.messages.storage.fallback import FallbackStorage
from django.contrib.sessions.middleware import SessionMiddleware
from django.core.cache import cache
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

# Alliance Auth
from allianceauth.authentication.models import CharacterOwnership
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
from freelance_tracker import views
from freelance_tracker.models import FreelanceJob, FreelanceJobParticipant, Owner
from freelance_tracker.tasks import _sync_lock_key, update_corp_freelance_jobs

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
        # Relative to now (not a fixed date) so it stays within the
        # participant-sync grace period regardless of when tests run.
        expires=timezone.now() + timedelta(days=7),
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
        # ...but participants are always rechecked regardless, since
        # contributions can change independently of the job detail.
        participant = FreelanceJobParticipant.objects.get(job=job, character_id=2003)
        self.assertEqual(participant.contributed, 99)

    @patch("freelance_tracker.tasks.esi")
    def test_syncs_participants_regardless_of_job_state(self, mock_esi):
        """Contributions can still change between state changes, e.g. shortly before Completed"""

        job_id = uuid.uuid4()
        detail = _make_job_detail(job_id)
        detail.state = "Completed"
        detail.details.finished = datetime(2026, 1, 10, tzinfo=dt_timezone.utc)
        detail.model_dump = lambda mode=None: {
            "configuration": {
                "method": "BoostShield", "version": 1, "parameters": {"matcher": {"values": []}},
            },
            "contribution": {"max_committed_participants": 10000},
            "access_and_visibility": {"acl_protected": False},
        }

        listing_page = SimpleNamespace(freelance_jobs=[SimpleNamespace(id=job_id)])
        participants_page = SimpleNamespace(
            participants=[
                SimpleNamespace(id=2004, name="Late Contributor", state="Committed", contributed=7)
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
        self.assertEqual(job.state, FreelanceJob.State.COMPLETED)
        participant = FreelanceJobParticipant.objects.get(job=job, character_id=2004)
        self.assertEqual(participant.contributed, 7)

    @patch("freelance_tracker.tasks.esi")
    def test_stops_syncing_participants_more_than_48h_after_expiry(self, mock_esi):
        job_id = uuid.uuid4()
        detail = _make_job_detail(job_id)
        detail.details.expires = timezone.now() - timedelta(hours=49)

        listing_page = SimpleNamespace(freelance_jobs=[SimpleNamespace(id=job_id)])
        participants_page = SimpleNamespace(
            participants=[
                SimpleNamespace(id=2005, name="Too Late", state="Committed", contributed=1)
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

        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsParticipants.assert_not_called()
        self.assertFalse(FreelanceJobParticipant.objects.filter(job_id=job_id).exists())

    @patch("freelance_tracker.tasks.esi")
    def test_force_still_syncs_participants_past_the_grace_period(self, mock_esi):
        job_id = uuid.uuid4()
        detail = _make_job_detail(job_id)
        detail.details.expires = timezone.now() - timedelta(hours=49)

        listing_page = SimpleNamespace(freelance_jobs=[SimpleNamespace(id=job_id)])
        participants_page = SimpleNamespace(
            participants=[
                SimpleNamespace(id=2006, name="Forced Check", state="Committed", contributed=1)
            ]
        )

        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsListing.return_value.results.return_value = [
            listing_page
        ]
        mock_esi.client.Freelance_Jobs.GetFreelanceJobsDetail.return_value.result.return_value = detail
        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsParticipants.return_value.results.return_value = [
            participants_page
        ]

        update_corp_freelance_jobs(self.owner.pk, force=True)

        participant = FreelanceJobParticipant.objects.get(job_id=job_id, character_id=2006)
        self.assertEqual(participant.contributed, 1)

    @patch("freelance_tracker.tasks.esi")
    def test_second_sync_is_skipped_while_a_sync_is_already_in_progress(self, mock_esi):
        """A concurrent trigger (periodic + manual, say) must not race the first sync"""

        cache.set(_sync_lock_key(self.owner.pk), True, timeout=60)

        result = update_corp_freelance_jobs(self.owner.pk)

        self.assertIsNone(result)
        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsListing.assert_not_called()

    @patch("freelance_tracker.tasks.esi")
    def test_lock_is_released_once_sync_completes(self, mock_esi):
        job_id = uuid.uuid4()
        detail = _make_job_detail(job_id)

        listing_page = SimpleNamespace(freelance_jobs=[SimpleNamespace(id=job_id)])
        participants_page = SimpleNamespace(participants=[])

        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsListing.return_value.results.return_value = [
            listing_page
        ]
        mock_esi.client.Freelance_Jobs.GetFreelanceJobsDetail.return_value.result.return_value = detail
        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsParticipants.return_value.results.return_value = [
            participants_page
        ]

        update_corp_freelance_jobs(self.owner.pk)

        self.assertIsNone(cache.get(_sync_lock_key(self.owner.pk)))

    @patch("freelance_tracker.tasks.esi")
    def test_lock_is_released_when_sync_is_skipped_for_no_token(self, mock_esi):
        Token.objects.all().delete()

        update_corp_freelance_jobs(self.owner.pk)

        self.assertIsNone(cache.get(_sync_lock_key(self.owner.pk)))


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

    fixtures = ["freelance_tracker_sde"]

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
        FreelanceJobParticipant.objects.create(
            job=cls.job,
            character_id=1005,
            character_name="Main Character",
            state=FreelanceJobParticipant.State.COMMITTED,
            contributed=42,
        )

    def setUp(self):
        self.user = AuthUtils.create_member("member")
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.basic_access", self.user)
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.view_corp", self.user)
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.view_participants", self.user)
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

    def test_shows_any_for_an_empty_matcher_values_list(self):
        """An empty `values` list means "unrestricted", not "nothing set" """

        self.client.force_login(self.user)

        response = self.client.get(
            reverse("freelance_tracker:job_detail", args=[self.job.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Any")

    def test_shows_participants_with_view_participants_permission(self):
        self.client.force_login(self.user)

        response = self.client.get(
            reverse("freelance_tracker:job_detail", args=[self.job.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Main Character")

    def test_hides_participants_without_view_participants_permission(self):
        user = AuthUtils.create_member("no_participants_perm")
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.basic_access", user)
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.view_corp", user)
        AuthUtils.add_main_character_2(
            user, "Other Character", 1006,
            corp_id=self.corporation.corporation_id,
            corp_name=self.corporation.corporation_name,
            corp_ticker=self.corporation.corporation_ticker,
        )
        self.client.force_login(user)

        response = self.client.get(
            reverse("freelance_tracker:job_detail", args=[self.job.id])
        )

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Main Character")
        self.assertNotContains(response, "Participants")

    def test_resolves_a_solarsystem_parameter_value_to_its_name(self):
        """A `location`/`solarsystem` value must render as a name, not a bare id"""

        job = FreelanceJob.objects.create(
            id=uuid.uuid4(),
            owner=self.owner,
            name="Boost My Shield",
            state=FreelanceJob.State.ACTIVE,
            configuration_method="BoostShield",
            configuration_parameters={
                "location": {
                    "matcher": {
                        "values": [{"value_type": "solarsystem", "values": ["30000142"]}]
                    }
                }
            },
            last_modified=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse("freelance_tracker:job_detail", args=[job.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Jita")
        self.assertNotContains(response, "30000142")

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

    def test_reactivates_a_previously_disabled_owner(self):
        """Re-linking via 'Add / Refresh Corp Token' must undo a prior is_active=False

        Otherwise an owner disabled (e.g. after a broken token) stays
        permanently excluded from the hourly periodic sync even after a
        director successfully refreshes its token.
        """

        corporation = EveCorporationInfo.objects.create(
            corporation_id=5566, corporation_name="Some Corp", corporation_ticker="SOME"
        )
        owner = Owner.objects.create(corporation=corporation, is_active=False)

        director_character_id = 1010
        EveCharacter.objects.create(
            character_id=director_character_id,
            character_name="Director Guy",
            corporation_id=corporation.corporation_id,
            corporation_name=corporation.corporation_name,
            corporation_ticker=corporation.corporation_ticker,
        )
        token = SimpleNamespace(character_id=director_character_id)

        user = AuthUtils.create_member("director")
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.add_corp_owner", user)

        request = RequestFactory().get("/freelance-tracker/add-corp/")
        request.user = user
        SessionMiddleware(lambda r: None).process_request(request)
        request.session.save()
        request._messages = FallbackStorage(request)

        raw_add_corp = inspect.unwrap(views.add_corp)
        with patch("freelance_tracker.views.update_corp_freelance_jobs") as mock_update:
            response = raw_add_corp(request, token)

        self.assertEqual(response.status_code, 302)
        owner.refresh_from_db()
        self.assertTrue(owner.is_active)
        mock_update.assert_called_once_with(owner.pk)


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

    @patch("freelance_tracker.views.esi")
    def test_unchanged_listing_is_skipped_instead_of_crashing(self, mock_esi):
        """A 304 with no local cache fallback must not 500 the page"""

        _create_token(
            user=self.user,
            character_id=1006,
            character_name="No Change Character",
            scope_name="esi-characters.read_freelance_jobs.v1",
        )
        mock_esi.client.Freelance_Jobs.GetCharactersFreelanceJobsListing.return_value.result.side_effect = (
            HTTPNotModified(status_code=304, headers={})
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("freelance_tracker:my_jobs"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "None of your linked characters are participating")

    @patch("freelance_tracker.views.esi")
    def test_unchanged_job_detail_is_skipped_instead_of_crashing(self, mock_esi):
        """A 304 on a job's detail/participation must not 500 the whole page"""

        _create_token(
            user=self.user,
            character_id=1007,
            character_name="Stale Job Character",
            scope_name="esi-characters.read_freelance_jobs.v1",
        )
        job_id = uuid.uuid4()
        listing = SimpleNamespace(
            freelance_jobs=[SimpleNamespace(id=job_id, name="Stale Job")]
        )

        mock_esi.client.Freelance_Jobs.GetCharactersFreelanceJobsListing.return_value.result.return_value = listing
        mock_esi.client.Freelance_Jobs.GetCharactersFreelanceJobsParticipation.return_value.result.side_effect = (
            HTTPNotModified(status_code=304, headers={})
        )

        self.client.force_login(self.user)
        response = self.client.get(reverse("freelance_tracker:my_jobs"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Stale Job")


@override_settings(STORAGES=_TEST_STORAGES)
class TestLeaderboardView(TestCase):
    """Tests for views.leaderboard"""

    @classmethod
    def setUpTestData(cls):
        cls.corporation = EveCorporationInfo.objects.create(
            corporation_id=3456, corporation_name="Leaderboard Corp", corporation_ticker="LEAD"
        )
        cls.owner = Owner.objects.create(corporation=cls.corporation)

    def setUp(self):
        self.viewer = AuthUtils.create_member("viewer")
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.basic_access", self.viewer)
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.view_corp", self.viewer)
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.view_participants", self.viewer)
        AuthUtils.add_main_character_2(
            self.viewer, "Viewer Main", 2001,
            corp_id=self.corporation.corporation_id,
            corp_name=self.corporation.corporation_name,
            corp_ticker=self.corporation.corporation_ticker,
        )

    def _make_job(self, career, created):
        return FreelanceJob.objects.create(
            id=uuid.uuid4(),
            owner=self.owner,
            name=f"{career} job",
            state=FreelanceJob.State.ACTIVE,
            career=career,
            configuration_method="",
            configuration_parameters={},
            created=created,
            last_modified=created,
        )

    def test_hides_contributed_column_without_view_participants_permission(self):
        no_perm_viewer = AuthUtils.create_member("no_contrib_perm")
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.basic_access", no_perm_viewer)
        AuthUtils.add_permission_to_user_by_name("freelance_tracker.view_corp", no_perm_viewer)
        AuthUtils.add_main_character_2(
            no_perm_viewer, "No Perm Viewer", 2002,
            corp_id=self.corporation.corporation_id,
            corp_name=self.corporation.corporation_name,
            corp_ticker=self.corporation.corporation_ticker,
        )
        job = self._make_job(FreelanceJob.Career.ENFORCER, timezone.now())
        FreelanceJobParticipant.objects.create(
            job=job, character_id=9001, character_name="Hidden Contributor", contributed=42,
        )

        self.client.force_login(no_perm_viewer)
        response = self.client.get(reverse("freelance_tracker:leaderboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Hidden Contributor")
        self.assertNotContains(response, "Contributed")
        self.assertNotContains(response, ">42<")

    def test_shows_contributed_column_with_view_participants_permission(self):
        job = self._make_job(FreelanceJob.Career.ENFORCER, timezone.now())
        FreelanceJobParticipant.objects.create(
            job=job, character_id=9002, character_name="Visible Contributor", contributed=42,
        )

        self.client.force_login(self.viewer)
        response = self.client.get(reverse("freelance_tracker:leaderboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Visible Contributor")
        self.assertContains(response, "Contributed")
        self.assertContains(response, ">42<")

    def test_rolls_up_alt_participations_under_their_main_character(self):
        """An alt's participations must count toward its owner's main character"""

        main_char = AuthUtils.add_main_character_2(
            AuthUtils.create_member("grinder"), "Grinder Main", 3001,
            corp_id=self.corporation.corporation_id,
            corp_name=self.corporation.corporation_name,
            corp_ticker=self.corporation.corporation_ticker,
        )
        CharacterOwnership.objects.create(
            character=main_char, user=main_char.userprofile.user, owner_hash="main-hash",
        )
        alt_char = EveCharacter.objects.create(
            character_id=3002, character_name="Grinder Alt",
            corporation_id=self.corporation.corporation_id,
            corporation_name=self.corporation.corporation_name,
            corporation_ticker=self.corporation.corporation_ticker,
        )
        CharacterOwnership.objects.create(
            character=alt_char, user=main_char.userprofile.user, owner_hash="alt-hash",
        )

        job = self._make_job(FreelanceJob.Career.ENFORCER, timezone.now())
        FreelanceJobParticipant.objects.create(
            job=job, character_id=3001, character_name="Grinder Main", contributed=5,
        )
        FreelanceJobParticipant.objects.create(
            job=job, character_id=3002, character_name="Grinder Alt", contributed=7,
        )

        self.client.force_login(self.viewer)
        response = self.client.get(reverse("freelance_tracker:leaderboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Grinder Main")
        self.assertContains(response, ">12<")  # sum of both contributions rolled up under one row
        self.assertNotContains(response, "Grinder Alt")

    def test_shows_corporation_and_alliance_ticker_for_resolved_main_character(self):
        alliance = EveAllianceInfo.objects.create(
            alliance_id=99001, alliance_name="Test Alliance", alliance_ticker="TSTA",
        )
        main_char = AuthUtils.add_main_character_2(
            AuthUtils.create_member("ticker_test"), "Ticker Main", 3101,
            corp_id=self.corporation.corporation_id,
            corp_name=self.corporation.corporation_name,
            corp_ticker=self.corporation.corporation_ticker,
            alliance_id=alliance.alliance_id,
            alliance_name=alliance.alliance_name,
        )
        main_char.alliance_ticker = alliance.alliance_ticker
        main_char.save()
        CharacterOwnership.objects.create(
            character=main_char, user=main_char.userprofile.user, owner_hash="ticker-hash",
        )

        job = self._make_job(FreelanceJob.Career.ENFORCER, timezone.now())
        FreelanceJobParticipant.objects.create(
            job=job, character_id=3101, character_name="Ticker Main", contributed=1,
        )

        self.client.force_login(self.viewer)
        response = self.client.get(reverse("freelance_tracker:leaderboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ticker Main")
        self.assertContains(response, "[LEAD]")
        self.assertContains(response, "&lt;TSTA&gt;")

    def test_falls_back_to_character_name_when_unlinked(self):
        """A participant with no linked user shows their own character name"""

        job = self._make_job(FreelanceJob.Career.INDUSTRIALIST, timezone.now())
        FreelanceJobParticipant.objects.create(
            job=job, character_id=4001, character_name="Unlinked Miner", contributed=1,
        )

        self.client.force_login(self.viewer)
        response = self.client.get(reverse("freelance_tracker:leaderboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Unlinked Miner")

    def test_excludes_contributors_with_zero_total(self):
        job = self._make_job(FreelanceJob.Career.INDUSTRIALIST, timezone.now())
        FreelanceJobParticipant.objects.create(
            job=job, character_id=4002, character_name="Zero Contributor", contributed=0,
        )
        FreelanceJobParticipant.objects.create(
            job=job, character_id=4003, character_name="Real Contributor", contributed=1,
        )

        self.client.force_login(self.viewer)
        response = self.client.get(reverse("freelance_tracker:leaderboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Real Contributor")
        self.assertNotContains(response, "Zero Contributor")

    def test_separates_leaderboards_by_career_and_excludes_unspecified(self):
        enforcer_job = self._make_job(FreelanceJob.Career.ENFORCER, timezone.now())
        industrialist_job = self._make_job(FreelanceJob.Career.INDUSTRIALIST, timezone.now())
        unspecified_job = self._make_job(FreelanceJob.Career.UNSPECIFIED, timezone.now())

        FreelanceJobParticipant.objects.create(
            job=enforcer_job, character_id=5001, character_name="Cop", contributed=1,
        )
        FreelanceJobParticipant.objects.create(
            job=industrialist_job, character_id=5002, character_name="Builder", contributed=1,
        )
        FreelanceJobParticipant.objects.create(
            job=unspecified_job, character_id=5003, character_name="Mystery", contributed=1,
        )

        self.client.force_login(self.viewer)
        response = self.client.get(reverse("freelance_tracker:leaderboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Cop")
        self.assertContains(response, "Builder")
        self.assertNotContains(response, "Mystery")

    def test_excludes_jobs_outside_the_day_window(self):
        old_job = self._make_job(
            FreelanceJob.Career.ENFORCER, timezone.now() - timedelta(days=40)
        )
        FreelanceJobParticipant.objects.create(
            job=old_job, character_id=6001, character_name="Old Timer", contributed=1,
        )

        self.client.force_login(self.viewer)
        response = self.client.get(reverse("freelance_tracker:leaderboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No contributions in this window.")
        self.assertNotContains(response, "Old Timer")

    def test_days_query_param_narrows_the_window(self):
        job = self._make_job(
            FreelanceJob.Career.ENFORCER, timezone.now() - timedelta(days=10)
        )
        FreelanceJobParticipant.objects.create(
            job=job, character_id=7001, character_name="Recent Enforcer", contributed=1,
        )

        self.client.force_login(self.viewer)

        response = self.client.get(reverse("freelance_tracker:leaderboard"), {"days": 30})
        self.assertContains(response, "Recent Enforcer")

        response = self.client.get(reverse("freelance_tracker:leaderboard"), {"days": 1})
        self.assertNotContains(response, "Recent Enforcer")

    def test_limits_each_board_to_the_top_15(self):
        job = self._make_job(FreelanceJob.Career.ENFORCER, timezone.now())
        for i in range(16):
            FreelanceJobParticipant.objects.create(
                job=job, character_id=8000 + i, character_name=f"Rank {i}", contributed=i + 1,
            )

        self.client.force_login(self.viewer)
        response = self.client.get(reverse("freelance_tracker:leaderboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Rank 15")  # highest contributed (16), rank 1
        self.assertNotContains(response, "Rank 0")  # lowest contributed (1), 16th place - cut off


@override_settings(STORAGES=_TEST_STORAGES)
class TestFreelanceJobAdminRawEsi(TestCase):
    """Tests for FreelanceJobAdmin's live/uncached raw ESI data view"""

    @classmethod
    def setUpTestData(cls):
        cls.corporation = EveCorporationInfo.objects.create(
            corporation_id=4567, corporation_name="Admin Corp", corporation_ticker="ADMN"
        )
        cls.owner = Owner.objects.create(corporation=cls.corporation)
        cls.job = FreelanceJob.objects.create(
            id=uuid.uuid4(), owner=cls.owner, name="Admin Test Job",
            state=FreelanceJob.State.ACTIVE,
            last_modified=datetime(2026, 1, 1, tzinfo=dt_timezone.utc),
        )

    def setUp(self):
        self.admin_user = AuthUtils.create_member("admin_user")
        self.admin_user.is_staff = True
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.raw_esi_url = reverse(
            "admin:freelance_tracker_freelancejob_raw_esi", args=[self.job.pk]
        )

    @patch("freelance_tracker.admin.esi")
    @patch("freelance_tracker.admin._get_owner_token")
    def test_shows_raw_esi_json_bypassing_cache(self, mock_get_token, mock_esi):
        mock_get_token.return_value = SimpleNamespace(character_id=1001)
        detail = SimpleNamespace(model_dump=lambda mode=None: {"id": str(self.job.pk), "name": "Live Name"})
        mock_esi.client.Freelance_Jobs.GetFreelanceJobsDetail.return_value.result.return_value = detail

        self.client.force_login(self.admin_user)
        response = self.client.get(self.raw_esi_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Live Name")
        mock_esi.client.Freelance_Jobs.GetFreelanceJobsDetail.return_value.result.assert_called_once_with(
            force_refresh=True
        )

    @patch("freelance_tracker.admin._get_owner_token")
    def test_redirects_with_an_error_when_no_token_is_available(self, mock_get_token):
        mock_get_token.return_value = None

        self.client.force_login(self.admin_user)
        response = self.client.get(self.raw_esi_url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No valid freelance-jobs ESI token found")

    @patch("freelance_tracker.admin.esi")
    @patch("freelance_tracker.admin._get_owner_token")
    def test_redirects_with_an_error_when_esi_fails(self, mock_get_token, mock_esi):
        mock_get_token.return_value = SimpleNamespace(character_id=1001)
        mock_esi.client.Freelance_Jobs.GetFreelanceJobsDetail.return_value.result.side_effect = (
            Exception("ESI is down")
        )

        self.client.force_login(self.admin_user)
        response = self.client.get(self.raw_esi_url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ESI request failed")

    def test_denies_non_staff_users(self):
        non_staff = AuthUtils.create_member("not_staff")
        self.client.force_login(non_staff)

        response = self.client.get(self.raw_esi_url)

        self.assertEqual(response.status_code, 302)


@override_settings(STORAGES=_TEST_STORAGES)
class TestOwnerAdminRawEsi(TestCase):
    """Tests for OwnerAdmin's live/uncached raw ESI job listing view"""

    @classmethod
    def setUpTestData(cls):
        cls.corporation = EveCorporationInfo.objects.create(
            corporation_id=4568, corporation_name="Owner Admin Corp", corporation_ticker="OADM"
        )
        cls.owner = Owner.objects.create(corporation=cls.corporation)

    def setUp(self):
        self.admin_user = AuthUtils.create_member("owner_admin_user")
        self.admin_user.is_staff = True
        self.admin_user.is_superuser = True
        self.admin_user.save()
        self.raw_esi_url = reverse("admin:freelance_tracker_owner_raw_esi", args=[self.owner.pk])

    @patch("freelance_tracker.admin.esi")
    @patch("freelance_tracker.admin._get_owner_token")
    def test_shows_raw_esi_json_bypassing_cache(self, mock_get_token, mock_esi):
        mock_get_token.return_value = SimpleNamespace(character_id=1001)
        page = SimpleNamespace(model_dump=lambda mode=None: {"freelance_jobs": [{"id": "live-job"}]})
        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsListing.return_value.results.return_value = [
            page
        ]

        self.client.force_login(self.admin_user)
        response = self.client.get(self.raw_esi_url)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "live-job")
        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsListing.return_value.results.assert_called_once_with(
            after="0", force_refresh=True
        )

    @patch("freelance_tracker.admin._get_owner_token")
    def test_redirects_with_an_error_when_no_token_is_available(self, mock_get_token):
        mock_get_token.return_value = None

        self.client.force_login(self.admin_user)
        response = self.client.get(self.raw_esi_url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No valid freelance-jobs ESI token found")

    @patch("freelance_tracker.admin.esi")
    @patch("freelance_tracker.admin._get_owner_token")
    def test_redirects_with_an_error_when_esi_fails(self, mock_get_token, mock_esi):
        mock_get_token.return_value = SimpleNamespace(character_id=1001)
        mock_esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsListing.return_value.results.side_effect = (
            Exception("ESI is down")
        )

        self.client.force_login(self.admin_user)
        response = self.client.get(self.raw_esi_url, follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ESI request failed")

    def test_denies_non_staff_users(self):
        non_staff = AuthUtils.create_member("owner_not_staff")
        self.client.force_login(non_staff)

        response = self.client.get(self.raw_esi_url)

        self.assertEqual(response.status_code, 302)
