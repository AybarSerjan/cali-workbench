"""A2L asset index: MEASUREMENT dictionary + FUNCTION graph (Phase A of the
MF4+A2L+HEX auto-validation flow).

Deterministic, zero-LLM. The index is content-addressed by the A2L file's
SHA-256 under ``data/a2l_index/{sha}/`` and never rewritten once built —
consumers (signal matching, traceback, the LLM drafter) only read the index,
never the raw A2L text.
"""
from __future__ import annotations

import difflib
import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.config import ROOT
from backend.models import DatasetFile
from backend.services.calibration import (
    _dataset_file_path,
    _strip_comments,
    parse_byte_order,
    parse_characteristics,
    parse_compu_methods,
    parse_functions,
    parse_measurements,
)
from backend.services.runs import json_write

INDEX_SCHEMA_VERSION = 2


def _index_dir(sha: str) -> Path:
    return ROOT / "data" / "a2l_index" / sha / f"v{INDEX_SCHEMA_VERSION}"


# ponytail: module-level cache keyed by sha (single-user process), mirrors the
# a2l defs cache used by the evolution report.
_CACHE: dict[str, dict[str, Any]] = {}


def build_index(db: Session, dataset_file_id: str) -> dict[str, Any]:
    """Parse one A2L dataset file into the on-disk index. Idempotent: an
    existing index for the same sha is returned untouched."""
    item = db.get(DatasetFile, dataset_file_id)
    if not item:
        raise ValueError(f"数据集文件不存在: {dataset_file_id}")
    if item.suffix.lower() != ".a2l":
        raise ValueError(f"需要 .a2l 文件: {item.original_name}")
    path = _dataset_file_path(item)
    sha = item.sha256 or hashlib.sha256(path.read_bytes()).hexdigest()
    if (_index_dir(sha) / "meta.json").exists():
        return meta(sha)

    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    text = _strip_comments(text)

    methods = parse_compu_methods(text)
    measurements = parse_measurements(text, methods)
    functions = parse_functions(text)
    characteristics = parse_characteristics(text, methods)
    byte_order = parse_byte_order(text)

    target = _index_dir(sha)
    json_write(target / "measurements.json", {k: asdict(v) for k, v in measurements.items()})
    json_write(target / "functions.json", {k: asdict(v) for k, v in functions.items()})
    json_write(target / "meta.json", {
        "schema_version": INDEX_SCHEMA_VERSION,
        "sha256": sha,
        "source_name": item.original_name,
        "byte_order": byte_order,
        "counts": {
            "measurements": len(measurements),
            "characteristics": len(characteristics),
            "functions": len(functions),
        },
    })
    _CACHE.pop(sha, None)
    return meta(sha)


def _load(sha: str) -> dict[str, Any]:
    if sha in _CACHE:
        return _CACHE[sha]
    target = _index_dir(sha)
    if not (target / "meta.json").exists():
        raise ValueError(f"A2L 索引不存在，请先构建: {sha}")
    measurements = json.loads((target / "measurements.json").read_text(encoding="utf-8"))
    functions = json.loads((target / "functions.json").read_text(encoding="utf-8"))
    # Reverse graph, built once per load: signal -> producing / consuming
    # functions, characteristic -> owning functions.
    out_to_function: dict[str, list[str]] = {}
    in_to_functions: dict[str, list[str]] = {}
    char_to_functions: dict[str, list[str]] = {}
    for name, fn in functions.items():
        for sig in fn.get("out_measurements") or []:
            out_to_function.setdefault(sig, []).append(name)
        for sig in fn.get("in_measurements") or []:
            in_to_functions.setdefault(sig, []).append(name)
        for param in (fn.get("def_characteristics") or []) + (fn.get("ref_characteristics") or []):
            char_to_functions.setdefault(param, []).append(name)
    _CACHE[sha] = {
        "meta": json.loads((target / "meta.json").read_text(encoding="utf-8")),
        "measurements": measurements,
        "functions": functions,
        "out_to_function": out_to_function,
        "in_to_functions": in_to_functions,
        "char_to_functions": char_to_functions,
    }
    return _CACHE[sha]


