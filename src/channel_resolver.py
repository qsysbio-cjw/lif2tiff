#!/usr/bin/env python3
"""Evidence-based channel identity and acquisition consistency diagnostics."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


RESOURCE_ROOT = (
    Path(sys._MEIPASS)
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parents[1]
)
REGISTRY_PATH = RESOURCE_ROOT / "resources" / "channel_registry.json"
PROTOCOL_REGISTRY_PATH = RESOURCE_ROOT / "resources" / "protocol_registry.json"
TRANSMISSION_TERMS = ("tld", "trans pmt", "transmission")
CONFIDENCE_ORDER = {"confirmed": 4, "high": 3, "medium": 2, "low": 1, "unknown": 0}
OPTICAL_TOLERANCE_NM = 0.5


def load_registry(path: Path | None = None) -> dict:
    return json.loads((path or REGISTRY_PATH).read_text(encoding="utf-8"))


def user_protocol_registry_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
        return root / "LIF2TIFF" / "protocol_registry.json"
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "lif2tiff" / "protocol_registry.json"


def protocol_registry_path(path: Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser()
    override = os.environ.get("LIF2TIFF_PROTOCOL_REGISTRY", "").strip()
    if override:
        return Path(override).expanduser()
    user_path = user_protocol_registry_path()
    return user_path if user_path.is_file() else PROTOCOL_REGISTRY_PATH


def load_protocol_registry(path: Path | None = None) -> dict:
    selected = protocol_registry_path(path)
    return json.loads(selected.read_text(encoding="utf-8"))


def _text(value) -> str:
    return str(value or "").strip()


def _normalized(value) -> str:
    value = _text(value).lower()
    value = value.removeprefix("leica/")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return " ".join(value.split())


def canonical_assignment_id(value: str, registry: dict | None = None) -> str:
    """Resolve a user-entered dye name, registry id, or special assignment."""
    registry = registry or load_registry()
    normalized = _normalized(value)
    special = {
        "brightfield": "brightfield",
        "bf": "brightfield",
        "unknown": "unknown",
        "unassigned": "unknown",
        "none": "unknown",
    }
    if normalized in special:
        return special[normalized]
    matches = []
    for entry in registry["entries"]:
        names = {
            _normalized(entry["id"]),
            _normalized(entry.get("display_name")),
            *(_normalized(alias) for alias in entry.get("aliases", [])),
        }
        if normalized in names:
            matches.append(entry["id"])
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(f"ambiguous channel assignment {value!r}: {', '.join(matches)}")
    valid = [entry["id"] for entry in registry["entries"] if entry.get("kind") == "stain"]
    raise ValueError(
        f"unknown channel assignment {value!r}; use brightfield, unknown, or one of: "
        + ", ".join(valid)
    )


def parse_channel_assignments(values: list[str] | None, registry: dict | None = None) -> dict[int, str]:
    """Parse repeatable C0=<dye> assignments and canonicalize user input."""
    registry = registry or load_registry()
    assignments = {}
    for value in values or []:
        match = re.fullmatch(r"c?(\d+)=(.+)", value.strip(), flags=re.IGNORECASE)
        if not match:
            raise ValueError(f"invalid channel role {value!r}; expected C<index>=<dye>")
        index = int(match.group(1))
        assignment = canonical_assignment_id(match.group(2), registry)
        if index in assignments and assignments[index] != assignment:
            raise ValueError(f"conflicting assignments supplied for C{index}")
        assignments[index] = assignment
    return assignments


def _numbers(value) -> list[float]:
    if value is None:
        return []
    if isinstance(value, (int, float)):
        return [float(value)]
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("wavelength_nm")
            result.extend(_numbers(item))
        return result
    result = []
    for token in re.split(r"[;, ]+", str(value).strip()):
        if not token:
            continue
        try:
            result.append(float(token))
        except ValueError:
            pass
    return result


def _active_laser_evidence(channel: dict) -> list[float]:
    wavelengths = _numbers(
        channel.get("excitation_wavelengths_nm")
        or channel.get("excitation_settings")
    )
    if wavelengths:
        return wavelengths
    all_wavelengths = _numbers(channel.get("all_visible_laser_wavelengths_nm"))
    intensities = _numbers(channel.get("all_visible_laser_intensities_percent"))
    return [
        wavelength
        for wavelength, intensity in zip(all_wavelengths, intensities)
        if intensity > 0
    ]


def normalize_channel(channel: dict) -> dict:
    index = int(channel.get("channel_index", channel.get("index", 0)))
    lut = _text(channel.get("lut"))
    detector_name = _text(channel.get("detector_name"))
    detector_type = _text(channel.get("detector_type"))
    scan_type = _text(channel.get("scan_type"))
    acquisition_mode = _text(channel.get("acquisition_mode"))
    dye_name = _text(
        channel.get("dye_name_from_leica", channel.get("dye_name"))
    )
    excitation = _active_laser_evidence(channel)
    emission_begin = _numbers(channel.get("emission_window_begin_nm"))
    emission_end = _numbers(channel.get("emission_window_end_nm"))
    return {
        "index": index,
        "channel_key": f"C{index:02d}",
        "physical_label": _text(channel.get("physical_label") or channel.get("label")),
        "lut": lut,
        "detector_name": detector_name,
        "detector_type": detector_type,
        "scan_type": scan_type,
        "acquisition_mode": acquisition_mode,
        "dye_name": dye_name,
        "excitation_wavelengths_nm": excitation,
        "emission_window_begin_nm": emission_begin[0] if emission_begin else None,
        "emission_window_end_nm": emission_end[0] if emission_end else None,
        "gain": channel.get("gain"),
        "offset": channel.get("offset"),
        "bit_depth": channel.get("bit_depth"),
        "excitation_intensities_percent": _numbers(
            channel.get("excitation_intensities_percent")
            or [
                item.get("intensity_percent")
                for item in channel.get("excitation_settings", [])
                if isinstance(item, dict)
            ]
        ),
    }


def _quantized(value, step: str) -> str:
    if value is None or value == "":
        return ""
    unit = Decimal(step)
    number = Decimal(str(value))
    bucket = (number / unit).quantize(Decimal("1"), rounding=ROUND_HALF_UP) * unit
    places = max(0, -unit.as_tuple().exponent)
    return f"{bucket:.{places}f}"


def _quantized_list(values: list[float], step: str) -> str:
    return ";".join(_quantized(value, step) for value in values)


def _protocol_signature_key(signature: dict) -> tuple[str, ...]:
    return tuple(
        str(signature.get(field) or "")
        for field in (
            "excitation_nm",
            "laser_intensity_percent",
            "emission_begin_nm",
            "emission_end_nm",
            "gain",
            "offset",
            "detector",
            "detector_type",
            "scan_type",
            "acquisition_mode",
            "bit_depth",
        )
    )


def _channel_protocol_key(channel: dict) -> tuple[str, ...]:
    return _protocol_signature_key(
        {
            "excitation_nm": _quantized_list(channel["excitation_wavelengths_nm"], "0.1"),
            "laser_intensity_percent": _quantized_list(
                channel["excitation_intensities_percent"], "0.05"
            ),
            "emission_begin_nm": _quantized(channel["emission_window_begin_nm"], "0.1"),
            "emission_end_nm": _quantized(channel["emission_window_end_nm"], "0.1"),
            "gain": _quantized(channel["gain"], "0.1"),
            "offset": _quantized(channel["offset"], "0.1"),
            "detector": channel["detector_name"],
            "detector_type": channel["detector_type"],
            "scan_type": channel["scan_type"],
            "acquisition_mode": channel["acquisition_mode"],
            "bit_depth": str(channel["bit_depth"] or ""),
        }
    )


def _matching_protocols(channel: dict, protocol_registry: dict | None) -> list[dict]:
    if not protocol_registry:
        return []
    key = _channel_protocol_key(channel)
    index = protocol_registry.get("_signature_index")
    if index is None:
        index = defaultdict(list)
        for group in protocol_registry.get("groups", []):
            index[_protocol_signature_key(group["signature"])].append(group)
        protocol_registry["_signature_index"] = dict(index)
    return index.get(key, [])


def _modality(channel: dict) -> dict:
    detector_text = " ".join(
        [
            channel["detector_name"],
            channel["detector_type"],
            channel["scan_type"],
            channel["acquisition_mode"],
        ]
    ).lower()
    if any(term in detector_text for term in TRANSMISSION_TERMS):
        return {
            "value": "brightfield",
            "confidence": "high",
            "evidence": [
                value
                for value in (
                    f"detector={channel['detector_name']}" if channel["detector_name"] else "",
                    f"scan_type={channel['scan_type']}" if channel["scan_type"] else "",
                )
                if value
            ],
        }
    if channel["scan_type"].lower() == "internal" or channel["detector_name"].lower().startswith(
        ("hyd", "pmt")
    ):
        return {
            "value": "fluorescence",
            "confidence": "high",
            "evidence": [
                value
                for value in (
                    f"detector={channel['detector_name']}" if channel["detector_name"] else "",
                    f"scan_type={channel['scan_type']}" if channel["scan_type"] else "",
                )
                if value
            ],
        }
    if channel["excitation_wavelengths_nm"]:
        return {
            "value": "fluorescence",
            "confidence": "medium",
            "evidence": [
                "active_excitation="
                + ",".join(f"{value:g}" for value in channel["excitation_wavelengths_nm"])
                + " nm"
            ],
        }
    return {
        "value": "unknown",
        "confidence": "unknown",
        "evidence": ["insufficient detector and excitation metadata"],
    }


def _match_registry(value: str, registry: dict) -> list[dict]:
    normalized = _normalized(value)
    if not normalized:
        return []
    matches = []
    for entry in registry["entries"]:
        aliases = {_normalized(alias) for alias in entry.get("aliases", [])}
        if normalized in aliases:
            matches.append(entry)
    return matches


def _filename_matches(value: str, registry: dict) -> list[dict]:
    normalized = f" {_normalized(value)} "
    matches = []
    for entry in registry["entries"]:
        if entry.get("kind") != "stain":
            continue
        for alias in entry.get("aliases", []):
            token = f" {_normalized(alias)} "
            if token.strip() and token in normalized:
                matches.append(entry)
                break
    return matches


def _optical_compatibility(channel: dict, entry: dict) -> tuple[str, list[str]]:
    hint = entry.get("optical_hint")
    if not hint:
        return "unknown", ["registry has no optical hint"]
    excitation = channel["excitation_wavelengths_nm"]
    emission_begin = channel["emission_window_begin_nm"]
    emission_end = channel["emission_window_end_nm"]
    if not excitation or emission_begin is None or emission_end is None:
        return "unknown", ["channel optical metadata is incomplete"]
    ex_min, ex_max = hint["excitation_range_nm"]
    em_min, em_max = hint["emission_range_nm"]
    excitation_matches = any(ex_min <= value <= ex_max for value in excitation)
    emission_overlaps = max(emission_begin, em_min) <= min(emission_end, em_max)
    evidence = [
        "channel_excitation="
        + ",".join(f"{value:g}" for value in excitation)
        + " nm",
        f"channel_emission={emission_begin:g}-{emission_end:g} nm",
        f"registry_hint=Ex {ex_min}-{ex_max} nm; Em {em_min}-{em_max} nm",
    ]
    return (
        ("compatible" if excitation_matches and emission_overlaps else "conflict"),
        evidence,
    )


def resolve_channel(
    channel: dict,
    *,
    series_name: str = "",
    source_name: str = "",
    user_assignment: str | None = None,
    registry: dict | None = None,
    protocol_registry: dict | None = None,
) -> dict:
    registry = registry or load_registry()
    normalized = normalize_channel(channel)
    modality = _modality(normalized)
    if user_assignment == "brightfield":
        modality = {
            "value": "brightfield",
            "confidence": "confirmed",
            "evidence": ["user_assignment=brightfield"],
        }
    elif user_assignment and user_assignment != "unknown":
        modality = {
            "value": "fluorescence",
            "confidence": "confirmed",
            "evidence": [f"user_assignment={user_assignment}"],
        }
    lut_label = (
        _normalized(normalized["lut"]).replace(" ", "_")
        or _normalized(normalized["physical_label"]).replace(" ", "_")
        or "unknown"
    )
    directory_label = (
        "brightfield" if modality["value"] == "brightfield" else lut_label
    )
    candidates = []

    protocol_matches = _matching_protocols(normalized, protocol_registry)

    if user_assignment == "unknown":
        confirmed = {
            "id": "unknown",
            "display_name": "Unknown",
            "role": None,
            "kind": "explicit_unknown",
            "confidence": "confirmed",
            "source": "user_override",
            "evidence": ["user explicitly left this channel unassigned"],
        }
    elif user_assignment == "brightfield":
        confirmed = {
            "id": "brightfield",
            "display_name": "Brightfield",
            "role": "morphology",
            "kind": "modality",
            "confidence": "confirmed",
            "source": "user_override",
            "evidence": ["user_assignment=brightfield"],
        }
    elif user_assignment:
        entry = next(
            (item for item in registry["entries"] if item["id"] == user_assignment),
            None,
        )
        confirmed = {
            "id": user_assignment,
            "display_name": entry.get("display_name", user_assignment) if entry else user_assignment,
            "role": entry.get("default_role") if entry else None,
            "kind": entry.get("kind", "user_defined") if entry else "user_defined",
            "confidence": "confirmed",
            "source": "user_override",
            "evidence": [f"user_assignment={user_assignment}"],
        }
        candidates.append(confirmed)
    else:
        confirmed = None
        for entry in _match_registry(normalized["dye_name"], registry):
            candidates.append(
                {
                    "id": entry["id"],
                    "display_name": entry["display_name"],
                    "role": entry.get("default_role"),
                    "kind": entry["kind"],
                    "confidence": "high",
                    "source": "leica_dye_name",
                    "evidence": [f"Leica DyeName={normalized['dye_name']}"],
                }
            )
        existing = {item["id"] for item in candidates}
        for match in protocol_matches:
            candidate_id = match.get("candidate_dye_id")
            if not candidate_id or candidate_id in existing:
                continue
            entry = next(
                (item for item in registry["entries"] if item["id"] == candidate_id),
                None,
            )
            if entry is None:
                continue
            candidates.append(
                {
                    "id": entry["id"],
                    "display_name": entry["display_name"],
                    "role": entry.get("default_role"),
                    "kind": entry["kind"],
                    "confidence": "high",
                    "source": "frozen_protocol_match",
                    "evidence": [
                        f"protocol_group={match['id']}",
                        f"historical_acquisition_family={match['acquisition_family']}",
                        f"historical_channels={match['channel_count']}",
                        "acquisition match does not prove reagent presence",
                    ],
                }
            )
            existing.add(candidate_id)
        filename_context = f"{source_name} {series_name}"
        if modality["value"] == "fluorescence":
            for entry in _filename_matches(filename_context, registry):
                if entry["id"] in existing:
                    continue
                compatibility, optical_evidence = _optical_compatibility(
                    normalized, entry
                )
                if compatibility == "conflict":
                    continue
                candidates.append(
                    {
                        "id": entry["id"],
                        "display_name": entry["display_name"],
                        "role": entry.get("default_role"),
                        "kind": entry["kind"],
                        "confidence": (
                            "medium" if compatibility == "compatible" else "low"
                        ),
                        "source": "filename_hint",
                        "evidence": [
                            f"filename_or_series={filename_context.strip()}",
                            *optical_evidence,
                        ],
                    }
                )

    stain_candidates = [item for item in candidates if item["kind"] == "stain"]
    inferred = max(
        stain_candidates,
        key=lambda item: CONFIDENCE_ORDER[item["confidence"]],
        default=None,
    )
    confirmed_dye = confirmed if confirmed and confirmed.get("kind") == "stain" else None
    if confirmed is not None:
        review_status = "confirmed"
    elif modality["value"] == "brightfield" and modality["confidence"] == "high":
        review_status = "resolved_modality"
    elif inferred is not None and inferred["confidence"] in {"high", "medium"}:
        review_status = "candidate"
    else:
        review_status = "unknown"
    return {
        "channel_key": normalized["channel_key"],
        "channel_index": normalized["index"],
        "directory": f"{normalized['channel_key']}_{directory_label}",
        "display": {"lut": normalized["lut"] or None},
        "modality": modality,
        "acquisition": {
            key: normalized[key]
            for key in (
                "detector_name",
                "detector_type",
                "scan_type",
                "acquisition_mode",
                "dye_name",
                "excitation_wavelengths_nm",
                "emission_window_begin_nm",
                "emission_window_end_nm",
                "gain",
                "offset",
                "excitation_intensities_percent",
            )
        },
        "stain_candidates": candidates,
        "confirmed_assignment": confirmed,
        "inferred_dye": inferred,
        "confirmed_dye": confirmed_dye,
        "review_status": review_status,
        "protocol_matches": [
            {
                "id": match["id"],
                "acquisition_family": match["acquisition_family"],
                "candidate_dye_id": match.get("candidate_dye_id"),
                "historical_channel_count": match["channel_count"],
            }
            for match in protocol_matches
        ],
    }


def _protocol_signatures(channel: dict) -> tuple[tuple, tuple]:
    item = normalize_channel(channel)
    modality = _modality(item)["value"]
    modality_signature = (
        modality,
        item["detector_name"].lower(),
        item["detector_type"].lower(),
        item["scan_type"].lower(),
    )
    setting_signature = (
        _text(item["gain"]),
        _text(item["offset"]),
        tuple(round(float(value), 3) for value in item["excitation_intensities_percent"]),
    )
    return modality_signature, setting_signature


def _field_spread_within(values: list, tolerance: float) -> bool:
    if not values:
        return True
    if any(value is None for value in values):
        return all(value is None for value in values)
    if any(isinstance(value, (list, tuple)) for value in values):
        if not all(isinstance(value, (list, tuple)) for value in values):
            return False
        lengths = {len(value) for value in values}
        if len(lengths) != 1:
            return False
        return all(
            max(float(value[index]) for value in values)
            - min(float(value[index]) for value in values)
            <= tolerance
            for index in range(next(iter(lengths)))
        )
    return max(float(value) for value in values) - min(float(value) for value in values) <= tolerance


def _optical_rows_consistent(rows: list[dict], tolerance_nm: float) -> bool:
    normalized = [normalize_channel(row) for row in rows]
    return all(
        _field_spread_within([item[field] for item in normalized], tolerance_nm)
        for field in (
            "excitation_wavelengths_nm",
            "emission_window_begin_nm",
            "emission_window_end_nm",
        )
    )


def assess_consistency(
    series_channels: list[dict],
    *,
    optical_tolerance_nm: float = OPTICAL_TOLERANCE_NM,
) -> dict:
    """Assess normalized rows containing series_index and one channel record."""
    by_series: dict[str, list[dict]] = defaultdict(list)
    by_channel: dict[int, list[dict]] = defaultdict(list)
    for row in series_channels:
        series_index = _series_index_text(row)
        by_series[series_index].append(row)
        by_channel[int(row.get("channel_index", row.get("index", 0)))].append(row)

    issues = []
    channel_counts = sorted({len(rows) for rows in by_series.values()})
    channel_layouts = sorted(
        {
            tuple(
                sorted(
                    int(row.get("channel_index", row.get("index", 0)))
                    for row in rows
                )
            )
            for rows in by_series.values()
        }
    )
    if len(channel_counts) > 1:
        issues.append(
            {
                "severity": "warning",
                "code": "channel_count_varies",
                "message": f"series channel counts vary: {channel_counts}",
            }
        )
    elif len(channel_layouts) > 1:
        issues.append(
            {
                "severity": "warning",
                "code": "channel_layout_varies",
                "message": f"series channel index layouts vary: {channel_layouts}",
            }
        )

    variation_found = False
    for index, rows in sorted(by_channel.items()):
        normalized_rows = [normalize_channel(row) for row in rows]
        modalities = [_modality(item)["value"] for item in normalized_rows]
        if "unknown" in modalities:
            issues.append(
                {
                    "severity": "warning",
                    "code": "unknown_modality",
                    "channel_key": f"C{index:02d}",
                    "message": f"C{index:02d} modality cannot be resolved",
                }
            )
        if any(
            modality == "fluorescence"
            and (
                not item["excitation_wavelengths_nm"]
                or item["emission_window_begin_nm"] is None
                or item["emission_window_end_nm"] is None
            )
            for item, modality in zip(normalized_rows, modalities)
        ):
            issues.append(
                {
                    "severity": "warning",
                    "code": "incomplete_fluorescence_optics",
                    "channel_key": f"C{index:02d}",
                    "message": (
                        f"C{index:02d} fluorescence excitation or emission metadata "
                        "is incomplete"
                    ),
                }
            )
        modality_signatures = set()
        setting_signatures = set()
        for row in rows:
            modality, settings = _protocol_signatures(row)
            modality_signatures.add(modality)
            setting_signatures.add(settings)
        if len(modality_signatures) > 1:
            issues.append(
                {
                    "severity": "warning",
                    "code": "modality_varies",
                    "channel_key": f"C{index:02d}",
                    "message": f"C{index:02d} modality/detector mapping varies",
                }
            )
        elif not _optical_rows_consistent(rows, optical_tolerance_nm):
            issues.append(
                {
                    "severity": "warning",
                    "code": "optical_protocol_varies",
                    "channel_key": f"C{index:02d}",
                    "message": (
                        f"C{index:02d} excitation or emission settings vary beyond "
                        f"{optical_tolerance_nm:g} nm"
                    ),
                }
            )
        if len(setting_signatures) > 1:
            variation_found = True

    if any(item["severity"] == "warning" for item in issues):
        status = "warning"
    elif variation_found:
        status = "variation"
        issues.append(
            {
                "severity": "variation",
                "code": "acquisition_values_vary",
                "message": "gain, offset, or laser power varies without changing modality",
            }
        )
    else:
        status = "stable"
    return {
        "status": status,
        "series_count": len(by_series),
        "channel_keys": [f"C{index:02d}" for index in sorted(by_channel)],
        "optical_tolerance_nm": optical_tolerance_nm,
        "issues": issues,
    }


def decide_workflow(consistency: dict) -> dict:
    """Route ordinary acquisitions to standard flow and exceptions to review."""
    warnings = [
        issue for issue in consistency.get("issues", []) if issue["severity"] == "warning"
    ]
    if warnings:
        return {
            "route": "review",
            "status": "review_required",
            "requires_acknowledgement": True,
            "reason_codes": sorted({issue["code"] for issue in warnings}),
            "summary": "Acquisition structure or optical identity varies or is incomplete.",
        }
    if consistency.get("status") == "variation":
        return {
            "route": "standard",
            "status": "ready_with_variation",
            "requires_acknowledgement": False,
            "reason_codes": ["acquisition_values_vary"],
            "summary": "Only gain, offset, or laser power varies across series.",
        }
    return {
        "route": "standard",
        "status": "ready",
        "requires_acknowledgement": False,
        "reason_codes": [],
        "summary": "Channel structure and optical identity are consistent.",
    }


def _series_sort_key(value) -> tuple[int, object]:
    try:
        return 0, int(value)
    except (TypeError, ValueError):
        return 1, _text(value)


def _series_index_text(row: dict) -> str:
    value = row.get("series_index", 0)
    return "0" if value is None else str(value).strip()


def _value_key(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _value_summary(rows: list[dict], field: str) -> dict:
    values = [normalize_channel(row)[field] for row in rows]
    counts = Counter(_value_key(value) for value in values)
    by_key = {_value_key(value): value for value in values}
    representative_key = min(counts, key=lambda key: (-counts[key], key))
    representative = by_key[representative_key]
    if any(value is None for value in values):
        minimum = maximum = None
    elif values and isinstance(values[0], (list, tuple)):
        lengths = {len(value) for value in values}
        if len(lengths) == 1:
            minimum = [min(float(value[i]) for value in values) for i in range(len(values[0]))]
            maximum = [max(float(value[i]) for value in values) for i in range(len(values[0]))]
        else:
            minimum = maximum = None
    else:
        minimum = min(float(value) for value in values)
        maximum = max(float(value) for value in values)
    return {
        "representative": representative,
        "min": minimum,
        "max": maximum,
    }


def _series_descriptor(series_index, rows: list[dict]) -> dict:
    ordered = sorted(
        rows,
        key=lambda row: int(row.get("channel_index", row.get("index", 0))),
    )
    source_index = ordered[0].get("series_index") if ordered else None
    return {
        "series_index": series_index if source_index is None else source_index,
        "series_name": _text(ordered[0].get("series_name")) if ordered else "",
        "rows": ordered,
        "channels": {
            int(row.get("channel_index", row.get("index", 0))): row for row in ordered
        },
    }


def _group_accepts(group: dict, series: dict, tolerance_nm: float) -> bool:
    reference_keys = set(group["members"][0]["channels"])
    if set(series["channels"]) != reference_keys:
        return False
    for channel_index in reference_keys:
        existing = [member["channels"][channel_index] for member in group["members"]]
        candidate = series["channels"][channel_index]
        reference_modality, _settings = _protocol_signatures(existing[0])
        candidate_modality, _settings = _protocol_signatures(candidate)
        if candidate_modality != reference_modality:
            return False
        if not _optical_rows_consistent([*existing, candidate], tolerance_nm):
            return False
    return True


def _cluster_series(series_channels: list[dict], tolerance_nm: float) -> list[dict]:
    by_series: dict[str, list[dict]] = defaultdict(list)
    for row in series_channels:
        by_series[_series_index_text(row)].append(row)
    descriptors = [
        _series_descriptor(index, rows)
        for index, rows in sorted(by_series.items(), key=lambda item: _series_sort_key(item[0]))
    ]
    groups: list[dict] = []
    for series in descriptors:
        target = next(
            (group for group in groups if _group_accepts(group, series, tolerance_nm)),
            None,
        )
        if target is None:
            groups.append({"members": [series]})
        else:
            target["members"].append(series)
    return sorted(
        groups,
        key=lambda group: (
            -len(group["members"]),
            _series_sort_key(group["members"][0]["series_index"]),
        ),
    )


def _group_summary(group: dict, group_id: str, role: str) -> dict:
    members = group["members"]
    channel_indices = sorted(members[0]["channels"])
    channels = []
    for index in channel_indices:
        rows = [member["channels"][index] for member in members]
        normalized = normalize_channel(rows[0])
        channels.append(
            {
                "channel_key": f"C{index:02d}",
                "modality": _modality(normalized)["value"],
                "detector": {
                    "name": normalized["detector_name"],
                    "type": normalized["detector_type"],
                    "scan_type": normalized["scan_type"],
                },
                "optical": {
                    field: _value_summary(rows, field)
                    for field in (
                        "excitation_wavelengths_nm",
                        "emission_window_begin_nm",
                        "emission_window_end_nm",
                    )
                },
            }
        )
    return {
        "group_id": group_id,
        "role": role,
        "series_count": len(members),
        "series": [
            {
                "series_index": member["series_index"],
                "series_name": member["series_name"],
            }
            for member in members
        ],
        "channel_count": len(channel_indices),
        "channel_keys": [f"C{index:02d}" for index in channel_indices],
        "channels": channels,
    }


def _merge_range_exceeds(left: dict, right: dict, tolerance_nm: float) -> bool:
    left_min, left_max = left["min"], left["max"]
    right_min, right_max = right["min"], right["max"]
    if any(value is None for value in (left_min, left_max, right_min, right_max)):
        return (left_min, left_max) != (right_min, right_max)
    if isinstance(left_min, list) or isinstance(right_min, list):
        if not isinstance(left_min, list) or not isinstance(right_min, list):
            return True
        if len(left_min) != len(right_min):
            return True
        return any(
            max(left_max[index], right_max[index])
            - min(left_min[index], right_min[index])
            > tolerance_nm
            for index in range(len(left_min))
        )
    return max(left_max, right_max) - min(left_min, right_min) > tolerance_nm


def _review_differences(groups: list[dict], tolerance_nm: float) -> list[dict]:
    if not groups:
        return []
    reference = groups[0]
    reference_channels = {item["channel_key"]: item for item in reference["channels"]}
    differences = []
    for variant in groups[1:]:
        variant_channels = {item["channel_key"]: item for item in variant["channels"]}
        if variant["channel_keys"] != reference["channel_keys"]:
            code = (
                "channel_count_varies"
                if variant["channel_count"] != reference["channel_count"]
                else "channel_layout_varies"
            )
            differences.append(
                {
                    "category": "channel_structure",
                    "code": code,
                    "field": "channel_keys",
                    "reference": reference["channel_keys"],
                    "variant": variant["channel_keys"],
                    "variant_group_id": variant["group_id"],
                }
            )
        for key in sorted(set(reference_channels) & set(variant_channels)):
            left = reference_channels[key]
            right = variant_channels[key]
            for field, left_value, right_value in (
                ("modality", left["modality"], right["modality"]),
                ("detector_name", left["detector"]["name"], right["detector"]["name"]),
                ("detector_type", left["detector"]["type"], right["detector"]["type"]),
                ("scan_type", left["detector"]["scan_type"], right["detector"]["scan_type"]),
            ):
                if left_value != right_value:
                    differences.append(
                        {
                            "category": "detector_modality",
                            "code": "modality_varies",
                            "channel_key": key,
                            "field": field,
                            "reference": left_value,
                            "variant": right_value,
                            "variant_group_id": variant["group_id"],
                        }
                    )
            for field in (
                "excitation_wavelengths_nm",
                "emission_window_begin_nm",
                "emission_window_end_nm",
            ):
                left_value = left["optical"][field]
                right_value = right["optical"][field]
                if _merge_range_exceeds(left_value, right_value, tolerance_nm):
                    differences.append(
                        {
                            "category": "optical",
                            "code": "optical_protocol_varies",
                            "channel_key": key,
                            "field": field,
                            "reference": left_value,
                            "variant": right_value,
                            "variant_group_id": variant["group_id"],
                            "tolerance_nm": tolerance_nm,
                        }
                    )
    for group in groups:
        for channel in group["channels"]:
            if channel["modality"] == "unknown":
                differences.append(
                    {
                        "category": "detector_modality",
                        "code": "unknown_modality",
                        "channel_key": channel["channel_key"],
                        "field": "modality",
                        "group_id": group["group_id"],
                    }
                )
            if channel["modality"] != "fluorescence":
                continue
            missing = [
                field
                for field, summary in channel["optical"].items()
                if summary["representative"] in (None, [])
            ]
            if missing:
                differences.append(
                    {
                        "category": "incomplete_metadata",
                        "code": "incomplete_fluorescence_optics",
                        "channel_key": channel["channel_key"],
                        "fields": missing,
                        "group_id": group["group_id"],
                    }
                )
    return differences


def _informational_variations(series_channels: list[dict]) -> list[dict]:
    by_channel: dict[int, list[dict]] = defaultdict(list)
    for row in series_channels:
        by_channel[int(row.get("channel_index", row.get("index", 0)))].append(row)
    variations = []
    fields = (
        ("gain", "gain_varies"),
        ("offset", "offset_varies"),
        ("excitation_intensities_percent", "laser_power_varies"),
    )
    for index, rows in sorted(by_channel.items()):
        normalized = [normalize_channel(row) for row in rows]
        for field, code in fields:
            distributions: dict[str, dict] = {}
            for row, item in zip(rows, normalized):
                value = item[field]
                key = _value_key(value)
                entry = distributions.setdefault(key, {"value": value, "series": []})
                entry["series"].append(
                    {
                        "series_index": row.get("series_index"),
                        "series_name": _text(row.get("series_name")),
                    }
                )
            if len(distributions) <= 1:
                continue
            values = sorted(
                distributions.values(),
                key=lambda entry: (-len(entry["series"]), _value_key(entry["value"])),
            )
            variations.append(
                {
                    "category": "acquisition_setting",
                    "code": code,
                    "channel_key": f"C{index:02d}",
                    "field": field,
                    "reference_value": values[0]["value"],
                    "distinct_value_count": len(values),
                    "values": [
                        {
                            "value": entry["value"],
                            "series_count": len(entry["series"]),
                            "series": entry["series"],
                        }
                        for entry in values
                    ],
                }
            )
    return variations


def build_workflow_review(
    series_channels: list[dict],
    consistency: dict,
    workflow: dict,
    *,
    optical_tolerance_nm: float = OPTICAL_TOLERANCE_NM,
) -> dict:
    clustered = _cluster_series(series_channels, optical_tolerance_nm)
    groups = []
    for index, group in enumerate(clustered):
        is_reference = index == 0
        group_id = "RG-001" if is_reference else f"VG-{index:03d}"
        groups.append(
            _group_summary(group, group_id, "reference" if is_reference else "variant")
        )
    variant_ids = [group["group_id"] for group in groups[1:]]
    if workflow["route"] == "review":
        decision = {
            "status": "pending",
            "action": None,
            "source": None,
            "accepted_reason_codes": [],
            "reviewed_variant_group_ids": [],
            "note": None,
        }
    else:
        decision = {
            "status": "not_required",
            "action": "continue_all",
            "source": "automatic_standard_route",
            "accepted_reason_codes": [],
            "reviewed_variant_group_ids": [],
            "note": None,
        }
    return {
        "schema_name": "candida_lif_workflow_review",
        "schema_version": "0.1",
        "route": workflow["route"],
        "status": workflow["status"],
        "reason_codes": workflow["reason_codes"],
        "issues": consistency["issues"],
        "comparison_policy": {
            "reference_group": "most frequent acquisition protocol; not a correctness claim",
            "optical_tolerance_nm": optical_tolerance_nm,
            "optical_rule": "combined max-min must be <= tolerance",
            "informational_fields": ["gain", "offset", "excitation_intensities_percent"],
        },
        "reference_group_id": groups[0]["group_id"] if groups else None,
        "variant_group_ids": variant_ids,
        "groups": groups,
        "review_differences": _review_differences(groups, optical_tolerance_nm),
        "informational_variations": _informational_variations(series_channels),
        "decision": decision,
    }


def summarize_resolved_channels(
    metadata: list[dict],
    registry: dict | None = None,
    protocol_registry: dict | None = None,
    user_assignments: dict[int, str] | None = None,
) -> dict:
    registry = registry or load_registry()
    protocol_registry = protocol_registry or load_protocol_registry()
    user_assignments = user_assignments or {}
    resolved_rows = []
    raw_rows = []
    for series in metadata:
        for channel in series.get("channel_info", []):
            enriched_channel = dict(channel)
            if not enriched_channel.get("excitation_settings"):
                lasers = series.get("laser_settings") or []
                enriched_channel["all_visible_laser_wavelengths_nm"] = [
                    item.get("wavelength_nm") for item in lasers
                ]
                enriched_channel["all_visible_laser_intensities_percent"] = [
                    item.get("intensity_percent") for item in lasers
                ]
            row = {
                **enriched_channel,
                "series_index": series.get("series_index"),
                "series_name": series.get("series_name"),
            }
            raw_rows.append(row)
            resolved_rows.append(
                {
                    "series_index": series.get("series_index"),
                    "series_name": series.get("series_name"),
                    **resolve_channel(
                        enriched_channel,
                        series_name=series.get("series_name", ""),
                        source_name=series.get("source_file", ""),
                        user_assignment=user_assignments.get(int(channel.get("index", 0))),
                        registry=registry,
                        protocol_registry=protocol_registry,
                    ),
                }
            )

    by_channel = defaultdict(list)
    for row in resolved_rows:
        by_channel[row["channel_index"]].append(row)
    channels = []
    for index, rows in sorted(by_channel.items()):
        modalities = Counter(row["modality"]["value"] for row in rows)
        directories = Counter(row["directory"] for row in rows)
        candidate_map = {}
        for row in rows:
            for candidate in row["stain_candidates"]:
                previous = candidate_map.get(candidate["id"])
                if previous is None or CONFIDENCE_ORDER[candidate["confidence"]] > CONFIDENCE_ORDER[
                    previous["confidence"]
                ]:
                    candidate_map[candidate["id"]] = candidate
        candidates = sorted(
            candidate_map.values(),
            key=lambda item: (-CONFIDENCE_ORDER[item["confidence"]], item["id"]),
        )
        inferred = next(
            (item for item in candidates if item["kind"] == "stain"),
            None,
        )
        confirmed = next(
            (row["confirmed_assignment"] for row in rows if row["confirmed_assignment"]),
            None,
        )
        channels.append(
            {
                "channel_key": f"C{index:02d}",
                "series_presence": len(rows),
                "directory_suggestion": directories.most_common(1)[0][0],
                "modality": modalities.most_common(1)[0][0],
                "modality_variants": dict(modalities),
                "stain_candidates": candidates,
                "inferred_dye": inferred,
                "confirmed_assignment": confirmed,
                "confirmed_dye": (
                    confirmed if confirmed and confirmed.get("kind") == "stain" else None
                ),
            }
        )
    consistency = assess_consistency(raw_rows)
    for channel in channels:
        key = channel["channel_key"]
        warnings = [
            issue
            for issue in consistency["issues"]
            if issue["severity"] == "warning"
            and (issue.get("channel_key") in {None, key})
        ]
        if warnings:
            channel["review_status"] = "needs_review"
        elif channel["confirmed_assignment"] is not None:
            channel["review_status"] = "confirmed"
        elif channel["modality"] == "brightfield":
            channel["review_status"] = "resolved_modality"
        elif channel["inferred_dye"] is not None:
            channel["review_status"] = "candidate"
        else:
            channel["review_status"] = "unknown"
    workflow = decide_workflow(consistency)
    return {
        "schema_name": "candida_channel_resolution",
        "schema_version": "0.3",
        "series_count": len(metadata),
        "channels": channels,
        "consistency": consistency,
        "workflow": workflow,
        "workflow_review": build_workflow_review(
            raw_rows,
            consistency,
            workflow,
            optical_tolerance_nm=consistency["optical_tolerance_nm"],
        ),
        "user_assignments": {
            f"C{index}": assignment for index, assignment in sorted(user_assignments.items())
        },
        "series_channels": resolved_rows,
    }


def compact_channel_table(report: dict) -> str:
    lines = [
        "Channel  Directory          Modality (confidence)       Stain candidates",
        "-------  -----------------  --------------------------  ----------------",
    ]
    for channel in report["channels"]:
        candidates = channel["stain_candidates"]
        candidate_text = ", ".join(
            f"{item['id']} ({item['confidence']})"
            for item in candidates
            if item["kind"] == "stain" and item["confidence"] != "low"
        ) or "-"
        representative = next(
            row
            for row in report["series_channels"]
            if row["channel_index"] == int(channel["channel_key"][1:])
            and row["modality"]["value"] == channel["modality"]
        )
        modality_text = (
            f"{channel['modality']} ({representative['modality']['confidence']})"
        )
        lines.append(
            f"{channel['channel_key']:<7}  "
            f"{channel['directory_suggestion']:<17}  "
            f"{modality_text:<26}  "
            f"{candidate_text}"
        )
    lines.append("")
    lines.append(f"Consistency: {report['consistency']['status']}")
    for issue in report["consistency"]["issues"]:
        lines.append(f"- {issue['severity']}: {issue['message']}")
    workflow = report["workflow"]
    lines.append("")
    lines.append(f"Workflow: {workflow['route']} ({workflow['status']})")
    if workflow["reason_codes"]:
        lines.append("Reasons: " + ", ".join(workflow["reason_codes"]))
    review = report.get("workflow_review") or {}
    groups = review.get("groups") or []
    if groups:
        group_counts = ", ".join(
            f"{group['group_id']}={group['series_count']} series" for group in groups
        )
        lines.append(f"Protocol groups: {group_counts}")
    return "\n".join(lines)
