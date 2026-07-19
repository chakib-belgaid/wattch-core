import json

from pywattch._frames import FrameKind
from pywattch._messages import (
    Event,
    EventBatch,
    HelloResponse,
    RawSample,
    RawSampleBatch,
    RecordEventAck,
    decode_payload,
    encode_record_event,
)


def test_v02_event_messages_match_rust_json_shape():
    encoded = encode_record_event(
        "run-1", "target-spawn", "TARGET_SPAWN", {"program": "p1"}
    )
    assert json.loads(encoded) == {
        "run_id": "run-1",
        "event_id": "target-spawn",
        "kind": "TARGET_SPAWN",
        "labels": {"program": "p1"},
    }

    event_payload = {
        "event_id": "target-spawn",
        "run_id": "run-1",
        "node_id": "node-0",
        "monotonic_ns": 42,
        "kind": "TARGET_SPAWN",
        "labels": {"program": "p1"},
    }
    batch = decode_payload(
        FrameKind.EVENT_BATCH,
        json.dumps(
            {
                "run_id": "run-1",
                "node_id": "node-0",
                "batch_sequence_no": 3,
                "events": [event_payload],
            }
        ).encode(),
    )
    assert batch == EventBatch(
        run_id="run-1",
        node_id="node-0",
        batch_sequence_no=3,
        events=(Event(**event_payload),),
    )

    ack = decode_payload(
        FrameKind.RECORD_EVENT_ACK,
        json.dumps({"event": event_payload}).encode(),
    )
    assert ack == RecordEventAck(event=Event(**event_payload))


def test_legacy_hello_defaults_clock_identity_and_raw_sample_shape_is_unchanged():
    hello = decode_payload(
        FrameKind.HELLO_RESPONSE,
        b'{"daemon_name":"d","daemon_version":"0.1.0",'
        b'"protocol_version":"0.1.0","node_id":"n","sources":[]}',
    )
    assert isinstance(hello, HelloResponse)
    assert hello.clock_instance_id == ""

    raw = RawSampleBatch(
        run_id="legacy",
        node_id="n",
        batch_sequence_no=0,
        samples=(RawSample(7, 9, 99, 12.5),),
    )
    assert raw.to_dict() == {
        "run_id": "legacy",
        "node_id": "n",
        "batch_sequence_no": 0,
        "samples": [
            {
                "source_id": 7,
                "sequence_no": 9,
                "monotonic_ns": 99,
                "value": {"type": "f64", "value": 12.5},
            }
        ],
    }
