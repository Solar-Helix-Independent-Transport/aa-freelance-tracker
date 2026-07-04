"""Admin models"""

# Standard Library
import json

# Django
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.urls import path, reverse
from django.utils.html import format_html

# AA Freelance Tracker
from freelance_tracker.models import FreelanceJob, FreelanceJobParticipant, Owner
from freelance_tracker.providers import esi
from freelance_tracker.tasks import _get_owner_token, update_corp_freelance_jobs


def _raw_esi_link(url) -> str:
    return format_html('<a href="{}" target="_blank">Fetch live from ESI (bypasses cache)</a>', url)


def _render_raw_esi(admin_obj, request, owner, change_url, object_label, fetch):
    """Fetch something live from ESI (bypassing cache) via `fetch(token)` and
    display it, or redirect back to `change_url` with an error message.

    Shared by FreelanceJobAdmin (one job's detail) and OwnerAdmin (a corp's
    full job listing) - only what's fetched and `object_label` (used for the
    breadcrumb back to `change_url`) differs.
    """

    token = _get_owner_token(owner)
    if token is None:
        admin_obj.message_user(
            request,
            f"No valid freelance-jobs ESI token found for {owner.corporation}.",
            level=messages.ERROR,
        )
        return HttpResponseRedirect(change_url)

    try:
        raw_json = json.dumps(fetch(token), indent=2, sort_keys=True)
    except Exception as e:  # - surface any ESI failure to the admin, don't 500
        admin_obj.message_user(request, f"ESI request failed: {e}", level=messages.ERROR)
        return HttpResponseRedirect(change_url)

    context = {
        **admin_obj.admin_site.each_context(request),
        "title": f"Raw ESI data: {object_label}",
        "object_label": object_label,
        "change_url": change_url,
        "raw_json": raw_json,
        "opts": admin_obj.model._meta,
    }
    return render(request, "admin/freelance_tracker/raw_esi.html", context)


class FreelanceJobParticipantInline(admin.TabularInline):
    """Inline participants under a FreelanceJob"""

    model = FreelanceJobParticipant
    extra = 0
    readonly_fields = ("character_id", "character_name", "state", "contributed")
    can_delete = False


@admin.register(Owner)
class OwnerAdmin(admin.ModelAdmin):
    """Admin for Owner"""

    list_display = ("corporation", "is_active", "last_update")
    list_filter = ("is_active",)
    actions = ("force_resync",)
    readonly_fields = ("raw_esi_link",)

    @admin.action(description="Force a full resync from ESI (bypasses cache)")
    def force_resync(self, request, queryset):
        for owner in queryset:
            update_corp_freelance_jobs(owner.pk, force=True)
        self.message_user(request, f"Queued a forced resync for {queryset.count()} owner(s).")

    def get_urls(self):
        urls = [
            path(
                "<path:object_id>/raw-esi/",
                self.admin_site.admin_view(self.raw_esi_view),
                name="freelance_tracker_owner_raw_esi",
            ),
        ]
        return urls + super().get_urls()

    @admin.display(description="Raw ESI data")
    def raw_esi_link(self, obj):
        """Link to a live, uncached fetch of this owner's full job listing"""

        if not obj.pk:
            return ""

        return _raw_esi_link(reverse("admin:freelance_tracker_owner_raw_esi", args=[obj.pk]))

    def raw_esi_view(self, request, object_id):
        """Fetch a corp's full freelance job listing straight from ESI (force_refresh)"""

        owner = self.get_object(request, object_id)
        change_url = reverse("admin:freelance_tracker_owner_change", args=[object_id])

        if owner is None or not self.has_view_permission(request, owner):
            self.message_user(request, "Owner not found.", level=messages.ERROR)
            return HttpResponseRedirect(reverse("admin:freelance_tracker_owner_changelist"))

        def fetch(token):
            pages = esi.client.Freelance_Jobs.GetCorporationsFreelanceJobsListing(
                corporation_id=owner.corporation.corporation_id, token=token,
            ).results(after="0", force_refresh=True)
            return [page.model_dump(mode="json") for page in pages]

        return _render_raw_esi(
            self, request, owner, change_url,
            f"{owner.corporation} freelance jobs listing",
            fetch,
        )


@admin.register(FreelanceJob)
class FreelanceJobAdmin(admin.ModelAdmin):
    """Admin for FreelanceJob"""

    list_display = ("name", "owner", "career", "state", "expires", "last_modified")
    list_filter = ("owner", "state", "career")
    search_fields = ("name",)
    inlines = (FreelanceJobParticipantInline,)
    readonly_fields = ("raw_esi_link",)

    def has_add_permission(self, request):
        return False

    def get_urls(self):
        urls = [
            path(
                "<path:object_id>/raw-esi/",
                self.admin_site.admin_view(self.raw_esi_view),
                name="freelance_tracker_freelancejob_raw_esi",
            ),
        ]
        return urls + super().get_urls()

    @admin.display(description="Raw ESI data")
    def raw_esi_link(self, obj):
        """Link to a live, uncached fetch of this job's raw ESI JSON"""

        if not obj.pk:
            return ""

        return _raw_esi_link(reverse("admin:freelance_tracker_freelancejob_raw_esi", args=[obj.pk]))

    def raw_esi_view(self, request, object_id):
        """Fetch a job's detail straight from ESI (force_refresh) and display it as-is"""

        job = self.get_object(request, object_id)
        change_url = reverse("admin:freelance_tracker_freelancejob_change", args=[object_id])

        if job is None or not self.has_view_permission(request, job):
            self.message_user(request, "Freelance job not found.", level=messages.ERROR)
            return HttpResponseRedirect(reverse("admin:freelance_tracker_freelancejob_changelist"))

        def fetch(token):
            detail = esi.client.Freelance_Jobs.GetFreelanceJobsDetail(
                job_id=job.pk, token=token,
            ).result(force_refresh=True)
            return detail.model_dump(mode="json")

        return _render_raw_esi(self, request, job.owner, change_url, job.name, fetch)
