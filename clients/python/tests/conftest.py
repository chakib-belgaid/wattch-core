"""A pure-Python fake Wattch daemon speaking protocol v0.2 for tests."""

from __future__ import annotations

import json
import os
import select
import socket
import threading
import time
import uuid
from typing import Callable

import pytest

from pywattch._frames import FrameKind, read_frame, write_frame

MICROJOULE_DESCRIPTOR = {
    "local_source_id": 1,
    "source_uid": "fake://fake-node/cpu/package/0/energy",
    "node_id": "fake-node",
    "backend": "fake",
    "kind": "CPU_PACKAGE_ENERGY",
    "unit": "MICROJOULE",
    "temporality": "CUMULATIVE",
    "evidence_class": "RAW_HARDWARE_COUNTER",
    "source_path": "cpu/package/0/energy",
    "parent_source_uid": None,
    "clock_domain": "monotonic",
    "sampling_mode": "SYNTHETIC",
    "nominal_period_ns": None,
    "max_value": 1000000.0,
    "resolution": 1.0,
    "labels": {},
}

DRAM_DESCRIPTOR = {
    **MICROJOULE_DESCRIPTOR,
    "local_source_id": 2,
    "source_uid": "fake://fake-node/cpu/package/0/dram/energy",
    "kind": "DRAM_ENERGY",
    "source_path": "cpu/package/0/dram/energy",
    "parent_source_uid": "fake://fake-node/cpu/package/0/energy",
}

GPU_DESCRIPTOR = {
    **MICROJOULE_DESCRIPTOR,
    "local_source_id": 3,
    "source_uid": "fake://fake-node/gpu/0/power",
    "kind": "GPU_POWER",
    "unit": "WATT",
    "temporality": "INSTANT",
    "evidence_class": "RAW_SENSOR_READING",
    "source_path": "gpu/0/power",
    "max_value": None,
}


def default_value_fn(tick: int) -> list[tuple[int, dict]]:
    """Package +1000 uJ/tick, DRAM +200 uJ/tick, GPU constant 10 W."""
    return [
        (1, {"type": "i64", "value": 10_000 + 1_000 * tick}),
        (2, {"type": "i64", "value": 5_000 + 200 * tick}),
        (3, {"type": "f64", "value": 10.0}),
    ]


class FakeDaemon:
    def __init__(
        self,
        descriptors: list[dict] | None = None,
        value_fn: Callable[[int], list[tuple[int, dict]]] = default_value_fn,
        node_id: str = "fake-node",
    ):
        self.descriptors = descriptors or [
            MICROJOULE_DESCRIPTOR,
            DRAM_DESCRIPTOR,
            GPU_DESCRIPTOR,
        ]
        self.value_fn = value_fn
        self.node_id = node_id
        self.socket_path = os.path.join(
            "/tmp", f"wattch-fake-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
        )
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self.socket_path)
        self._listener.listen(4)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        try:
            waker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            waker.connect(self.socket_path)
            waker.close()
        except OSError:
            pass
        self._thread.join(timeout=2)
        self._listener.close()
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)

    # -- internals ----------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            if self._stop.is_set():
                conn.close()
                return
            threading.Thread(
                target=self._handle_connection, args=(conn,), daemon=True
            ).start()

    def _send(self, conn: socket.socket, kind: FrameKind, payload: dict) -> None:
        write_frame(conn, kind, json.dumps(payload).encode())

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            kind, payload = read_frame(conn)
            if kind != FrameKind.HELLO_REQUEST:
                self._send(
                    conn, FrameKind.ERROR, {"code": 1, "message": "expected hello"}
                )
                return
            self._send(
                conn,
                FrameKind.HELLO_RESPONSE,
                {
                    "daemon_name": "fake-daemon",
                    "daemon_version": "0.0.0",
                    "protocol_version": "0.2.0",
                    "node_id": self.node_id,
                    "clock_instance_id": "fake-clock-instance",
                    "sources": self.descriptors,
                },
            )

            tick = 0
            batch_seq = 0
            event_batch_seq = 0
            start_ns = time.monotonic_ns()
            while not self._stop.is_set():
                kind, payload = read_frame(conn)
                if kind != FrameKind.START_CAPTURE:
                    self._send(
                        conn,
                        FrameKind.ERROR,
                        {"code": 1, "message": f"unexpected {kind.name}"},
                    )
                    continue
                request = json.loads(payload)
                known = {d["local_source_id"] for d in self.descriptors}
                requested = request.get("source_ids") or sorted(known)
                unknown = [i for i in requested if i not in known]
                if unknown:
                    self._send(
                        conn,
                        FrameKind.ERROR,
                        {"code": 3, "message": f"unknown source id {unknown[0]}"},
                    )
                    continue
                self._send(
                    conn,
                    FrameKind.START_CAPTURE_ACK,
                    {"run_id": request["run_id"], "accepted_source_ids": requested},
                )

                interval_s = max(request.get("interval_ms", 50), 1) / 1000.0
                batches_sent = 0
                samples_sent = 0
                while True:
                    samples = [
                        {
                            "source_id": source_id,
                            "sequence_no": tick,
                            "monotonic_ns": time.monotonic_ns() - start_ns,
                            "value": value,
                        }
                        for source_id, value in self.value_fn(tick)
                        if source_id in requested
                    ]
                    tick += 1
                    self._send(
                        conn,
                        FrameKind.RAW_SAMPLE_BATCH,
                        {
                            "run_id": request["run_id"],
                            "node_id": self.node_id,
                            "batch_sequence_no": batch_seq,
                            "samples": samples,
                        },
                    )
                    batch_seq += 1
                    batches_sent += 1
                    samples_sent += len(samples)

                    # Poll for StopCapture without tearing frames.
                    readable, _, _ = select.select([conn], [], [], interval_s)
                    if readable:
                        kind, control_payload = read_frame(conn)
                        if kind == FrameKind.STOP_CAPTURE:
                            self._send(
                                conn,
                                FrameKind.STOP_CAPTURE_ACK,
                                {
                                    "run_id": request["run_id"],
                                    "batches_sent": batches_sent,
                                    "samples_sent": samples_sent,
                                },
                            )
                            break
                        if kind == FrameKind.RECORD_EVENT:
                            marker = json.loads(control_payload)
                            event = {
                                "event_id": marker["event_id"],
                                "run_id": marker["run_id"],
                                "node_id": self.node_id,
                                "monotonic_ns": time.monotonic_ns() - start_ns,
                                "kind": marker["kind"],
                                "labels": marker.get("labels") or {},
                            }
                            self._send(
                                conn,
                                FrameKind.EVENT_BATCH,
                                {
                                    "run_id": request["run_id"],
                                    "node_id": self.node_id,
                                    "batch_sequence_no": event_batch_seq,
                                    "events": [event],
                                },
                            )
                            event_batch_seq += 1
                            self._send(
                                conn,
                                FrameKind.RECORD_EVENT_ACK,
                                {"event": event},
                            )
        except Exception:
            pass
        finally:
            conn.close()


@pytest.fixture
def fake_daemon():
    daemon = FakeDaemon()
    yield daemon
    daemon.close()
