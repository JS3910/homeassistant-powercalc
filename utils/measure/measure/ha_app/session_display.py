"""How a stored measurement is titled and grouped on the sessions list."""

from collections.abc import Sequence

from measure.clock import elapsed_seconds, parse_utc
from measure.ha_app.session import SessionSnapshot, snapshot_progress_timing
from measure.home_assistant_entities import EntityCatalogSnapshot, EntityDescriptor
from measure.request import MeasurementRequest

_ENTITY_ID_PREFIXES = ("light.", "media_player.", "fan.", "vacuum.", "lawn_mower.")


def looks_like_entity_list(value: str, entity_ids: Sequence[str] = ()) -> bool:
    """True when a stored label is a dump of entity ids or joined friendly names."""

    text = value.strip()
    if not text:
        return False
    if text in entity_ids or text.startswith(_ENTITY_ID_PREFIXES):
        return True
    return ", " in text and len(text.split(", ")) >= 2


def brand_model_title(manufacturer: str, product: str) -> str:
    """One brand + model line. The product already includes the brand is left as-is."""

    manufacturer = manufacturer.strip()
    product = product.strip()
    if manufacturer and product:
        if product.casefold().startswith(manufacturer.casefold()):
            return product
        return f"{manufacturer} · {product}"
    return product


def descriptor_for_request(
    request: MeasurementRequest,
    catalog: EntityCatalogSnapshot | None,
) -> EntityDescriptor | None:
    """First controlled entity that still has device-registry identity in HA."""

    if catalog is None:
        return None
    fallback: EntityDescriptor | None = None
    for entity_id in request.controlled_entity_ids:
        entity = catalog.get(entity_id)
        if entity is None:
            continue
        if entity.manufacturer or entity.product_name:
            return entity
        fallback = entity if fallback is None else fallback
    return fallback


def session_identity(
    request: MeasurementRequest,
    descriptor: EntityDescriptor | None = None,
) -> tuple[str, str, str]:
    """Return ``(manufacturer, product, display_title)`` for a session card."""

    manufacturer = (descriptor.manufacturer or "").strip() if descriptor is not None else ""
    product = (descriptor.product_name or "").strip() if descriptor is not None else ""
    stored = request.product_name.strip()
    if not product and stored and not looks_like_entity_list(stored, request.controlled_entity_ids):
        product = stored
    title = brand_model_title(manufacturer, product)
    if title:
        return manufacturer, product, title
    session_name = request.session_name.strip()
    if session_name and not looks_like_entity_list(session_name, request.controlled_entity_ids):
        return manufacturer, product, session_name
    if request.model_id.strip():
        return manufacturer, product, request.model_id.strip()
    if request.controlled_entity_ids:
        return manufacturer, product, request.controlled_entity_ids[0]
    return manufacturer, product, "Measurement"


def session_family_key(request: MeasurementRequest, *, product: str = "") -> str:
    """Group refine/resume history of the same model on the same meter."""

    model = request.model_id.strip() or product.strip() or request.measure_type.value
    device = request.measure_device.strip() or "unknown-meter"
    return f"{request.measure_type.value}|{model.casefold()}|{device.casefold()}"


def session_modes(request: MeasurementRequest) -> list[str]:
    modes = getattr(request, "modes", None)
    if not modes:
        return []
    return sorted(str(mode) for mode in modes)


def session_duration_seconds(snapshot: SessionSnapshot) -> int | None:
    elapsed, _ = snapshot_progress_timing(snapshot)
    if elapsed is not None:
        return int(elapsed)
    fallback = elapsed_seconds(snapshot.created_at, ended_at=snapshot.updated_at)
    return None if fallback is None else round(fallback)


def points_this_run(*, completed: int, already_measured: int, recorded: int | None) -> int:
    """Points actually taken in this run, not the planned grid or inherited seed.

    ``recorded`` is unique LUT keys written to ``*.raw.jsonl`` after the run started.
    When that log is missing, fall back to ``completed - already_measured``.
    """

    if recorded is not None:
        return max(0, recorded)
    return max(0, completed - already_measured)


def raw_since_timestamp(snapshot: SessionSnapshot) -> float:
    """Unix time after which a raw.jsonl line counts as this session's own sample.

    Refine copies the seed's ``*.raw.jsonl``. Those lines keep their original
    timestamps, so the floor is this session's run start — or ``created_at`` if
    it was cancelled before the runner reported a start.
    """

    when = snapshot.run_started_at or snapshot.created_at
    return parse_utc(when).timestamp() if when else 0.0
