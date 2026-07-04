"""Resolve a live Freelance Job parameter's raw ESI value(s) into display names.

The SDE schema's own `accepted_value_types` declarations turn out to be the
runtime `value_type` vocabulary too - confirmed for `solarsystem`/`station`
against real fixture data, and for the rest by trusting the SDE's own
per-parameter `acceptedValueTypes` list as the spec it is (e.g. `DeliverItem`'s
`inventoryType` matcher literally declares `["item_type", "item_group"]`).
Extend `_RESOLVERS` below as more `value_type`s are seen in the wild; anything
missing just isn't resolved (callers fall back to the raw id).
"""

# Standard Library
from dataclasses import dataclass

# Third Party
# Django EVE SDE
from eve_sde.models import (
    Constellation,
    ItemGroup,
    ItemType,
    NPCStation,
    Region,
    SolarSystem,
)

# Alliance Auth
from allianceauth.eveonline.models import (
    EveAllianceInfo,
    EveCharacter,
    EveCorporationInfo,
    EveFactionInfo,
)


@dataclass(frozen=True)
class _ValueTypeResolver:
    model: type
    lookup_field: str  # "pk" or a natural-key field name (e.g. "character_id")
    name_field: str


_RESOLVERS = {
    "solarsystem": _ValueTypeResolver(SolarSystem, "pk", "name"),
    "constellation": _ValueTypeResolver(Constellation, "pk", "name"),
    "region": _ValueTypeResolver(Region, "pk", "name"),
    # Only NPC stations exist in the SDE - a "structure" value_type (player
    # Upwell structures) has no static row to resolve to.
    "station": _ValueTypeResolver(NPCStation, "pk", "name"),
    # Every "_type"/"_group" pair below is the SDE's own accepted_value_types
    # naming for a given parameter's category - always ItemType/ItemGroup.
    "ship_type": _ValueTypeResolver(ItemType, "pk", "name"),
    "ship_class": _ValueTypeResolver(ItemGroup, "pk", "name"),
    "ore_type": _ValueTypeResolver(ItemType, "pk", "name"),
    "ore_group": _ValueTypeResolver(ItemGroup, "pk", "name"),
    "item_type": _ValueTypeResolver(ItemType, "pk", "name"),
    "item_group": _ValueTypeResolver(ItemGroup, "pk", "name"),
    "character": _ValueTypeResolver(EveCharacter, "character_id", "character_name"),
    "corporation": _ValueTypeResolver(EveCorporationInfo, "corporation_id", "corporation_name"),
    "alliance": _ValueTypeResolver(EveAllianceInfo, "alliance_id", "alliance_name"),
    "faction": _ValueTypeResolver(EveFactionInfo, "faction_id", "faction_name"),
}


def resolve_values(value_type: str, ids: list[str]) -> dict[str, str]:
    """Resolve raw ESI id strings of a given `value_type` to display names.

    Returns `{id: name}` for every id that could be resolved. Ids that can't
    be resolved (unknown value_type, or no matching row) are simply absent -
    callers should fall back to showing the raw id for those.
    """

    resolver = _RESOLVERS.get(value_type)
    if not resolver or not ids:
        return {}

    try:
        int_ids = [int(i) for i in ids]
    except (TypeError, ValueError):
        return {}

    lookup = "pk" if resolver.lookup_field == "pk" else resolver.lookup_field
    queryset = resolver.model.objects.filter(**{f"{lookup}__in": int_ids})

    return {
        str(getattr(obj, lookup)): getattr(obj, resolver.name_field)
        for obj in queryset
    }
