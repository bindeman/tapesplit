import json
from pathlib import Path

from tapesplit.geocoding import (
    GEOCODES_FILENAME,
    _context_suspect,
    _place_queries,
    _result_confidence,
    build_place_query,
    geocode_candidate,
    load_place_geocodes,
    normalized_geocode_key,
)
from tapesplit.visualization import _attach_place_geocodes


def test_build_place_query_uses_context():
    assert (
        build_place_query("Maplewood Elementary School", city="Eugene", region="OR", country="US")
        == "Maplewood Elementary School Eugene OR US"
    )


def test_geocode_candidate_dry_run_does_not_call_api():
    result = geocode_candidate(name="Maplewood Elementary School", city="Eugene", region="OR")

    assert result["api_called"] is False
    assert result["query"] == "Maplewood Elementary School Eugene OR"


# ----------------------------------------------------------- nominatim batch


def test_context_suspect_flags_disjoint_regions():
    assert _context_suspect({"scope_label": "Moscow, Oregon", "parent_place_labels": []})
    assert _context_suspect({"scope_label": None, "parent_place_labels": ["Alaska", "Davos, Switzerland"]})
    assert not _context_suspect({"scope_label": "Eugene, Oregon", "parent_place_labels": ["Oregon"]})


def test_place_queries_drop_hints_for_suspect_contexts():
    suspect = {"label": "El Matador Beach", "scope_label": "Oregon, Wisconsin", "parent_place_labels": []}
    assert _place_queries(suspect) == ["El Matador Beach"]

    trusted = {"label": "Maplewood School", "scope_label": None, "parent_place_labels": ["Eugene", "Oregon"]}
    queries = _place_queries(trusted)
    assert queries[0] == "Maplewood School Eugene Oregon"
    assert queries[-1] == "Maplewood School"


def test_result_confidence_rewards_unambiguous_results():
    single = _result_confidence([{"importance": 0.4}])
    multi = _result_confidence([{"importance": 0.4}, {"importance": 0.3}])
    assert single > multi
    assert _result_confidence([]) == 0.0


def _write_cache(project: Path, rows: list[dict]) -> None:
    project.mkdir(exist_ok=True)
    (project / GEOCODES_FILENAME).write_text(
        "\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8"
    )


def test_load_place_geocodes_keeps_best_row_per_label(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    _write_cache(
        project,
        [
            {"place_label": "Kolomna", "selected": {"lat": 55.1, "lng": 38.8}, "confidence": 0.5},
            {"place_label": "Kolomna", "selected": {"lat": 55.09, "lng": 38.77}, "confidence": 0.9},
            {"place_label": "Nowhere", "selected": None, "confidence": 0.0},
        ],
    )
    cache = load_place_geocodes(project)
    assert cache[normalized_geocode_key("Kolomna")]["confidence"] == 0.9
    assert normalized_geocode_key("Nowhere") not in cache


def test_attach_place_geocodes_fills_selected_geocode_hook(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    _write_cache(
        project,
        [
            {
                "place_label": "Jakobshorn",
                "selected": {
                    "lat": 46.77,
                    "lng": 9.85,
                    "formatted_address": "Jakobshorn, Davos, Switzerland",
                    "city": "Davos",
                    "country": "Switzerland",
                },
                "confidence": 0.95,
                "context_suspect": False,
                "alternatives": [],
            }
        ],
    )
    places = [
        {"id": "pg_1", "label": "Jakobshorn", "kind": "named_place_candidate"},
        {"id": "pg_2", "label": "home", "kind": "generic_place_context"},
    ]
    _attach_place_geocodes(project, places)

    enriched = places[0]
    assert enriched["metadata"]["selected_geocode"] == {"lat": 46.77, "lng": 9.85}
    assert enriched["geocode"]["approximate"] is False
    assert enriched["geocode"]["country"] == "Switzerland"
    assert "geocode" not in places[1]


def test_attach_marks_low_confidence_and_suspect_as_approximate(tmp_path: Path):
    project = tmp_path / "p.tapesplit"
    _write_cache(
        project,
        [
            {
                "place_label": "Maplewood School",
                "selected": {"lat": 25.8, "lng": -80.3, "city": "Hialeah"},
                "confidence": 0.45,
                "context_suspect": True,
                "alternatives": [{}, {}],
            }
        ],
    )
    places = [{"id": "pg_1", "label": "Maplewood School", "kind": "named_place_candidate"}]
    _attach_place_geocodes(project, places)
    assert places[0]["geocode"]["approximate"] is True
    assert places[0]["geocode"]["context_suspect"] is True
    assert places[0]["geocode"]["alternatives"] == 2
