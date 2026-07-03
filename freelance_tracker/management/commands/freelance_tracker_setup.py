"""Bootstrap the periodic sync task for the Freelance Tracker module"""

# Third Party
from django_celery_beat.models import CrontabSchedule, PeriodicTask

# Django
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Schedule the periodic freelance job sync task"

    def handle(self, *args, **options):
        self.stdout.write("Configuring periodic tasks...")

        schedule, _created = CrontabSchedule.objects.get_or_create(
            minute="0", hour="*", day_of_week="*", day_of_month="*", month_of_year="*",
            timezone="UTC",
        )

        PeriodicTask.objects.update_or_create(
            task="freelance_tracker.tasks.update_all_corp_freelance_jobs",
            defaults={
                "crontab": schedule,
                "name": "Freelance Tracker - Sync all corporations",
                "enabled": True,
            },
        )

        self.stdout.write(self.style.SUCCESS("Done! Freelance jobs will sync hourly."))
