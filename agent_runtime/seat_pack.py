#598 rework: seat definitions are config, code is only the loader.
#
# Shipped defaults: agent_runtime/seats/delivery_seats.yaml (package data).
# User overrides: ~/.fusion-agent-studio/seats/*.yaml|json, merged per
# seat id (user wins). Invalid override files fail visibly — logged as
# errors and skipped, never silently partial; an invalid SHIPPED pack
# raises (fail fast at seed time).
#
# Reviewer seats carry prompt_kind: reviewer plus data fields (role,
# checklist, findings_schema); the loader renders their system_prompt so
# schema/checklist tweaks are config-only changes.

import logging
from pathlib import Path

import yaml

from .paths import studio_home

logger = logging.getLogger(__name__)

SHIPPED_PACK = Path(__file__).parent / "seats" / "delivery_seats.yaml"
USER_OVERRIDE_DIRNAME = "seats"

_REVIEWER_PROMPT_TEMPLATE = (
    "You are the {role} seat in the product-delivery pipeline. You "
    "review a PR diff (read it with the git/file tools, or via the "
    "diff text given in the input) and produce a machine-consumable "
    "verdict the pipeline counts deterministically (blocking = 0 "
    "means the task is ready for gate C).\n\n"
    "Checklist (fixed, do not skip items):\n{checklist}\n\n"
    "Output contract (machine-consumable, output ONLY this JSON):\n"
    "{{\"verdict\": {verdicts},\n"
    " \"findings\": [{{{fields}}}]}}\n"
    "verdict=blocking iff any finding is {blocking}. No prose outside "
    "the JSON.\n\n"
    "Severity calibration is shared across all reviewer seats: "
    "{severities_note}. Only report what the diff shows; never invent "
    "file paths or line numbers."
)


def _render_reviewer_prompt(spec: dict) -> str:
    schema = spec.get("findings_schema") or {}
    checklist = "\n".join(
        f"- {item}" for item in (spec.get("checklist") or [])
    )
    fields = ", ".join(
        f'"{f}": "<{f}>"' for f in schema.get("finding_fields", [])
    )
    severities = schema.get("severities", [])
    blocking = " or ".join(schema.get("blocking_severities", []))
    severities_note = (
        f"{' = '.join(severities[:1])} = exploitable/blocking now, "
        f"{blocking} = must fix before gate C, later severities = "
        "should fix / note"
        if severities and blocking
        else "follow the shared calibration"
    )
    return _REVIEWER_PROMPT_TEMPLATE.format(
        role=spec.get("role", "Reviewer"),
        checklist=checklist,
        verdicts=" | ".join(f'"{v}"' for v in schema.get("verdict", [])),
        fields=fields,
        blocking=blocking,
        severities_note=severities_note,
    )


def _load_pack_file(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or not isinstance(
        data.get("seats"), dict
    ):
        raise ValueError(f"seat pack {path} has no 'seats' mapping")
    seats = {}
    for seat_id, spec in data["seats"].items():
        if not isinstance(spec, dict) or not spec.get("id", seat_id):
            raise ValueError(f"seat pack {path}: invalid seat {seat_id}")
        spec = dict(spec)
        spec["id"] = seat_id
        if spec.get("prompt_kind") == "reviewer":
            spec["system_prompt"] = _render_reviewer_prompt(spec)
        for key in ("tools", "capabilities", "tags", "checklist"):
            if isinstance(spec.get(key), list):
                spec[key] = [str(x) for x in spec[key]]
        seats[seat_id] = spec
    return seats


def user_seats_dir() -> Path:
    return studio_home() / USER_OVERRIDE_DIRNAME


def load_seat_specs() -> dict:
    """Load shipped defaults + user overrides, keyed by seat id.

    User override dir wins per seat id. Invalid override files are
    logged as errors and skipped (visible failure, never silent); a
    broken shipped pack raises so seeding fails loudly.
    """
    specs = _load_pack_file(SHIPPED_PACK)
    override_dir = user_seats_dir()
    if not override_dir.is_dir():
        return specs
    for path in sorted(override_dir.iterdir()):
        if path.suffix.lower() not in (".yaml", ".yml", ".json"):
            continue
        try:
            overrides = _load_pack_file(path)
        except Exception as e:
            logger.error(
                "Skipping invalid seat pack override %s: %s", path, e
            )
            continue
        for seat_id, spec in overrides.items():
            if seat_id in specs:
                logger.info(
                    "Seat %s overridden by user pack %s", seat_id, path
                )
            specs[seat_id] = spec
    return specs


def seat_ids() -> tuple:
    return tuple(load_seat_specs())
