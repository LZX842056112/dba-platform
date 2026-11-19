import unittest
from datetime import datetime, timedelta

from dba.api.stream_tickets import issue_ticket_response


class FakeTicket:
    ticket = "st_test"


class FakeStore:
    def __init__(self):
        self.calls = []

    def issue(self, **kwargs):
        self.calls.append(kwargs)
        return FakeTicket()


class IssueTicketResponseTest(unittest.TestCase):
    def test_response_shape_and_arguments(self):
        store = FakeStore()
        since = datetime(2026, 1, 1)
        until = since + timedelta(days=1)

        result = issue_ticket_response(
            store,
            trace_id="trace-1",
            user_id=7,
            since=since,
            until=until,
        )

        self.assertEqual(result, {"stream_ticket": "st_test", "expires_in": 60})
        self.assertEqual(store.calls[0]["trace_id"], "trace-1")
        self.assertEqual(store.calls[0]["user_id"], 7)
        self.assertEqual(store.calls[0]["since"], since)
        self.assertEqual(store.calls[0]["until"], until)


if __name__ == "__main__":
    unittest.main()
