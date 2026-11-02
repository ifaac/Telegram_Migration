"""End-to-end runs of the migration against a fake Telegram client (no network, no credentials)."""
import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from telegram_migration import bigfiles, config, mediatools, pipeline, progress
from telegram_migration.config import Settings

import fakes
from fakes import FakeTelegram


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        FakeTelegram.instances.clear()
        for patcher in (patch.object(pipeline, "TelegramClient", FakeTelegram),
                        patch.object(mediatools, "_enable_hachoir", return_value=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def settings(self, **changes):
        values = dict(api_id=1234, api_hash="hash", bot_token="token", channel_peer=-1001234567890,
                      base_dir=self.folder.name, dry_run=False, skip_confirmation=True,
                      send_delay_seconds=0, max_upload_mb=1)
        values.update(changes)
        return Settings(**values)

    def run_once(self, **changes):
        settings = self.settings(**changes)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            with config.use_settings(settings):
                    asyncio.run(pipeline.run_async(settings))
        return FakeTelegram.instances[-1], output.getvalue(), settings

    def path(self, name):
        return os.path.join(self.folder.name, name)

    # ------------------------------------------------------------------------------------
    def test_dry_run_sends_nothing(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("hello")), fakes.message(2, fakes.text("again"))])
        client, output, _ = self.run_once(dry_run=True)
        self.assertEqual([c for c in client.calls if c[0] in ("send_message", "send_file")], [])
        self.assertIn("DRY RUN", output)
        self.assertFalse(os.path.exists(self.path("migration_id_map.jsonl")))

    def test_text_messages_are_sent_with_time_and_sender_and_recorded(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("hello <b>there</b>"))])
        client, _, _ = self.run_once()
        self.assertEqual(client.texts(), ["[2023-03-12 10:01]\nAlice:\nhello there"])
        sent = client.kinds("send_message")[0][2]
        self.assertIsNone(sent["parse_mode"])                    # our text is never re-parsed as markdown
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"1": 1001})

    def test_second_run_resumes_and_does_not_repeat_messages(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("one")), fakes.message(2, fakes.text("two"))])
        self.run_once()
        client, _, _ = self.run_once()
        self.assertEqual(client.texts(), [])

    def test_forwarded_message_keeps_a_textual_header(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.forwarded("News Channel", "forwarded text"))])
        client, _, _ = self.run_once()
        self.assertIn("Forwarded from News Channel\n\nforwarded text", client.texts()[0])

    def test_reply_to_a_later_message_is_deferred_then_linked(self):
        fakes.write_export(self.folder.name, [
            fakes.message(1, fakes.text("first")),
            fakes.message(2, fakes.reply_to(3) + fakes.text("reply to the next one")),
            fakes.message(3, fakes.text("the target")),
        ])
        client, _, _ = self.run_once()
        order = client.texts()
        self.assertTrue(order[1].endswith("the target"), order)          # target went out before the reply
        reply_call = [c for c in client.kinds("send_message") if "reply to the next one" in c[1]][0]
        target_id = 1002                                                  # second message sent
        self.assertEqual(reply_call[2]["reply_to"], target_id)

    def test_reply_to_a_message_outside_the_export_gets_a_note(self):
        fakes.write_export(self.folder.name, [fakes.message(5, fakes.reply_to(999) + fakes.text("orphan reply"))])
        client, _, _ = self.run_once()
        self.assertIn("Original reply target #999 is unavailable", client.texts()[0])

    def test_oversize_file_stops_cleanly_and_the_next_run_skips_it(self):
        fakes.write_file(self.folder.name, "video_files/big.mp4", size=2 * 1024 * 1024)      # limit is 1 MiB
        fakes.write_export(self.folder.name, [
            fakes.message(1, fakes.text("before")),
            fakes.message(2, fakes.text("has a big video") + fakes.video("video_files/big.mp4")),
            fakes.message(3, fakes.text("after")),
            fakes.message(4, fakes.reply_to(2) + fakes.text("reply to the big one")),
        ])
        client, output, _ = self.run_once()
        # stopped cleanly: first message sent, nothing of the big message, nothing after it
        self.assertEqual(len(client.texts()), 1)
        self.assertEqual(client.kinds("send_file"), [])
        self.assertTrue(client.disconnected)
        self.assertIn("STOPPED: message id=2", output)
        self.assertIn(os.path.abspath(self.path("video_files/big.mp4")), output)
        # logged, and recorded for the next run automatically
        with open(self.path("failed_messages.txt"), encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith("2\tFILE_TOO_LARGE"))
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"1": 1001})
            self.assertEqual(bigfiles.load_manual_uploads()[0], {"2"})
        self.assertFalse(os.path.exists(self.path("migration_reply_audit.jsonl")))   # no audit pass after a stop
        # second run: message 2 is skipped (you uploaded it by hand), 3 and 4 are sent
        client, output, _ = self.run_once()
        self.assertEqual(client.kinds("send_file"), [])
        self.assertTrue(any("after" in t for t in client.texts()))
        self.assertTrue(any("reply to the big one" in t and "Original reply target #2 is unavailable" in t
                            for t in client.texts()))
        self.assertIn("SKIPPED by the script (ids: 2)", output)

    def test_manual_upload_with_destination_id_links_later_replies(self):
        fakes.write_file(self.folder.name, "video_files/big.mp4", size=2 * 1024 * 1024)
        fakes.write_export(self.folder.name, [
            fakes.message(2, fakes.text("big") + fakes.video("video_files/big.mp4")),
            fakes.message(4, fakes.reply_to(2) + fakes.text("reply to the big one")),
        ])
        with open(self.path("migration_manual_uploads.txt"), "w", encoding="utf-8") as handle:
            handle.write("2 5000   # uploaded by hand\n")
        client, _, _ = self.run_once()
        call = [c for c in client.kinds("send_message") if "reply to the big one" in c[1]][0]
        self.assertEqual(call[2]["reply_to"], 5000)

    def test_rejected_formatting_is_retried_as_plain_text(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("some <b>bold</b> words"))])
        original = FakeTelegram.__init__

        def init(this, *a, **k):
            original(this, *a, **k)
            this.reject_formatting = ValueError("Some of provided entities have invalid bounds")

        with patch.object(FakeTelegram, "__init__", init):
            client, output, _ = self.run_once()
        self.assertEqual(len(client.texts()), 1)
        self.assertIsNone(client.kinds("send_message")[0][2]["formatting_entities"])
        self.assertIn("sending it as plain text", output)
        with open(self.path("migration_formatting_dropped.jsonl"), encoding="utf-8") as handle:
            self.assertEqual(json.loads(handle.readline())["src"], "1")

    def test_a_media_message_that_was_already_uploaded_is_not_uploaded_again(self):
        fakes.write_file(self.folder.name, "photos/p.jpg")
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("caption") + fakes.photo("photos/p.jpg"))])
        with open(self.path("migration_partial_sends.jsonl"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"src": "1", "state": {"media_id": 777}}) + "\n")
        client, _, _ = self.run_once()
        self.assertEqual(client.kinds("send_file"), [])
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"1": 777})

    def test_batch_limit_stops_the_run(self):
        fakes.write_export(self.folder.name, [fakes.message(i, fakes.text(f"m{i}")) for i in range(1, 6)])
        client, output, _ = self.run_once(batch_limit=2)
        self.assertEqual(len(client.texts()), 2)
        self.assertIn("Batch limit reached", output)

    def test_missing_credentials_raise_a_config_error(self):
        with self.assertRaises(config.ConfigError):
            asyncio.run(pipeline.run_async(Settings(base_dir=self.folder.name)))


if __name__ == "__main__":
    unittest.main()
