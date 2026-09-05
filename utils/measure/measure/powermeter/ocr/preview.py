"""A small embedded HTTP server showing what the OCR meter sees and reads, in a browser.

Standard library only. ``/`` is a one-page viewer; ``/events`` is a server-sent event
stream with one event per processed frame carrying the state and the frame's version;
``/frame.jpg`` serves the annotated frame (``?v=`` selects the version the page saw in
the event, so picture and numbers always belong to the same frame); ``/state.json`` is
the latest state for scripts.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import threading
from typing import Any

import cv2

from measure.powermeter.ocr.engine import Image
from measure.powermeter.ocr.reader import DisplayReading, rotate

_LOGGER = logging.getLogger("measure")

_GREEN = (80, 200, 80)
_RED = (60, 60, 230)
_YELLOW = (40, 200, 240)
_WHITE = (240, 240, 240)

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>OCR power meter</title>
<style>
body{font:14px system-ui,sans-serif;background:#111;color:#ddd;margin:0;display:flex;flex-wrap:wrap;
gap:16px;padding:16px}
img{max-width:100%;background:#000;border:1px solid #333}
table{border-collapse:collapse;min-width:22em}td,th{padding:4px 10px;border-bottom:1px solid #333;text-align:left}
th{color:#999;font-weight:normal}.ok{color:#7c7}.bad{color:#e66}.big{font-size:2.2em;font-weight:600}
code{color:#bbb}#c{padding:4px 10px}
</style></head><body>
<div><img id="s" alt="camera"><div id="c" class="bad">connecting</div></div>
<div><table id="t"></table></div>
<script>
const fmt=(d)=>(v)=>v==null?"-":v.toFixed(d);
const rows=[["power","Power (W)",fmt(2)],["voltage","Voltage (V)",fmt(1)],
["current","Current (A)",fmt(3)],["pf","Power factor",fmt(3)]];
const row=(k,v)=>`<tr><th>${k}</th><td>${v}</td></tr>`;
const img=document.getElementById("s"),conn=document.getElementById("c");
let lastEvent=0,version=-1;
function render(s){const last=s.last||{};let h="";
h+=row("Reported power",`<span class="big">${s.power==null?"-":s.power.toFixed(2)+" W"}</span>`);
h+=row("Last frame",`<span class="${last.accepted?"ok":"bad"}">${last.accepted?"accepted":"rejected"}</span>`
+(last.reason?`<br><code>${last.reason}</code>`:""));
for(const [k,label,f] of rows){h+=row(label,`${f(last[k])} <code>${(last.raw||{})[k]??""}</code>`)}
h+=row("Frame age",s.frame_age==null?"-":s.frame_age.toFixed(1)+" s");
h+=row("Camera",(s.fps==null?"-":s.fps.toFixed(1)+" fps")
+(s.source_error?` <span class="bad">${s.source_error}</span>`:""));
h+=row("Levelled by",s.angle==null?"not located":s.angle.toFixed(1)+" deg");
h+=row("Frames",`${s.frames} read, ${s.accepted} accepted, ${s.rejected} rejected, ${s.relocations} relocations`);
document.getElementById("t").innerHTML=h}
function connect(){const es=new EventSource("/events");
es.onopen=()=>{conn.textContent="connected";conn.className="ok"};
es.onmessage=(e)=>{const m=JSON.parse(e.data);lastEvent=Date.now();
if(m.version!==version){version=m.version;img.src=`/frame.jpg?v=${version}`}render(m.state)};
es.onerror=()=>{es.close();conn.textContent="disconnected from the measure tool, retrying";conn.className="bad";
setTimeout(connect,2000)}}
setInterval(()=>{if(lastEvent&&conn.className==="ok"){const age=(Date.now()-lastEvent)/1000;
conn.textContent=age>3?`connected, no frame processed for ${age.toFixed(0)} s`:"connected"}},1000);
connect();
</script></body></html>
"""


