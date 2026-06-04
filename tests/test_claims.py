from tapesplit.claims import _events_from_event_claims, _normalize_event_candidates


def test_normalize_event_candidates_uses_cited_evidence_seconds():
    events = [
        {
            "title": "Philip losing his second tooth",
            "start_s": 13.32,
            "end_s": 13.4,
            "evidence_ids": ["ev_1"],
        }
    ]
    evidence = {
        "ev_1": {
            "id": "ev_1",
            "kind": "twelvelabs_search_transcript",
            "start_s": 812.25,
            "end_s": 819.5,
        }
    }

    normalized = _normalize_event_candidates(events, evidence)

    assert normalized[0]["start_s"] == 812.25
    assert normalized[0]["end_s"] == 819.5


def test_normalize_event_candidates_drops_non_content_only_events():
    events = [
        {
            "title": "Blue screen",
            "start_s": 1,
            "end_s": 2,
            "evidence_ids": ["ev_1"],
        }
    ]
    evidence = {
        "ev_1": {
            "id": "ev_1",
            "kind": "non_content_range",
            "start_s": 4150,
            "end_s": 4786.915,
        }
    }

    assert _normalize_event_candidates(events, evidence) == []


def test_events_from_event_claims_builds_fallback_timeline_events():
    claims = [
        {
            "predicate": "event",
            "value": "Philip loses a second tooth",
            "confidence": 0.7,
            "evidence_ids": ["ev_1"],
            "notes": "Transcript states Philip loses a second tooth.",
        }
    ]
    evidence = {
        "ev_1": {
            "id": "ev_1",
            "kind": "twelvelabs_search_transcript",
            "start_s": 812.25,
            "end_s": 819.5,
        }
    }

    events = _events_from_event_claims(claims, evidence)

    assert events == [
        {
            "title": "Philip loses a second tooth",
            "start_s": 812.25,
            "end_s": 819.5,
            "confidence": 0.7,
            "evidence_ids": ["ev_1"],
            "summary": "Transcript states Philip loses a second tooth.",
            "review_status": "unreviewed",
            "derived_from": "event_claim",
        }
    ]
