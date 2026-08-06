from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/cgi-bin/gettoken":
            self._json(404, {"errcode": 404, "errmsg": "not found"})
            return
        query = parse_qs(parsed.query)
        if query.get("corpid") != ["test-corp"] or query.get("corpsecret") != [
            "test-secret"
        ]:
            self._json(200, {"errcode": 40013, "errmsg": "invalid corpid"})
            return
        self._json(
            200, {"errcode": 0, "access_token": "test-token", "expires_in": 7200}
        )

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path != "/cgi-bin/message/send" or query.get("access_token") != [
            "test-token"
        ]:
            self._json(404, {"errcode": 404, "errmsg": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        print(json.dumps({"wecom_message": payload}, ensure_ascii=False), flush=True)
        self._json(200, {"errcode": 0, "errmsg": "ok", "msgid": "mock-message-id"})

    def log_message(self, format: str, *args: object) -> None:
        return

    def _json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 18080), Handler).serve_forever()