def meta(sha: str) -> dict[str, Any]:
    return _load(sha)["meta"]


def search_measurements(sha: str, query: str = "", limit: int = 50) -> list[dict[str, Any]]:
    """Case-insensitive substring search over name + description. 25k entries
    scan in-memory in milliseconds; no FTS needed for a single-user tool."""
    index = _load(sha)
    q = query.casefold()
    hits: list[dict[str, Any]] = []
    for name, mi in index["measurements"].items():
        if not q or q in name.casefold() or q in (mi.get("description") or "").casefold():
            hits.append(mi)
            if len(hits) >= limit:
                break
    return hits


def get_measurement(sha: str, name: str) -> dict[str, Any] | None:
    return _load(sha)["measurements"].get(name)


def _function_role(fn: dict[str, Any]) -> dict[str, Any]:
    return {
        "function": fn["name"],
        "description": fn.get("description", ""),
        "def_characteristics": fn.get("def_characteristics") or [],
        "ref_characteristics": fn.get("ref_characteristics") or [],
        "in_measurements": fn.get("in_measurements") or [],
        "out_measurements": fn.get("out_measurements") or [],
    }


def trace_query(sha: str, signal: str, depth: int = 2) -> dict[str, Any]:
    """Signal → producing FUNCTION → candidate CHARACTERISTIC set, with
    upstream recursion over IN_MEASUREMENT and consumer cross-evidence.
    This graph is the hard boundary for traceback candidates."""
    index = _load(sha)
    functions = index["functions"]

    producers = [_function_role(functions[f]) for f in index["out_to_function"].get(signal, [])]
    consumers = [f for f in index["in_to_functions"].get(signal, [])]

    upstream: list[dict[str, Any]] = []
    seen = {f["function"] for f in producers}
    frontier = [signal]
    for level in range(1, max(depth, 1)):
        next_frontier: list[str] = []
        for sig in frontier:
            for fname in index["out_to_function"].get(sig, []):
                fn = functions[fname]
                for upstream_sig in fn.get("in_measurements") or []:
                    for up_fname in index["out_to_function"].get(upstream_sig, []):
                        if up_fname in seen:
                            continue
                        seen.add(up_fname)
                        upstream.append({**_function_role(functions[up_fname]), "level": level, "via_signal": upstream_sig})
                    next_frontier.append(upstream_sig)
        frontier = next_frontier
        if not frontier:
            break

    candidates: list[str] = []
    for fn in producers:
        candidates.extend(fn["def_characteristics"])
        candidates.extend(fn["ref_characteristics"])
    return {
        "signal": signal,
        "found": signal in index["measurements"] or bool(producers),
        "producers": producers,
        "candidate_characteristics": sorted(dict.fromkeys(candidates)),
        "upstream": upstream,
        "consumers": consumers,
    }


def variant_family(sha: str, name: str) -> list[dict[str, Any]]:
    """Deterministic variant derivation: same COMPU_METHOD plus a prefix
    relation (nmot_w / nmot_w_can) or near-identical description."""
    index = _load(sha)
    base = index["measurements"].get(name)
    if not base:
        return []
    base_desc = (base.get("description") or "").casefold()
    out: list[dict[str, Any]] = []
    for other_name, other in index["measurements"].items():
        if other_name == name or other.get("compu_method") != base.get("compu_method"):
            continue
        prefix = other_name.startswith(name + "_") or name.startswith(other_name + "_")
        desc = (other.get("description") or "").casefold()
        similar = bool(base_desc) and bool(desc) and difflib.SequenceMatcher(None, base_desc, desc).ratio() >= 0.85
        if prefix or similar:
            out.append({**other, "variant_reason": "name_prefix" if prefix else "description_similar"})
    return out
