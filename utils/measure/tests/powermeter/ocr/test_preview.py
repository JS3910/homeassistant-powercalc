from __future__ import annotations

from collections.abc import Iterator
import http.client
import json
import logging
import threading
import time
from urllib.parse import urlparse

import cv2
from measure.powermeter.ocr.engine import TextBox
from measure.powermeter.ocr.preview import PreviewServer, annotate
from measure.powermeter.ocr.reader import DisplayReading, Location
import numpy as np
import pytest

from tests.powermeter.ocr.conftest import PF_BOX, POWER_BOX, VI_BOX, blank_frame, rect_of


@pytest.fixture
def server() -> Iterator[PreviewServer]:
    preview = PreviewServer("127.0.0.1", 0).start()
    yield preview
    preview.close()


def connect(server: PreviewServer) -> http.client.HTTPConnection:
    url = urlparse(server.url)
    assert url.hostname is not None
    assert url.port is not None
    return http.client.HTTPConnection(url.hostname, url.port, timeout=5)


def located_reading(accepted: bool = True, angle: float = 0.0) -> DisplayReading:
    location = Location(
        angle=angle,
        regions={
            "power": rect_of(POWER_BOX),
            "voltage": rect_of(VI_BOX),
            "current": rect_of(VI_BOX),
            "pf": rect_of(PF_BOX),
        },
        boxes=(TextBox(POWER_BOX, "Power4.60w"),),
    )
    return DisplayReading(
        timestamp=1000.0,
        accepted=accepted,
        reason=None if accepted else "pf unreadable: 'PF'",
        power=4.6,
        voltage=233.0,
        current=0.055,
        pf=0.354 if accepted else None,
        raw={"power": "Power4.60w", "voltage": "V233.0vC0.055A", "current": "V233.0vC0.055A", "pf": "PF0.354"},
        location=location,
    )


def test_annotate_draws_on_a_copy_and_marks_accept_and_reject() -> None:
    frame = blank_frame()
    accepted = annotate(frame, located_reading(True), fps=2.2)
    rejected = annotate(frame, located_reading(False), fps=2.2)

    assert (frame == 40).all(), "the source frame is left untouched"
    assert accepted.shape == frame.shape
    assert not np.array_equal(accepted, rejected)
    # Region boxes are green when accepted and red when rejected (BGR).
    x1, y1, _, _ = rect_of(POWER_BOX)
    assert tuple(accepted[y1, x1 + 20]) == (80, 200, 80)
    assert tuple(rejected[y1, x1 + 20]) == (60, 60, 230)


def test_annotate_shows_the_levelled_frame() -> None:
    image = annotate(blank_frame(), located_reading(angle=90.0), fps=2.2)
    assert image.shape == (800, 600, 3)


def test_annotate_without_a_location_reports_not_located() -> None:
    reading = DisplayReading(timestamp=1000.0, accepted=False, reason="display not located")
    image = annotate(blank_frame(), reading, fps=0.0)
    assert image.shape == (600, 800, 3)
    assert not (image == 40).all(), "status text is drawn"


def test_pages_and_state(server: PreviewServer) -> None:
    connection = connect(server)

    connection.request("GET", "/")
    response = connection.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/html; charset=utf-8"
    assert b"/events" in response.read()

    connection.request("GET", "/state.json?x=1")
    response = connection.getresponse()
    assert response.status == 200
    assert json.loads(response.read()) == {}

    connection.request("GET", "/frame.jpg")
    response = connection.getresponse()
    assert response.status == 503
    response.read()

    connection.request("GET", "/nope")
    response = connection.getresponse()
    assert response.status == 404
    response.read()

    server.publish(blank_frame(), {"power": 4.6, "frames": 1})

    connection.request("GET", "/state.json")
    response = connection.getresponse()
    assert json.loads(response.read()) == {"power": 4.6, "frames": 1}

    connection.request("GET", "/frame.jpg")
    response = connection.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type") == "image/jpeg"
    body = response.read()
    decoded = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded.shape == (600, 800, 3)
    connection.close()


