"""Private preparation, explicit destination guards and real loopback transport."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from watt_profile.cli.server_demo import DemoError, _origin, ingest, main, prepare
from watt_profile.contracts.upload import UploadBatch


def test_prepare_private_fresh_and_saved(tmp_path, capsys):
    first, second = tmp_path / "first", tmp_path / "second"
    assert main(["prepare", "--output", str(first)]) == 0
    prepare(second)
    assert first.stat().st_mode & 0o777 == 0o700
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in first.iterdir())
    assert first.joinpath("token").read_text() != second.joinpath("token").read_text()
    batches = [
        UploadBatch.model_validate_json(p.read_text()) for p in sorted(first.glob("batch-*"))
    ]
    assert [b.site_id for b in batches] == [
        batches[0].site_id,
        batches[1].site_id,
        batches[0].site_id,
    ]
    assert len({b.session.session_id for b in batches}) == 3
    assert len({b.device_id for b in batches}) == 1
    assert len({r.record_id for b in batches for r in b.records}) == 6
    assert batches[0].records[0].model_dump(mode="json")["metrics"]["power_export_kw"] == "0.000"
    before = {p.name: p.read_bytes() for p in first.iterdir()}
    assert main(["prepare", "--output", str(first)]) == 1
    assert before == {p.name: p.read_bytes() for p in first.iterdir()}
    assert first.joinpath("token").read_text().strip() not in str(capsys.readouterr())


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "http://127.0.0.1.evil",
        "http://user:pass@localhost",
        "http://localhost/",
        "http://localhost?x=1",
        "http://localhost#x",
        "http://server:8080",
        "http://127.0.0.1\\@evil",
        "http://localhost\n",
        "http://192.168.0.1",
    ],
)
def test_origin_refusal(origin):
    with pytest.raises(DemoError):
        _origin(origin, False)


def test_development_network_is_exact():
    assert _origin("http://server:8080", True).hostname == "server"
    for value in ("http://server:8081", "https://server:8080", "http://else:8080"):
        with pytest.raises(DemoError):
            _origin(value, True)


@pytest.mark.parametrize("mode", ["success", "redirect", "missing", "reordered"])
def test_real_http_replay_and_confirmation(tmp_path, monkeypatch, mode):
    directory = tmp_path / "demo"
    prepare(directory)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(payload)
            self.send_response(302 if mode == "redirect" else 200)
            self.send_header("Location", "http://example.invalid/leak")
            self.end_headers()
            ids = [r["record_id"] for r in payload["records"]]
            if mode == "missing":
                ids.pop()
            elif mode == "reordered":
                ids.reverse()
            self.wfile.write(
                json.dumps(
                    {
                        "schema_version": 1,
                        "device_id": payload["device_id"],
                        "confirmed_record_ids": ids,
                    }
                ).encode()
            )

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("HTTP_PROXY", "http://unreachable.invalid:9")
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        if mode == "success":
            assert ingest(directory, origin, site="A") == 4
            assert ingest(directory, origin, site="A") == 4
            assert requests[:2] == requests[2:]
        else:
            with pytest.raises(ValueError):
                ingest(directory, origin)
            assert len(requests) == 1
        (directory / "token").chmod(0o644)
        with pytest.raises(DemoError):
            ingest(directory, origin)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_symlink_and_public_input_refusal(tmp_path):
    directory = tmp_path / "demo"
    prepare(directory)
    token = directory / "token"
    token.rename(directory / "original")
    token.symlink_to(directory / "original")
    assert (
        main(["ingest", "--directory", str(directory), "--server-origin", "http://localhost:9"])
        == 1
    )
    directory.chmod(0o755)
    with pytest.raises(DemoError):
        ingest(directory, "http://localhost:9")
