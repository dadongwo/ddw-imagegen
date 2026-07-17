from __future__ import annotations

import argparse
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlparse


VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class State:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.lock = threading.Lock()
        self.next_id = 1

    def record(self, value: dict) -> None:
        with self.lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DDWMock/1.0"

        def log_message(self, format: str, *args: object) -> None:
            return

        def send_json(self, status: int, value: dict) -> None:
            body = json.dumps(value).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            with state.lock:
                job_id = f"mock-job-{state.next_id}"
                state.next_id += 1
            endpoint = parse_qs(parsed.query).get("endpoint", [""])[0]
            state.record(
                {
                    "method": "POST",
                    "path": parsed.path,
                    "endpoint": endpoint,
                    "bytes": length,
                    "job_id": job_id,
                }
            )
            self.send_json(202, {"id": job_id, "token": f"mock-token-{job_id}"})

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            job_id = parsed.path.rstrip("/").split("/")[-1]
            state.record({"method": "GET", "path": parsed.path, "job_id": job_id})
            self.send_json(
                200,
                {
                    "status": "succeeded",
                    "data": [
                        {
                            "b64_json": base64.b64encode(VALID_PNG).decode("ascii"),
                            "revised_prompt": "mock result",
                        }
                    ],
                },
            )

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--log", required=True)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), make_handler(State(Path(args.log))))
    print(f"http://{args.host}:{server.server_port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
