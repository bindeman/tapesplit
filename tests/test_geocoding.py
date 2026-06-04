from tapesplit.geocoding import build_place_query, geocode_candidate


def test_build_place_query_uses_context():
    assert (
        build_place_query("Maplewood Elementary School", city="Eugene", region="OR", country="US")
        == "Maplewood Elementary School Eugene OR US"
    )


def test_geocode_candidate_dry_run_does_not_call_api():
    result = geocode_candidate(name="Maplewood Elementary School", city="Eugene", region="OR")

    assert result["api_called"] is False
    assert result["query"] == "Maplewood Elementary School Eugene OR"
