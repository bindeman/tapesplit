from tapesplit.claims import _enrich_events_from_claims, _events_from_event_claims, _normalize_event_candidates


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


def test_enrich_events_from_claims_adds_shared_evidence_metadata():
    events = [
        {
            "title": "Child greeting grandparents",
            "evidence_ids": ["ev_1", "ev_2"],
            "metadata": {},
        }
    ]
    claims = [
        {
            "predicate": "person_mention",
            "value": "Person named 'Филя' (Filia) mentioned",
            "evidence_ids": ["ev_1"],
        },
        {
            "predicate": "language",
            "value": "Russian language used in transcript",
            "evidence_ids": ["ev_2"],
        },
        {
            "predicate": "date_candidate",
            "value": "Digitization or export date around 2026-03-16",
            "notes": "treated as digitization/export date",
            "evidence_ids": ["ev_1"],
        },
    ]

    enriched = _enrich_events_from_claims(events, claims)

    assert enriched[0]["metadata"] == {
        "people": ["Филя"],
        "languages": ["Russian"],
    }


def test_enrich_events_from_claims_extracts_named_child_from_event_claim():
    events = [
        {
            "title": "Child named Filia interaction",
            "evidence_ids": ["ev_1"],
            "metadata": {},
        }
    ]
    claims = [
        {
            "predicate": "event",
            "value": "Child named Filia (Филя) is asked about age",
            "evidence_ids": ["ev_1"],
        }
    ]

    enriched = _enrich_events_from_claims(events, claims)

    assert enriched[0]["metadata"] == {
        "event_type": "family",
        "people": ["Filia"],
    }
