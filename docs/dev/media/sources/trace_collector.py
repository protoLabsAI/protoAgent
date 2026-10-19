"""Local OTLP evidence collector, v1. Records actual SDK-exported spans only."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import gzip
import json
import threading
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

out = Path("/tmp/docs-trace-spans.json")
spans = []
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Content-Encoding") == "gzip":
            payload = gzip.decompress(payload)
        req = ExportTraceServiceRequest()
        req.ParseFromString(payload)
        rows = []
        for resource in req.resource_spans:
            service = next(
                (a.value.string_value for a in resource.resource.attributes if a.key == "service.name"), "unknown"
            )
            for scope in resource.scope_spans:
                for s in scope.spans:
                    rows.append(
                        {
                            "name": s.name,
                            "trace_id": s.trace_id.hex(),
                            "span_id": s.span_id.hex(),
                            "parent_span_id": s.parent_span_id.hex(),
                            "service": service,
                            "start": str(s.start_time_unix_nano),
                            "duration_ms": round((s.end_time_unix_nano - s.start_time_unix_nano) / 1e6, 1),
                            "status": s.status.code,
                        }
                    )
        with lock:
            spans.extend(rows)
            out.write_text(json.dumps(spans, indent=2) + "\n", encoding="utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/x-protobuf")
        self.end_headers()
        self.wfile.write(b"")

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"collector": "docs-local-otlp-v1", "spans": len(spans)}).encode())


ThreadingHTTPServer(("127.0.0.1", 4319), Handler).serve_forever()
