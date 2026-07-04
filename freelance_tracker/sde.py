"""SDE-derived reference data for Freelance Job contribution methods.

Backed by `eve_sde`'s `FreelanceJobSchema`/`FreelanceJobSchemaParameter`
models (imported from the EVE SDE, kept up to date by that app's own sync
task). Used to turn a job's raw `configuration_method` /
`configuration_parameters` into human-readable labels.
"""

# Third Party
# Django EVE SDE
from eve_sde.models import FreelanceJobSchema, FreelanceJobSchemaParameter

# AA Freelance Tracker
from freelance_tracker.resolve import resolve_values


def get_method(method: str) -> FreelanceJobSchema | None:
    """SDE schema for a contribution method, or None if unknown"""

    return FreelanceJobSchema.objects.filter(pk=method).first()


def get_method_title(method: str) -> str:
    """Human-readable title for a contribution method, falling back to the raw key"""

    schema = get_method(method)

    return schema.title if schema else method


def get_method_description(method: str) -> str:
    """Human-readable description for a contribution method"""

    schema = get_method(method)

    return schema.description or "" if schema else ""


def _iter_value_groups(value):
    """Yield (value_type, ids) for every matcher-shaped value group found in
    `value`, at any nesting depth.

    Matcher-kind parameters have one such group directly; the
    corporation_item_delivery kind nests two of them (one per sub-field) a
    level deeper - this walks either shape without special-casing either.

    A group with an empty `values` list means "unrestricted" (any value of
    that type is accepted) rather than "nothing set" - yielded as
    `(None, [])` so callers can render that distinctly from an id list.
    """

    if not isinstance(value, dict):
        return

    entries = value.get("values")
    if isinstance(entries, list):
        if entries and isinstance(entries[0], dict) and "value_type" in entries[0]:
            for entry in entries:
                yield entry.get("value_type"), entry.get("values") or []
        else:
            yield None, []
        return

    for nested in value.values():
        yield from _iter_value_groups(nested)


def describe_parameters(method: str, parameters: dict) -> list[dict]:
    """Label a job's raw configuration_parameters using the SDE parameter definitions.

    Each parameter value is one of 4 ESI-defined shapes (matcher/options/boolean/
    corporation_item_delivery), keyed by which "kind" it is. We label the
    parameter itself from the SDE, and resolve any location/ship/character/etc.
    ids inside it to display names via `resolve.resolve_values` - values whose
    `value_type` isn't recognized (or that aren't id references at all, e.g.
    booleans/options) are left for the raw `value` to cover.
    """

    param_defs = {
        p.key: p for p in FreelanceJobSchemaParameter.objects.filter(schema_id=method)
    }

    rows = []
    for key, value in (parameters or {}).items():
        param_def = param_defs.get(key)
        kind = next(iter(value), None) if isinstance(value, dict) else None
        row_value = value.get(kind) if kind and isinstance(value, dict) else value

        resolved = []
        for value_type, ids in _iter_value_groups(row_value):
            if value_type is None and not ids:
                resolved.append({"value_type": None, "id": None, "name": None, "is_any": True})
                continue

            names = resolve_values(value_type, ids)
            for raw_id in ids:
                resolved.append(
                    {"value_type": value_type, "id": raw_id, "name": names.get(str(raw_id))}
                )

        rows.append(
            {
                "key": key,
                "title": (param_def.title if param_def else None) or key,
                "value": row_value,
                "resolved": resolved,
            }
        )

    return rows
