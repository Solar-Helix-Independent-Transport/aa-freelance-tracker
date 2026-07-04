"""eve_sde test data spec for this app.

See https://github.com/Solar-Helix-Independent-Transport/django-eveonline-sde/blob/master/docs/test_data.md
Regenerate with: python manage.py esde_generate_test_data freelance_tracker
"""

# Third Party
# Django EVE SDE
from eve_sde.test_data import ModelSpec

testdata_spec: list[ModelSpec] = [
    ModelSpec("FreelanceJobSchema", ids=["BoostShield", "DeliverItem"]),
    ModelSpec("FreelanceJobSchemaParameter", ids=["BoostShield", "DeliverItem"], field="schema_id"),
    ModelSpec("SolarSystem", ids=[30000142]),  # Jita
]
