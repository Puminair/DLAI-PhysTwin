"""Meraki Scanning API v3 receiver — webhooks in, Layer-3 records out.

Layer: none — integration tooling on the Layer-2/Layer-3 boundary.
May import: stdlib + dlai.ingest ONLY. NEVER world/ or sensing/ — this
process stands where the real Meraki cloud would, so it must know
nothing but the webhook contract and the Layer-3 ingest entry point.

The Meraki handshake contract implemented here:
  GET  <endpoint> -> 200 text/plain, body = the configured validator
                     (Meraki verifies ownership of the URL this way)
  POST <endpoint> -> JSON {"secret": ..., "data": {...}}
                     wrong/missing secret -> 403, invalid JSON -> 400
                     accepted -> records normalised via
                     dlai.ingest.normalise_batch and appended as JSONL

Sign convention: DevicesSeen RSSI arrives POSITIVE; normalise_batch
flips it to dBm (negative) and records the flip in each record's
`normalisation` list — an inference never claims to be an observation.

Request handling is factored into pure functions (parse / validate /
normalise) so tests never open a socket; only __main__ wires http.server.
The shared secret comes from env MERAKI_SCANNING_SECRET or an
interactive prompt — never argv, never a file.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import sys
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

if __package__ in (None, ""):    # direct script run: repo root onto sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dlai.ingest import normalise_batch


# ------------------------------------------------------- pure functions ----

def handshake_body(validator: str) -> bytes:
    """GET response body: Meraki expects the bare validator string."""
    return validator.encode("utf-8")


def secret_ok(body: dict, expected_secret: str) -> bool:
    """Constant-time comparison of the POSTed shared secret."""
    return hmac.compare_digest(str(body.get("secret", "")), expected_secret)


def adapt_meraki_body(body: dict) -> dict:
    """Adapt an incoming Meraki POST to the batch shape ingest expects.

    In: {"secret": ..., "version"?: ..., "type"?: ..., "data": {...}}
    Out: {"deliveredAt": ms, "records": [...]} — ObservationBatch.to_json()
    shape, i.e. exactly what dlai.ingest.normalise_batch consumes.

    Two `data` shapes are accepted:
      - the twin pipeline's batch: {"deliveredAt": ms, "records": [...]}
        (passed through);
      - a Meraki-style {"observations": [...]}: each observation becomes
        a record, defaulting `stream` to scanning_api_v3/DevicesSeen.
    deliveredAt falls back to the envelope's `sentAt`, then 0 (unknown).
    """
    data = body.get("data") or {}
    delivered = int(data.get("deliveredAt") or body.get("sentAt") or 0)
    if "records" in data:
        records = list(data["records"])
    elif "observations" in data:
        records = []
        for obs in data["observations"]:
            rec = dict(obs)
            rec.setdefault("stream", "scanning_api_v3/DevicesSeen")
            records.append(rec)
    else:
        records = []
    return {"deliveredAt": delivered, "records": records}


def handle_post(raw: bytes, expected_secret: str) -> tuple[int, Any]:
    """parse -> validate secret -> adapt -> normalise. No I/O, no socket.

    Returns (http_status, payload): 200 with the normalised records as
    dicts, or 4xx with {"error": ...}.
    """
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, {"error": "invalid JSON"}
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object"}
    if not secret_ok(body, expected_secret):
        return 403, {"error": "secret mismatch"}
    batch = adapt_meraki_body(body)
    try:
        normalised = normalise_batch(batch)
    except (KeyError, TypeError, ValueError) as exc:
        return 400, {"error": f"malformed batch: {exc!r}"}
    return 200, [asdict(o) for o in normalised]


def append_jsonl(records: list[dict], sink: Path) -> None:
    """Append normalised records to the JSONL sink, one per line."""
    sink.parent.mkdir(parents=True, exist_ok=True)
    with open(sink, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


# ----------------------------------------------------------- the server ----

class ScanningHandler(BaseHTTPRequestHandler):
    """Thin http.server shim over the pure functions above."""

    # configured by main(); class attributes so the stdlib factory works
    validator: str = ""
    secret: str = ""
    sink: Path = Path("scanning_ingest.jsonl")

    def do_GET(self) -> None:            # Meraki ownership handshake
        payload = handshake_body(self.validator)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        status, payload = handle_post(raw, self.secret)
        if status == 200:
            append_jsonl(payload, self.sink)
            out = {"accepted": len(payload), "sink": str(self.sink)}
        else:
            out = payload
        body = json.dumps(out).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[scanning_receiver] {self.address_string()} {fmt % args}",
              file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Meraki Scanning API v3 receiver -> dlai ingest JSONL. "
                    "Shared secret from env MERAKI_SCANNING_SECRET or an "
                    "interactive prompt — never argv.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--validator", required=True,
                    help="string echoed on GET (Meraki handshake; public)")
    ap.add_argument("--sink", type=Path,
                    default=Path("data") / "scanning_ingest.jsonl",
                    help="JSONL output of normalised records")
    args = ap.parse_args(argv)

    secret = os.environ.get("MERAKI_SCANNING_SECRET")
    if not secret:
        import getpass
        secret = getpass.getpass("scanning shared secret: ")

    ScanningHandler.validator = args.validator
    ScanningHandler.secret = secret
    ScanningHandler.sink = args.sink
    server = HTTPServer((args.host, args.port), ScanningHandler)
    print(f"[scanning_receiver] listening on http://{args.host}:{args.port} "
          f"-> {args.sink}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
