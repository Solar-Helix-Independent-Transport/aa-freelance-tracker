"""App URLs"""

# Django
from django.urls import path

# AA Freelance Tracker
from freelance_tracker import views

app_name: str = "freelance_tracker"  # pylint: disable=invalid-name

urlpatterns = [
    path("", views.index, name="index"),
    path("job/<uuid:job_id>/", views.job_detail, name="job_detail"),
    path("my-jobs/", views.my_jobs, name="my_jobs"),
    path("leaderboard/", views.leaderboard, name="leaderboard"),
    path("add-corp/", views.add_corp, name="add_corp"),
    path("add-character/", views.add_character, name="add_character"),
]