def read_event(response: http.client.HTTPResponse) -> dict[str, object]:
    """Read one server-sent event, skipping keep-alive comments."""
    assert response.fp is not None
    while True:
        line = response.fp.readline()
        assert line, "event stream ended"
        if line.startswith(b"data: "):
            assert response.fp.readline() == b"\n"
            return json.loads(line[6:])  # type: ignore[no-any-return]
        assert line in (b": keep-alive\n", b"\n")


def test_events_carry_state_and_frame_version_together(server: PreviewServer) -> None:
    server.publish(blank_frame(value=10), {"frames": 1})
    connection = connect(server)
    connection.request("GET", "/events")
    response = connection.getresponse()
    assert response.status == 200
    assert response.getheader("Content-Type") == "text/event-stream"

    first = read_event(response)
    assert first == {"version": 1, "state": {"frames": 1}}
    server.publish(blank_frame(value=200), {"frames": 2})
    second = read_event(response)
    assert second == {"version": 2, "state": {"frames": 2}}

    frame = connect(server)
    frame.request("GET", "/frame.jpg?v=2")
    body = frame.getresponse().read()
    decoded = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded is not None
    assert decoded[0, 0, 0] > 150
    frame.close()
    connection.close()


def test_events_keep_an_idle_connection_alive(server: PreviewServer) -> None:
    connection = connect(server)
    connection.request("GET", "/events")
    response = connection.getresponse()
    assert response.fp is not None
    assert response.fp.readline() == b": keep-alive\n"  # after the 1 s wait with nothing published
    connection.close()


def test_events_end_when_the_server_closes() -> None:
    server = PreviewServer("127.0.0.1", 0).start()
    server.publish(blank_frame(), {})
    connection = connect(server)
    connection.request("GET", "/events")
    response = connection.getresponse()
    assert read_event(response)["version"] == 1

    closer = threading.Thread(target=server.close)
    closer.start()
    closer.join(timeout=5)
    assert not closer.is_alive()
    assert server.closed
    connection.close()


def test_events_handler_ends_quietly_when_the_client_goes_away(server: PreviewServer) -> None:
    server.publish(blank_frame(), {})
    connection = connect(server)
    connection.request("GET", "/events")
    response = connection.getresponse()
    assert read_event(response)["version"] == 1
    response.close()  # the socket stays open while the response still references it
    connection.close()

    # The handler is blocked in wait_for_update; the next frames make it write to the dead socket.
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and handler_threads():
        server.publish(blank_frame(value=int(time.monotonic() * 50) % 255), {})
        time.sleep(0.02)
    assert not handler_threads()


def handler_threads() -> list[threading.Thread]:
    """``ThreadingHTTPServer`` names its per-request threads ``Thread-N (process_request_thread)``."""
    return [t for t in threading.enumerate() if "process_request_thread" in t.name and t.is_alive()]


def test_wait_for_update_times_out_without_a_newer_frame(server: PreviewServer) -> None:
    assert server.wait_for_update(0, timeout=0.01) is None
    server.publish(blank_frame(), {"frames": 1})
    assert server.wait_for_update(0, timeout=0.01) == (1, {"frames": 1})
    assert server.wait_for_update(1, timeout=0.01) is None
    assert server.jpeg() is not None


def test_publish_still_advances_state_when_encoding_fails(server: PreviewServer) -> None:
    server.publish(np.zeros((0, 0, 3), dtype=np.uint8), {"frames": 2})
    assert server.state() == {"frames": 2}
    assert server.wait_for_update(0, timeout=0.01) == (1, {"frames": 2})
    assert server.jpeg() is None


def test_requests_are_logged_at_debug(server: PreviewServer, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="measure")
    connection = connect(server)
    connection.request("GET", "/state.json")
    connection.getresponse().read()
    connection.close()
    assert "preview:" in caplog.text
    assert "GET /state.json" in caplog.text
