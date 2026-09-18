import pytest
import responses

from fairscape_publish.transport import (
    DryRunSession,
    Session,
    TransportError,
    redact_headers,
    redact_url,
)


class TestRedaction:
    def test_auth_headers_are_masked(self):
        masked = redact_headers({"Authorization": "Bearer hunter2", "Accept": "application/json"})
        assert masked["Authorization"] == "<redacted>"
        assert masked["Accept"] == "application/json"

    def test_dataverse_key_is_masked(self):
        assert redact_headers({"X-Dataverse-key": "s3cret"})["X-Dataverse-key"] == "<redacted>"

    def test_access_token_query_params_are_masked(self):
        assert redact_url("https://api.example.org/x?access_token=abc&page=2") == (
            "https://api.example.org/x?access_token=<redacted>&page=2"
        )


class TestRetries:
    @responses.activate
    def test_a_429_is_retried(self):
        responses.add(responses.GET, "https://api.example.org/x", status=429)
        responses.add(responses.GET, "https://api.example.org/x", json={"ok": True}, status=200)
        session = Session(max_retries=2)
        session._backoff = lambda *args: None
        assert session.get("https://api.example.org/x").json() == {"ok": True}

    @responses.activate
    def test_a_4xx_is_not_retried(self):
        responses.add(responses.GET, "https://api.example.org/x", json={"nope": True}, status=422)
        session = Session(max_retries=3)
        with pytest.raises(TransportError) as exc:
            session.get("https://api.example.org/x")
        assert exc.value.status_code == 422
        assert len(responses.calls) == 1

    @responses.activate
    def test_retries_are_bounded(self):
        for _ in range(5):
            responses.add(responses.GET, "https://api.example.org/x", status=503)
        session = Session(max_retries=2)
        session._backoff = lambda *args: None
        with pytest.raises(TransportError):
            session.get("https://api.example.org/x")
        assert len(responses.calls) == 3  # the original plus two retries

    @responses.activate
    def test_a_file_handle_is_rewound_before_a_retry(self, tmp_path):
        path = tmp_path / "payload.bin"
        path.write_bytes(b"0123456789")
        responses.add(responses.PUT, "https://api.example.org/f", status=500)
        responses.add(responses.PUT, "https://api.example.org/f", json={}, status=200)

        session = Session(max_retries=1)
        session._backoff = lambda *args: None
        with path.open("rb") as handle:
            session.put("https://api.example.org/f", data=handle)

        # Without the rewind the second attempt would send an empty body.
        assert _body(responses.calls[1].request) == b"0123456789"


class TestDryRunSession:
    def test_it_opens_no_connection_and_records_the_call(self):
        session = DryRunSession()
        session.post("https://api.example.org/x", json_body={"a": 1})
        assert session.dry_run is True
        assert session.calls[0].method == "POST"
        assert session.calls[0].json_body == {"a": 1}

    def test_responders_are_matched_in_registration_order(self):
        session = DryRunSession()
        session.add_responder(lambda m, u: u.endswith("/specific"), {"which": "specific"})
        session.add_responder(lambda m, u: True, {"which": "catch-all"})
        assert session.post("https://x/specific").json() == {"which": "specific"}
        assert session.post("https://x/other").json() == {"which": "catch-all"}

    def test_an_unmatched_call_returns_an_empty_body(self):
        assert DryRunSession().get("https://x/nothing").json() == {}

    def test_the_transcript_describes_the_request(self):
        session = DryRunSession(headers={"Authorization": "Bearer hunter2"})
        session.post("https://api.example.org/x", json_body={"title": "T"}, params={"id": 5})
        described = session.calls[0].describe()
        assert "POST https://api.example.org/x" in described
        assert "params: id=5" in described
        assert "hunter2" not in described
        assert '"title": "T"' in described

    def test_a_multipart_form_field_is_shown_not_treated_as_a_file(self):
        session = DryRunSession()
        session.post(
            "https://api.example.org/add",
            files={"file": ("x.csv", b"data", "text/csv"), "jsonData": (None, '{"k": 1}', "application/json")},
        )
        described = session.calls[0].describe()
        assert "file=<file x.csv>" in described
        assert 'jsonData={"k": 1}' in described


def _body(request):
    """responses hands back either bytes or the stream it was given."""
    body = request.body
    return body.read() if hasattr(body, "read") else body
