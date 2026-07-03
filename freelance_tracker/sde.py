"""Static SDE-derived reference data for Freelance Job contribution methods.

Bundled from the EVE SDE's freelance job contribution method definitions
(freelance_tracker/data/freelance_job_methods.json). Used to turn a job's raw
`configuration_method` / `configuration_parameters` into human-readable labels.
"""

# Standard Library
import json
from functools import lru_cache
from pathlib import Path

# Django
from django.utils.translation import get_language

_DATA_PATH = Path(__file__).resolve().parent / "data" / "freelance_job_methods.json"


@lru_cache(maxsize=1)
def _methods() -> dict:
    """Contribution method definitions, keyed by method name (e.g. 'BoostShield')"""

    with _DATA_PATH.open(encoding="utf-8") as data_file:
        raw = json.load(data_file)

    return {method["_key"]: method for method in raw["_value"]}


def _localized(text: dict | None, lang: str | None = None) -> str:
    if not text:
        return ""

    lang = (lang or get_language() or "en").split("-")[0]

    return text.get(lang) or text.get("en") or next(iter(text.values()), "")


def get_method(method: str) -> dict | None:
    """Raw SDE definition for a contribution method, or None if unknown"""

    return _methods().get(method)


def get_method_title(method: str, lang: str | None = None) -> str:
    """Human-readable title for a contribution method, falling back to the raw key"""

    data = get_method(method)

    return _localized(data.get("title"), lang) if data else method


def get_method_description(method: str, lang: str | None = None) -> str:
    """Human-readable description for a contribution method"""

    data = get_method(method)

    return _localized(data.get("description"), lang) if data else ""


def describe_parameters(method: str, parameters: dict, lang: str | None = None) -> list[dict]:
    """Label a job's raw configuration_parameters using the SDE parameter definitions.

    Each parameter value is one of 4 ESI-defined shapes (matcher/options/boolean/
    corporation_item_delivery), keyed by which "kind" it is. We label the parameter
    itself from the SDE; the underlying value (location IDs, ship type IDs, etc.)
    is passed through raw, since resolving those to names needs a separate
    ESI/SDE type lookup.
    """

    data = get_method(method)
    param_defs = {p["_key"]: p for p in data.get("parameters", [])} if data else {}

    # ESI's runtime `oneOf` kind name doesn't always match the SDE's field name
    # for the same variant (matcher/boolean do; corporation_item_delivery is
    # named "itemDelivery" in the SDE).
    sde_kind_names = {"corporation_item_delivery": "itemDelivery"}

    rows = []
    for key, value in (parameters or {}).items():
        param_def = param_defs.get(key, {})
        kind = next(iter(value), None) if isinstance(value, dict) else None
        kind_def = param_def.get(sde_kind_names.get(kind, kind), {}) if kind else {}

        rows.append(
            {
                "key": key,
                "title": _localized(kind_def.get("title") or param_def.get("title"), lang) or key,
                "value": value.get(kind) if kind and isinstance(value, dict) else value,
            }
        )

    return rows