def annotate(frame: Image, reading: DisplayReading, fps: float) -> Image:
    """Draw the located regions and the parsed values onto the levelled frame."""
    location = reading.location
    image = rotate(frame, location.angle).copy() if location is not None else frame.copy()
    colour = _GREEN if reading.accepted else _RED
    if location is not None:
        for name, rect in location.regions.items():
            x1, y1, x2, y2 = rect
            cv2.rectangle(image, (x1, y1), (x2, y2), colour, 2)
            label = f"{name}: {reading.raw.get(name, '')}"
            cv2.putText(image, label, (x1, max(12, y1 - 6)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1, cv2.LINE_AA)
    values = ""
    if reading.voltage is not None and reading.current is not None and reading.pf is not None:
        values = f"V {reading.voltage:.1f}  I {reading.current:.3f}  PF {reading.pf:.3f}"
    status = f"{fps:.1f} fps  " + (f"angle {location.angle:.1f}" if location is not None else "not located")
    lines = [
        line
        for line in (
            f"{reading.power:.2f} W" if reading.power is not None else "no reading",
            values,
            reading.reason or ("accepted" if reading.accepted else ""),
            status,
        )
        if line
    ]
    text_colour = _WHITE if reading.accepted else _YELLOW
    y = image.shape[0] - 8 - 18 * (len(lines) - 1)
    for line in lines:
        cv2.putText(image, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(image, line, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, text_colour, 1, cv2.LINE_AA)
        y += 18
    return image


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], preview: PreviewServer) -> None:
        super().__init__(address, _Handler)
        self.preview = preview


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        _LOGGER.debug("preview: %s", format % args)

    def do_GET(self) -> None:
        preview = self.server.preview
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, "text/html; charset=utf-8", _PAGE.encode())
        elif path == "/state.json":
            self._send(200, "application/json", json.dumps(preview.state()).encode())
        elif path == "/frame.jpg":
            jpeg = preview.jpeg()
            if jpeg is None:
                self._send(503, "text/plain", b"no frame yet")
            else:
                self._send(200, "image/jpeg", jpeg)
        elif path == "/events":
            self._events(preview)
        else:
            self._send(404, "text/plain", b"not found")

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _events(self, preview: PreviewServer) -> None:
        """One server-sent event per published frame; a comment keeps an idle connection alive."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        version = 0  # a client connecting after frames were published gets the current one at once
        try:
            while not preview.closed:
                update = preview.wait_for_update(version, timeout=1.0)
                if update is None:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                version, state = update
                payload = json.dumps({"version": version, "state": state})
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
        except BrokenPipeError, ConnectionResetError, ConnectionAbortedError:
            return


class PreviewServer:
    """Serves the latest annotated frame and state; the meter pushes into it with ``publish``."""

    def __init__(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        self._condition = threading.Condition()
        self._jpeg: bytes | None = None
        self._state: dict[str, Any] = {}
        self._version = 0
        self._closed = threading.Event()
        self._server = _Server((host, port), self)
        self._thread = threading.Thread(target=self._server.serve_forever, name="ocr-preview", daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}/"

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def start(self) -> PreviewServer:
        self._thread.start()
        _LOGGER.info("OCR preview at %s", self.url)
        return self

    def close(self) -> None:
        self._closed.set()
        with self._condition:
            self._condition.notify_all()
        self._server.shutdown()
        self._server.server_close()

    def publish(self, image: Image, state: dict[str, Any]) -> None:
        """Publish a processed frame together with the state that belongs to it."""
        try:
            ok, encoded = cv2.imencode(".jpg", image, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        except cv2.error as error:
            _LOGGER.debug("preview: frame not encodable: %s", error)
            ok = False
        with self._condition:
            if ok:
                self._jpeg = encoded.tobytes()
            self._state = state
            self._version += 1
            self._condition.notify_all()

    def state(self) -> dict[str, Any]:
        with self._condition:
            return dict(self._state)

    def jpeg(self) -> bytes | None:
        with self._condition:
            return self._jpeg

    def wait_for_update(self, after_version: int, timeout: float) -> tuple[int, dict[str, Any]] | None:
        """Block until a frame newer than ``after_version`` is published, or return ``None`` on timeout."""
        with self._condition:
            if self._version <= after_version:
                self._condition.wait(timeout)
            if self._version <= after_version:
                return None
            return self._version, dict(self._state)
