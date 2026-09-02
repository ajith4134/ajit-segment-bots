"""The session's behaviour is tested against a recorded transcript rather than
the live site: a test that needs the internet is a test that fails at 3am for a
reason that is not a defect. The recorded bodies are real, captured 2026-09-02."""

import pytest

from runtime.nse_public_data import NSE_HOME, NsePublicData

BAN_FILE = "Securities in Ban For Trade Date 02-SEP-2026:\n1,LICHSGFIN\n2,SAIL\n"
HOLIDAY_URL = "https://www.nseindia.com/api/holiday-master?type=trading"
BAN_URL = "https://nsearchives.nseindia.com/content/fo/fo_secban.csv"


class RecordedResponse:
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text

    def json(self):
        import json

        return json.loads(self.text)


class RecordedSession:
    """Stands in for curl_cffi's Session, recording what was asked in order."""

    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def get(self, url, timeout):
        self.asked.append(url)
        status, body = self.answers[url]
        return RecordedResponse(status, body)


def test_the_homepage_is_asked_before_the_first_api_call():
    """NSE refuses an /api/ call from a session with no homepage cookie
    (verified 2026-09-02). The warm-up is not politeness, it is the request
    working at all."""
    session = RecordedSession({
        NSE_HOME: (200, "<html></html>"),
        HOLIDAY_URL: (200, '{"FO": []}'),
    })
    nse = NsePublicData(session=session, timeout_seconds=20.0)
    nse.read_json(HOLIDAY_URL)
    assert session.asked[0] == NSE_HOME


def test_the_homepage_is_asked_once_not_before_every_call():
    session = RecordedSession({
        NSE_HOME: (200, "<html></html>"),
        HOLIDAY_URL: (200, '{"FO": []}'),
    })
    nse = NsePublicData(session=session, timeout_seconds=20.0)
    nse.read_json(HOLIDAY_URL)
    nse.read_json(HOLIDAY_URL)
    assert session.asked.count(NSE_HOME) == 1


def test_a_refused_request_raises_rather_than_returning_an_error_page():
    """A 403 body parsed as a ban list is an empty ban list, which reads as
    'nothing is banned today' -- the most dangerous possible wrong answer."""
    session = RecordedSession({NSE_HOME: (200, "<html></html>"), BAN_URL: (403, "Error 1010")})
    nse = NsePublicData(session=session, timeout_seconds=20.0)
    with pytest.raises(RuntimeError) as refused:
        nse.read_text(BAN_URL)
    assert "403" in str(refused.value)


def test_a_refused_json_request_raises_too():
    session = RecordedSession({NSE_HOME: (200, "<html></html>"), HOLIDAY_URL: (401, "denied")})
    nse = NsePublicData(session=session, timeout_seconds=20.0)
    with pytest.raises(RuntimeError):
        nse.read_json(HOLIDAY_URL)


def test_a_body_that_arrived_is_returned_unchanged():
    session = RecordedSession({NSE_HOME: (200, "<html></html>"), BAN_URL: (200, BAN_FILE)})
    nse = NsePublicData(session=session, timeout_seconds=20.0)
    assert nse.read_text(BAN_URL) == BAN_FILE


def test_the_standing_counts_what_was_asked_and_what_was_refused():
    """A part's own standing is what the board reads; a refusal that left no
    trace would be a fetch failure nobody could see."""
    session = RecordedSession({
        NSE_HOME: (200, "<html></html>"),
        BAN_URL: (200, BAN_FILE),
        HOLIDAY_URL: (403, "Error 1010"),
    })
    nse = NsePublicData(session=session, timeout_seconds=20.0)
    nse.read_text(BAN_URL)
    with pytest.raises(RuntimeError):
        nse.read_json(HOLIDAY_URL)
    assert nse.standing() == {"is_warm": True, "requests_made": 2, "requests_refused": 1}
