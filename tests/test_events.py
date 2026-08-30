from ash.events import read_events, record_event


def test_record_and_read_events(tmp_path) -> None:
    path = tmp_path / "events.jsonl"
    event = record_event(
        source="test",
        kind="alert",
        title="Hello",
        body="World",
        metadata={"x": 1},
        path=path,
    )

    events = read_events(limit=5, path=path)

    assert event.id.startswith("evt_")
    assert events[0]["title"] == "Hello"
    assert events[0]["metadata"] == {"x": 1}
