"""Admin models"""

# Django
from django.contrib import admin

# AA Freelance Tracker
from freelance_tracker.models import FreelanceJob, FreelanceJobParticipant, Owner
from freelance_tracker.tasks import update_corp_freelance_jobs


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

    @admin.action(description="Force a full resync from ESI (bypasses cache)")
    def force_resync(self, request, queryset):
        for owner in queryset:
            update_corp_freelance_jobs(owner.pk, force=True)
        self.message_user(request, f"Queued a forced resync for {queryset.count()} owner(s).")


@admin.register(FreelanceJob)
class FreelanceJobAdmin(admin.ModelAdmin):
    """Admin for FreelanceJob"""

    list_display = ("name", "owner", "career", "state", "expires", "last_modified")
    list_filter = ("owner", "state", "career")
    search_fields = ("name",)
    inlines = (FreelanceJobParticipantInline,)

    def has_add_permission(self, request):
        return False
