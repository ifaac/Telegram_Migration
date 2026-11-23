"""Regression tests for timestamps on preserved Telegram service events."""

import unittest
from datetime import datetime

from lxml import html

from telegram_migration import htmlexport, service_events
from telegram_migration.textutil import compose_message_header


class ServiceEventTimestampTests(unittest.TestCase):
    def event(self, date_markup=""):
        return html.fromstring(
            '<div class="message service" id="message670">'
            + date_markup
            + '<div class="body details">Alice joined the group</div>'
            '</div>'
        )

    def test_service_event_uses_its_own_full_timestamp(self):
        event = self.event(
            '<div class="pull_right date details" '
            'title="19.08.2026 21:46:54 GMT+02:00">21:46</div>'
        )
        data = htmlexport.extract_service_event_data(
            event, datetime(2026, 8, 19)
        )
        self.assertEqual(data["timestamp"], datetime(2026, 8, 19, 21, 46, 54))
        self.assertFalse(data["timestamp_date_only"])
        header, _ = compose_message_header(
            data["timestamp"], "Telegram service event",
            date_only=data["timestamp_date_only"],
        )
        self.assertIn("19 August 2026 · 21:46", header)

    def test_clock_only_service_event_uses_date_divider(self):
        event = self.event(
            '<div class="pull_right date details" title="21:46:54">21:46</div>'
        )
        data = htmlexport.extract_service_event_data(
            event, datetime(2026, 8, 19)
        )
        self.assertEqual(data["timestamp"], datetime(2026, 8, 19, 21, 46, 54))
        self.assertFalse(data["timestamp_date_only"])

    def test_date_title_and_visible_clock_are_combined(self):
        event = self.event(
            '<div class="pull_right date details" title="19.08.2026">21:46</div>'
        )
        data = htmlexport.extract_service_event_data(
            event, datetime(2026, 8, 19)
        )
        self.assertEqual(data["timestamp"], datetime(2026, 8, 19, 21, 46))
        self.assertFalse(data["timestamp_date_only"])

    def test_event_without_time_falls_back_to_date_only(self):
        event = self.event()
        data = htmlexport.extract_service_event_data(
            event, datetime(2026, 8, 19)
        )
        self.assertEqual(data["timestamp"], datetime(2026, 8, 19))
        self.assertTrue(data["timestamp_date_only"])
        header, _ = compose_message_header(
            data["timestamp"], "Telegram service event",
            date_only=data["timestamp_date_only"],
        )
        self.assertIn("19 August 2026", header)
        self.assertNotIn("·", header)

    def test_shared_service_event_parser_uses_the_same_timestamp(self):
        event = self.event(
            '<div class="pull_right date details" '
            'title="19.08.2026 21:46:54 GMT+02:00">21:46</div>'
        )
        data = service_events.extract_service_event_data(
            event, datetime(2026, 8, 19)
        )
        self.assertEqual(data["timestamp"], datetime(2026, 8, 19, 21, 46, 54))
        self.assertFalse(data["timestamp_date_only"])

    def test_pin_audit_timestamp_uses_the_general_service_event_parser(self):
        event = self.event(
            '<div class="pull_right date details" '
            'title="23.11.2026 12:06:16 UTC+02:00">12:06</div>'
        )
        self.assertEqual(
            service_events.extract_pin_event_timestamp(event),
            datetime(2026, 11, 23, 12, 6, 16),
        )


if __name__ == "__main__":
    unittest.main()
