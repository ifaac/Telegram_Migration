"""End-to-end runs of the migration against a fake Telegram client (no network, no credentials)."""
import asyncio
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from telethon.tl import types as tl

from telegram_migration import album, bigfiles, config, customemoji, htmlexport, mediatools, pipeline, progress, sending
from lxml import html
from telegram_migration.config import Settings

import fakes
from fakes import FakeTelegram


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        FakeTelegram.instances.clear()
        for patcher in (patch.object(pipeline, "TelegramClient", FakeTelegram),
                        patch.object(mediatools, "enable_hachoir", return_value=True)):
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
        self.assertEqual(client.texts(), ["Alice\n12 March 2023 · 10:01\n\nhello there"])
        sent = client.kinds("send_message")[0][2]
        self.assertIsNone(sent["parse_mode"])
        entities = sent["formatting_entities"]
        self.assertEqual(
            [(type(entity).__name__, entity.offset, entity.length) for entity in entities[:2]],
            [("MessageEntityBold", 0, 5), ("MessageEntityItalic", 6, 21)],
        )
        self.assertEqual(
            (type(entities[2]).__name__, entities[2].offset, entities[2].length),
            ("MessageEntityBold", 35, 5),
        )
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"1": 1001})

    def test_missing_media_only_message_is_sent_as_placeholder_not_skipped(self):
        fakes.write_export(self.folder.name, [
            fakes.message(1, '<div class="media_wrap clearfix">'
                             '<a class="photo_wrap" href="photos/gone.jpg">'
                             '<img class="photo" src="photos/gone_thumb.jpg"/>'
                             '</a></div>'),
        ])
        client, output, _ = self.run_once()
        self.assertEqual(
            client.texts(),
            ["Alice\n12 March 2023 · 10:01\n\n[Media not found: photos/gone.jpg]"],
        )
        self.assertIn("sent=1", output)
        self.assertIn("skipped=0", output)
        self.assertEqual(client.kinds("send_file"), [])
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"1": 1001})

    def test_empty_export_message_is_preserved_without_inventing_forward_attribution(self):
        # This matches the blank message shell in the user's Telegram Desktop
        # export: the sender and export timestamp survive, but there is no
        # forwarded body, original author, original timestamp, text, or media.
        empty_message = (
            '<div class="message default clearfix" id="message81">'
            '<div class="pull_left userpic_wrap">'
            '<div class="userpic userpic8"><div class="initials">⛯</div></div>'
            '</div>'
            '<div class="body">'
            '<div class="pull_right date details" '
            'title="01.07.2026 21:33:19 UTC+02:00">21:33</div>'
            '<div class="from_name">⛯⛯</div>'
            '</div>'
            '</div>'
        )
        fakes.write_export(self.folder.name, [empty_message])
        client, output, _ = self.run_once()

        self.assertEqual(
            client.texts(),
            [
                "⛯⛯\n1 July 2026 · 21:33\n\n"
                "[Empty message in source export; no text or media were included]"
            ],
        )
        self.assertNotIn("Forwarded from", client.texts()[0])
        self.assertIn("sent=1", output)
        self.assertIn("skipped=0", output)
        self.assertIn("failed=0", output)
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"81": 1001})

    def test_service_events_are_migrated_as_dated_archival_entries(self):
        fakes.write_export(self.folder.name, [
            '<div class="message service" id="message-1">'
            '<div class="body details">12 March 2023</div></div>',
            '<div class="message service" id="message2">'
            '<div class="body details">Alice invited Bob</div></div>',
            fakes.message(3, fakes.text("hello")),
        ])
        client, _, _ = self.run_once()
        self.assertEqual(client.texts(), [
            "Telegram service event\n12 March 2023\n\nAlice invited Bob",
            "Alice\n12 March 2023 · 10:03\n\nhello",
        ])
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"2": 1001, "3": 1002})

    def test_service_events_can_be_disabled(self):
        fakes.write_export(self.folder.name, [
            '<div class="message service" id="message-1">'
            '<div class="body details">12 March 2023</div></div>',
            '<div class="message service" id="message2">'
            '<div class="body details">Alice invited Bob</div></div>',
        ])
        client, _, _ = self.run_once(preserve_service_messages=False)
        self.assertEqual(client.texts(), [])

    def test_concurrent_runs_keep_settings_and_exports_isolated(self):
        with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
            fakes.write_export(first_dir, [fakes.message(1, fakes.text("first run"))])
            fakes.write_export(second_dir, [fakes.message(2, fakes.text("second run"))])
            first = self.settings(base_dir=first_dir)
            second = self.settings(base_dir=second_dir)

            async def scenario():
                await asyncio.gather(
                    pipeline.run_async(first),
                    pipeline.run_async(second),
                )

            with contextlib.redirect_stdout(io.StringIO()):
                asyncio.run(scenario())

        clients = FakeTelegram.instances[-2:]
        self.assertCountEqual([client.texts() for client in clients], [
            ["Alice\n12 March 2023 · 10:01\n\nfirst run"],
            ["Alice\n12 March 2023 · 10:02\n\nsecond run"],
        ])

    def test_configured_text_limit_splits_reconstructed_messages(self):
        fakes.write_export(self.folder.name, [
            fakes.message(1, fakes.text("x" * 100)),
        ])
        client, _, _ = self.run_once(text_limit=40)
        parts = client.texts()
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(part) <= 40 for part in parts))

    def test_second_run_resumes_and_does_not_repeat_messages(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("one")), fakes.message(2, fakes.text("two"))])
        self.run_once()
        client, _, _ = self.run_once()
        self.assertEqual(client.texts(), [])

    def test_joined_forward_continuations_inherit_the_original_forwarded_sender(self):
        messages = [
            (
                '<div class="message default clearfix" id="message247">'
                '<div class="body">'
                '<div class="pull_right date details" title="01.07.2026 21:42:46 UTC+02:00">21:42</div>'
                '<div class="from_name">⛯⛯</div>'
                '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
                '<div class="forwarded body">'
                '<div class="from_name">Valerie Demian'
                '<span class="date details" title="10.05.2026 17:00:58 UTC+02:00">'
                '10.05.2026 17:00:58</span></div>'
                '<div class="text">Может написать тому челу спросить за ту ученицу?</div>'
                '</div></div></div>'
            ),
        ]
        for message_id, clock, payload in [
            (248, "21:42:47", "Хотя мне так лень"),
            (249, "21:42:47", "Портить день еще себе ток"),
            (250, "21:42:48", "Ненавижу таких мужчин , что бывших преследуют"),
        ]:
            messages.append(
                f'<div class="message default clearfix joined" id="message{message_id}">'
                '<div class="body">'
                f'<div class="pull_right date details" title="01.07.2026 {clock} UTC+02:00">{clock}</div>'
                '<div class="forwarded body">'
                f'<div class="text">{payload}</div>'
                '</div></div></div>'
            )
        # A later forward starts a different attribution group. Its joined
        # continuation must inherit the new author, never Valerie's.
        messages.extend([
            (
                '<div class="message default clearfix" id="message251">'
                '<div class="body">'
                '<div class="pull_right date details" title="01.07.2026 21:42:49 UTC+02:00">21:42</div>'
                '<div class="from_name">⛯⛯</div>'
                '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
                '<div class="forwarded body">'
                '<div class="from_name">🍀 🌼'
                '<span class="date details" title="10.05.2026 17:02:00 UTC+02:00">'
                '10.05.2026 17:02:00</span></div>'
                '<div class="text">A separate forwarded message</div>'
                '</div></div></div>'
            ),
            (
                '<div class="message default clearfix joined" id="message252">'
                '<div class="body">'
                '<div class="pull_right date details" title="01.07.2026 21:42:50 UTC+02:00">21:42</div>'
                '<div class="forwarded body"><div class="text">Its continuation</div></div>'
                '</div></div>'
            ),
        ])
        fakes.write_export(self.folder.name, messages)
        client, _, _ = self.run_once()

        sent = client.kinds("send_message")
        self.assertEqual(len(sent), 6)
        for call in sent[:4]:
            self.assertIn("Forwarded from Valerie Demian", call[1])
        self.assertTrue(sent[1][1].endswith("Хотя мне так лень"))
        self.assertTrue(sent[2][1].endswith("Портить день еще себе ток"))
        self.assertTrue(sent[3][1].endswith("Ненавижу таких мужчин , что бывших преследуют"))
        for call in sent[4:]:
            self.assertIn("Forwarded from 🍀 🌼", call[1])
            self.assertNotIn("Forwarded from Valerie Demian", call[1])

    def test_reconstructed_text_forward_keeps_attribution_and_payload_in_one_message(self):
        forwarded = (
            '<div class="message default clearfix" id="message136">'
            '<div class="body">'
            '<div class="pull_right date details" '
            'title="08.11.2026 07:09:41 UTC+02:00">07:09</div>'
            '<div class="from_name">jbvjhvjhvjh</div>'
            '<div class="pull_left forwarded userpic_wrap">'
            '<div class="userpic userpic7"><div class="initials">H</div></div>'
            '</div>'
            '<div class="forwarded body">'
            '<div class="from_name">HLI8IOS '
            '<span class="date details" '
            'title="08.11.2026 07:09:09 UTC+02:00">'
            '08.11.2026 07:09:09</span></div>'
            '<div class="text">hello</div>'
            '</div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])
        client, _, _ = self.run_once()

        calls = client.kinds("send_message")
        self.assertEqual(len(calls), 1)
        sent = calls[0]
        self.assertEqual(
            sent[1],
            "jbvjhvjhvjh\n8 November 2026 · 07:09\n\n"
            "Forwarded from HLI8IOS\n8 November 2026 · 07:09:09\n\nhello",
        )
        self.assertIsNone(sent[2]["reply_to"])

        entities = sent[2]["formatting_entities"]
        quote = next(e for e in entities if type(e).__name__ == "MessageEntityBlockquote")
        quote_text = sent[1][quote.offset:quote.offset + quote.length]
        self.assertEqual(quote_text, "Forwarded from HLI8IOS\n8 November 2026 · 07:09:09")
        self.assertLess(quote.offset + quote.length, len(sent[1]))
        source_bold = next(e for e in entities
                           if type(e).__name__ == "MessageEntityBold"
                           and sent[1][e.offset:e.offset + e.length] == "HLI8IOS")
        self.assertEqual(sent[1][source_bold.offset:source_bold.offset + source_bold.length],
                         "HLI8IOS")
        source_date_italic = next(e for e in entities
                                  if type(e).__name__ == "MessageEntityItalic"
                                  and "07:09:09" in sent[1][e.offset:e.offset + e.length])
        self.assertIn("8 November 2026 · 07:09:09",
                      sent[1][source_date_italic.offset:
                              source_date_italic.offset + source_date_italic.length])
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"136": 1001})

    def test_joined_forwarded_sticker_inherits_original_author_and_timestamp(self):
        relative = "stickers/joined-forwarded.webp"
        fakes.write_file(self.folder.name, relative)
        first = (
            '<div class="message default clearfix" id="message701">'
            '<div class="body">'
            '<div class="pull_right date details" '
            'title="08.11.2026 09:28:59 UTC+02:00">09:28</div>'
            '<div class="from_name">Archive sender</div>'
            '<div class="forwarded body">'
            '<div class="from_name">Original sender '
            '<span class="date details" '
            'title="05.11.2026 19:35:11 UTC+02:00">05.11.2026 19:35:11</span>'
            '</div>'
            '<div class="text">preceding forwarded text</div>'
            '</div></div></div>'
        )
        joined_sticker = (
            '<div class="message default clearfix joined" id="message702">'
            '<div class="body">'
            '<div class="pull_right date details" '
            'title="08.11.2026 09:29:00 UTC+02:00">09:29</div>'
            '<div class="forwarded body">'
            '<div class="media_wrap clearfix">'
            f'<a class="sticker_wrap clearfix pull_left" href="{relative}"></a>'
            '</div></div></div></div>'
        )
        fakes.write_export(self.folder.name, [first, joined_sticker])
        client, _, _ = self.run_once()

        self.assertEqual(len(client.kinds("send_message")), 1)
        media_calls = client.kinds("send_file")
        self.assertEqual(len(media_calls), 1)
        caption = media_calls[0][2]["caption"]
        self.assertIn("Archive sender", caption)
        self.assertIn("8 November 2026 · 09:29", caption)
        self.assertIn("Forwarded from Original sender", caption)
        self.assertIn("5 November 2026 · 19:35:11", caption)
        self.assertIsNone(media_calls[0][2]["reply_to"])
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"701": 1001, "702": 1002})

    def test_reconstructed_forward_keeps_body_formatting_below_attribution_bar(self):
        forwarded = (
            '<div class="message default clearfix" id="message138">'
            '<div class="body">'
            '<div class="pull_right date details" title="08.11.2026 07:09:41 UTC+02:00">07:09</div>'
            '<div class="from_name">jbvjhvjhvjh</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name">HLI8IOS</div>'
            '<div class="text"><strong>hello</strong> <em>world</em></div>'
            '</div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])
        client, _, _ = self.run_once()
        calls = client.kinds("send_message")
        self.assertEqual(len(calls), 1)
        sent = calls[0]
        self.assertIn("Forwarded from HLI8IOS", sent[1])
        self.assertTrue(sent[1].endswith("hello world"))
        entities = sent[2]["formatting_entities"]
        names = [type(entity).__name__ for entity in entities]
        self.assertIn("MessageEntityBold", names)
        self.assertIn("MessageEntityItalic", names)
        quote = next(e for e in entities if type(e).__name__ == "MessageEntityBlockquote")
        quote_text = sent[1][quote.offset:quote.offset + quote.length]
        self.assertEqual(quote_text, "Forwarded from HLI8IOS")
        self.assertNotIn("hello", quote_text)
        bold = next(e for e in entities if type(e).__name__ == "MessageEntityBold"
                    and sent[1][e.offset:e.offset + e.length] == "hello")
        italic = next(e for e in entities if type(e).__name__ == "MessageEntityItalic"
                      and sent[1][e.offset:e.offset + e.length] == "world")
        self.assertEqual(sent[1][bold.offset:bold.offset + bold.length], "hello")
        self.assertEqual(sent[1][italic.offset:italic.offset + italic.length], "world")

    def test_forwarded_photo_album_with_repeated_reply_target_is_grouped(self):
        first_path = "photos/first.jpg"
        second_path = "photos/second.jpg"
        fakes.write_file(self.folder.name, first_path)
        fakes.write_file(self.folder.name, second_path)

        def forwarded_photo(msg_id, clock, path, caption="", joined=False):
            joined_class = " joined" if joined else ""
            caption_html = f'<div class="text">{caption}</div>' if caption else ""
            return (
                f'<div class="message default clearfix{joined_class}" id="message{msg_id}">'
                '<div class="body">'
                f'<div class="pull_right date details" title="01.07.2026 21:46:{clock} UTC+02:00">21:46</div>'
                '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
                '<div class="forwarded body">'
                '<div class="from_name">espritzeit '
                '<span class="date details" title="04.06.2026 18:10:26 UTC+02:00">'
                '04.06.2026 18:10:26</span></div>'
                '<div class="reply_to details"><a href="#go_to_message330">this message</a></div>'
                '<div class="media_wrap clearfix">'
                f'<a class="photo_wrap clearfix pull_left" href="{path}">'
                f'<img class="photo" src="{path}_thumb.jpg"/>'
                '</a></div>'
                f'{caption_html}'
                '</div></div></div>'
            )

        first = forwarded_photo(
            901, "56", first_path,
            "<blockquote>The subject's private channel, where the aforementioned "
            "Instagram sessions are advertised, is also featured prominently on "
            "the phishing website.</blockquote>",
        )
        second = forwarded_photo(902, "57", second_path, joined=True)
        fakes.write_export(self.folder.name, [first, second])

        client, _, _ = self.run_once()
        self.assertEqual(client.kinds("send_message"), [])
        album_calls = client.kinds("send_file")
        self.assertEqual(len(album_calls), 1)
        files, kwargs = album_calls[0][1], album_calls[0][2]
        self.assertEqual(files, [
            os.path.join(self.folder.name, first_path),
            os.path.join(self.folder.name, second_path),
        ])
        self.assertEqual(kwargs["caption"][0], "")
        self.assertIn("The subject's private channel", kwargs["caption"][1])
        self.assertIn("Forwarded from espritzeit", kwargs["caption"][1])
        self.assertNotIn("this message", kwargs["caption"][1])
        self.assertIsNone(kwargs["reply_to"])
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"901": 1001, "902": 1002})

    def test_reconstructed_forward_media_keeps_attribution_in_same_media_caption(self):
        media_path = "video_files/a6579a95-f3fd-497d-9b88-46592c1a878e_wm.mp4"
        fakes.write_file(self.folder.name, media_path)
        forwarded = (
            '<div class="message default clearfix" id="message151">'
            '<div class="body">'
            '<div class="pull_right date details" '
            'title="08.11.2026 09:28:59 UTC+02:00">09:28</div>'
            '<div class="from_name">jbvjhvjhvjh</div>'
            '<div class="pull_left forwarded userpic_wrap">'
            '<div class="userpic userpic7"><div class="initials">H</div></div>'
            '</div>'
            '<div class="forwarded body">'
            '<div class="from_name">HLI8IOS '
            '<span class="date details" title="05.11.2026 19:35:11 UTC+02:00">'
            '05.11.2026 19:35:11</span></div>'
            '<div class="media_wrap clearfix">'
            f'<a class="animated_wrap clearfix pull_left" href="{media_path}">'
            '<div class="video_play_bg"><div class="gif_play">GIF</div></div>'
            '</a></div>'
            '</div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])
        client, _, _ = self.run_once()

        self.assertEqual(client.kinds("send_message"), [])
        media_calls = client.kinds("send_file")
        self.assertEqual(len(media_calls), 1)
        sent_files, kwargs = media_calls[0][1], media_calls[0][2]
        self.assertEqual(sent_files, os.path.join(self.folder.name, media_path))
        self.assertIn("Forwarded from HLI8IOS", kwargs["caption"])
        self.assertIn("5 November 2026 · 19:35:11", kwargs["caption"])
        entities = kwargs["formatting_entities"]
        quote = next(e for e in entities if type(e).__name__ == "MessageEntityBlockquote")
        quote_text = kwargs["caption"][quote.offset:quote.offset + quote.length]
        self.assertEqual(quote_text, "Forwarded from HLI8IOS\n5 November 2026 · 19:35:11")
        italic = next(e for e in entities if type(e).__name__ == "MessageEntityItalic"
                      and "19:35:11" in kwargs["caption"][e.offset:e.offset + e.length])
        self.assertIn("5 November 2026 · 19:35:11",
                      kwargs["caption"][italic.offset:italic.offset + italic.length])
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"151": 1001})

    def test_reconstructed_forward_text_and_media_share_one_caption(self):
        media_path = "video_files/forward-with-caption.mp4"
        fakes.write_file(self.folder.name, media_path)
        forwarded = (
            '<div class="message default clearfix" id="message153">'
            '<div class="body">'
            '<div class="pull_right date details" title="08.11.2026 09:28:59 UTC+02:00">09:28</div>'
            '<div class="from_name">jbvjhvjhvjh</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name">HLI8IOS '
            '<span class="date details" title="05.11.2026 19:35:11 UTC+02:00">'
            '05.11.2026 19:35:11</span></div>'
            f'<div class="text"><em>original caption</em></div>'
            f'<div class="media_wrap clearfix"><a class="video_file_wrap" href="{media_path}"></a></div>'
            '</div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])
        client, _, _ = self.run_once()
        self.assertEqual(client.kinds("send_message"), [])
        calls = client.kinds("send_file")
        self.assertEqual(len(calls), 1)
        caption = calls[0][2]["caption"]
        self.assertTrue(caption.endswith("original caption"))
        self.assertIn("Forwarded from HLI8IOS", caption)
        entities = calls[0][2]["formatting_entities"]
        names = [type(entity).__name__ for entity in entities]
        self.assertIn("MessageEntityBlockquote", names)
        self.assertIn("MessageEntityItalic", names)
        quote = next(e for e in entities if type(e).__name__ == "MessageEntityBlockquote")
        quote_text = caption[quote.offset:quote.offset + quote.length]
        self.assertEqual(quote_text, "Forwarded from HLI8IOS\n5 November 2026 · 19:35:11")
        italic_caption = next(e for e in entities if type(e).__name__ == "MessageEntityItalic"
                              and caption[e.offset:e.offset + e.length] == "original caption")
        self.assertEqual(caption[italic_caption.offset:italic_caption.offset + italic_caption.length],
                         "original caption")

    def test_native_forward_with_media_and_archive_header_reconstructs_inline(self):
        media_path = "video_files/native-forward.mp4"
        fakes.write_file(self.folder.name, media_path)
        forwarded = (
            '<div class="message default clearfix" id="message152">'
            '<div class="body">'
            '<div class="pull_right date details" title="08.11.2026 09:28:59 UTC+02:00">09:28</div>'
            '<div class="from_name">Alice</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name"><a href="https://t.me/NewsChannel/4321">News Channel</a></div>'
            '<div class="media_wrap clearfix">'
            f'<a class="video_file_wrap" href="{media_path}"></a>'
            '</div>'
            '<div class="text">original video caption</div>'
            '</div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])
        native_calls = []

        async def native_forward(_client, _channel, peer_hint, source_msg_id, archive_source_id=None):
            native_calls.append((peer_hint, source_msg_id))
            return 777

        with patch.object(pipeline.sending, "_try_native_forward", new=native_forward):
            client, _, _ = self.run_once()

        self.assertEqual(native_calls, [])
        self.assertEqual(client.kinds("send_message"), [])
        media_calls = client.kinds("send_file")
        self.assertEqual(len(media_calls), 1)
        caption = media_calls[0][2]["caption"]
        self.assertIn("Forwarded from News Channel", caption)
        self.assertIn("original video caption", caption)

    def test_real_telegram_forward_body_link_is_used_for_native_source(self):
        forwarded = html.fromstring(
            '<div class="message default clearfix" id="message137">'
            '<div class="body">'
            '<div class="from_name">jbvjhvjhvjh</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name"><a href="https://t.me/NewsChannel/4321">News Channel</a>'
            '<span class="date details" title="08.11.2026 07:09:09 UTC+02:00">'
            '08.11.2026 07:09:09</span></div>'
            '<div class="text">forwarded text</div>'
            '</div></div></div>'
        )
        with config.use_settings(self.settings()):
            data = htmlexport.extract_message_data(forwarded)
        self.assertEqual(data["forward_source"], ("NewsChannel", 4321))
        self.assertEqual(data["forward_name"], "News Channel")
        self.assertTrue(data["is_forwarded"])
        self.assertEqual(data["forward_header"], "Forwarded from News Channel\n")
        self.assertEqual(data["forward_timestamp"].strftime("%d.%m.%Y %H:%M:%S"),
                         "08.11.2026 07:09:09")
        self.assertTrue(data["forward_timestamp_has_time"])
        self.assertTrue(data["forward_timestamp_has_seconds"])

    def test_native_forward_path_remains_available_without_archive_annotation(self):
        forwarded = (
            '<div class="message default clearfix" id="message139">'
            '<div class="body">'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name"><a href="https://t.me/NewsChannel/4321">News Channel</a></div>'
            '<div class="text">forwarded text</div>'
            '</div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])
        native_calls = []

        async def native_forward(_client, _channel, peer_hint, source_msg_id, archive_source_id=None):
            native_calls.append((peer_hint, source_msg_id))
            return 777

        with patch.object(pipeline.sending, "_try_native_forward", new=native_forward):
            client, output, _ = self.run_once()

        self.assertEqual(native_calls, [("NewsChannel", 4321)])
        self.assertEqual(client.kinds("send_message"), [])
        self.assertIn("native_forwards=1", output)

    def test_reply_to_a_later_message_is_deferred_then_linked(self):
        fakes.write_export(self.folder.name, [
            fakes.message(1, fakes.text("first")),
            fakes.message(2, fakes.reply_to(3) + fakes.text("reply to the next one")),
            fakes.message(3, fakes.text("the target")),
        ])
        client, _, _ = self.run_once()
        order = client.texts()
        self.assertTrue(order[1].endswith("the target"), order)
        reply_call = [c for c in client.kinds("send_message") if "reply to the next one" in c[1]][0]
        target_id = 1002
        self.assertEqual(reply_call[2]["reply_to"], target_id)

    def test_reply_to_a_message_outside_the_export_gets_a_note(self):
        fakes.write_export(self.folder.name, [fakes.message(5, fakes.reply_to(999) + fakes.text("orphan reply"))])
        client, _, _ = self.run_once()
        self.assertIn("Original reply target #999 is unavailable", client.texts()[0])

    def test_reply_to_a_specific_member_of_an_inferred_album_uses_that_member_id(self):
        first_path = "photos/album-first.jpg"
        second_path = "photos/album-second.jpg"
        fakes.write_file(self.folder.name, first_path)
        fakes.write_file(self.folder.name, second_path)
        fakes.write_export(self.folder.name, [
            fakes.message(
                901, fakes.photo(first_path) + fakes.text("album caption"),
                sender="Alice", minute=1,
            ),
            fakes.message(
                902, fakes.photo(second_path),
                sender="", classes="joined", minute=1,
            ),
            fakes.message(
                903, fakes.reply_to(902) + fakes.text("reply to the second album item"),
                sender="Alice", minute=2,
            ),
        ])

        client, _, _ = self.run_once()
        album_calls = client.kinds("send_file")
        self.assertEqual(len(album_calls), 1)
        self.assertEqual(album_calls[0][1], [
            self.path(first_path),
            self.path(second_path),
        ])

        reply_call = next(
            call for call in client.kinds("send_message")
            if "reply to the second album item" in call[1]
        )
        self.assertEqual(reply_call[2]["reply_to"], 1002)
        with config.use_settings(self.settings()):
            self.assertEqual(
                progress.load_id_map(),
                {"901": 1001, "902": 1002, "903": 1003},
            )

    def test_forwarded_message_reply_is_not_transferred_to_adjacent_sticker(self):
        # Reproduce the user's actual export records 106–108. Message107
        # explicitly replies to 106; message108 is a separate sticker with
        # no reply link of its own and must not inherit 107's reply target.
        sticker_path = "video_files/sticker (10).webm"
        photo_path = "photos/photo_7@01-07-2026_21-33-40.jpg"
        fakes.write_file(self.folder.name, sticker_path)
        fakes.write_file(self.folder.name, photo_path)

        first_forward = (
            '<div class="message default clearfix" id="message106">'
            '<div class="body">'
            '<div class="pull_right date details" title="01.07.2026 21:33:39 UTC+02:00">21:33</div>'
            '<div class="from_name">Archive owner</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name">Valerie Demian '
            '<span class="date details" title="03.05.2026 20:27:41 UTC+02:00">'
            '03.05.2026 20:27:41</span></div>'
            '<div class="reply_to details">In reply to '
            '<a href="#go_to_message105">this message</a></div>'
            '<div class="media_wrap clearfix">'
            '<a class="media clearfix pull_left block_link media_photo" href="' + sticker_path + '">'
            '<div class="body"><div class="title bold">Sticker</div>'
            '<div class="status details">🧐</div></div></a>'
            '</div></div></div></div>'
        )
        photo_forward = (
            '<div class="message default clearfix joined" id="message107">'
            '<div class="body">'
            '<div class="pull_right date details" title="01.07.2026 21:33:40 UTC+02:00">21:33</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name">Valerie Demian '
            '<span class="date details" title="03.05.2026 20:43:50 UTC+02:00">'
            '03.05.2026 20:43:50</span></div>'
            '<div class="reply_to details">In reply to '
            '<a href="#go_to_message106">this message</a></div>'
            '<div class="media_wrap clearfix">'
            '<a class="photo_wrap clearfix pull_left" href="' + photo_path + '">'
            '<img class="photo" src="' + photo_path + '_thumb.jpg"/></a>'
            '</div>'
            '<div class="text">The impotent old man and the sea of tears</div>'
            '</div></div></div>'
        )
        repeated_sticker = (
            '<div class="message default clearfix joined" id="message108">'
            '<div class="body">'
            '<div class="pull_right date details" title="01.07.2026 21:33:40 UTC+02:00">21:33</div>'
            '<div class="forwarded body">'
            '<div class="media_wrap clearfix">'
            '<a class="media clearfix pull_left block_link media_photo" href="' + sticker_path + '">'
            '<div class="body"><div class="title bold">Sticker</div>'
            '<div class="status details">🧐</div></div></a>'
            '</div></div></div></div>'
        )
        fakes.write_export(self.folder.name, [
            fakes.message(105, fakes.text("earlier source message")),
            first_forward,
            photo_forward,
            repeated_sticker,
        ])

        with config.use_settings(self.settings(native_forward=False)):
            document = html.fromstring(
                open(self.path("messages.html"), encoding="utf-8").read()
            )
            parsed = {
                source_id: htmlexport.extract_message_data(
                    document.xpath('//*[@id="message' + source_id + '"]')[0]
                )
                for source_id in ("106", "107", "108")
            }
            self.assertEqual(parsed["106"]["forward_reply_src_id"], "105")
            self.assertEqual(parsed["107"]["forward_reply_src_id"], "106")
            self.assertIsNone(parsed["108"]["forward_reply_src_id"])
            groups = htmlexport.infer_media_groups([self.path("messages.html")])

        self.assertEqual(groups, {})

        client, _, _ = self.run_once(native_forward=False)
        media_calls = client.kinds("send_file")
        self.assertEqual(len(media_calls), 3)
        sticker_calls = [
            call for call in media_calls
            if call[1] == os.path.join(self.folder.name, sticker_path)
        ]
        photo_call = next(
            call for call in media_calls
            if call[1] == os.path.join(self.folder.name, photo_path)
        )
        self.assertEqual(len(sticker_calls), 2)
        self.assertEqual(sticker_calls[0][2]["reply_to"], 1001)
        self.assertEqual(photo_call[2]["reply_to"], 1002)
        self.assertIsNone(sticker_calls[1][2]["reply_to"])
        self.assertIn("Forwarded from Valerie Demian", sticker_calls[1][2]["caption"])
        self.assertNotIn("The impotent old man and the sea", sticker_calls[1][2]["caption"])
        self.assertIn("The impotent old man and the sea", photo_call[2]["caption"])
        self.assertEqual(client.texts(), ["Alice\n\nearlier source message"])
        with config.use_settings(self.settings()):
            self.assertEqual(
                progress.load_id_map(),
                {"105": 1001, "106": 1002, "107": 1003, "108": 1004},
            )

    def test_original_forwarded_reply_context_disables_native_forward_for_all_media_kinds(self):
        cases = [
            ("photo", "photos/forwarded.jpg",
             '<div class="media_wrap clearfix"><a class="photo_wrap" href="photos/forwarded.jpg">'
             '<img class="photo" src="photos/forwarded.jpg"/></a></div>'),
            ("video", "video_files/forwarded.mp4",
             '<div class="media_wrap clearfix"><a class="video_file_wrap" href="video_files/forwarded.mp4"></a></div>'),
            ("audio", "audio_files/forwarded.mp3",
             '<div class="media_wrap clearfix"><a class="media_audio_file" href="audio_files/forwarded.mp3"></a></div>'),
            ("voice", "voice_messages/forwarded.ogg",
             '<div class="media_wrap clearfix"><a class="media_voice_message" href="voice_messages/forwarded.ogg"></a></div>'),
            ("gif", "video_files/forwarded.gif",
             '<div class="media_wrap clearfix"><a class="video_file_wrap" href="video_files/forwarded.gif"></a></div>'),
            ("document", "files/forwarded.pdf",
             '<div class="media_wrap clearfix"><a class="media_file" href="files/forwarded.pdf"></a></div>'),
            ("sticker_static", "stickers/forwarded.webp",
             '<div class="sticker_wrap"><a href="stickers/forwarded.webp"></a></div>'),
            ("sticker_animated", "stickers/forwarded.tgs",
             '<div class="sticker_wrap"><a href="stickers/forwarded.tgs"></a></div>'),
            ("sticker_video", "stickers/forwarded.webm",
             '<div class="sticker_wrap"><a href="stickers/forwarded.webm"></a></div>'),
            ("round_video", "round_video_messages/forwarded.mp4",
             '<div class="roundvideo_file_wrap"><a href="round_video_messages/forwarded.mp4"></a></div>'),
        ]

        for index, (expected_kind, media_path, media_html) in enumerate(cases, start=1):
            with self.subTest(kind=expected_kind):
                self.folder.cleanup()
                self.folder = tempfile.TemporaryDirectory()
                self.addCleanup(self.folder.cleanup)
                fakes.write_file(self.folder.name, media_path)

                forwarded = (
                    f'<div class="message default clearfix" id="message{900 + index}">'
                    '<div class="body">'
                    '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
                    '<div class="forwarded body">'
                    '<div class="from_name"><a href="https://t.me/NewsChannel/4321">'
                    'News Channel</a></div>'
                    '<div class="reply_to details">In reply to '
                    '<a href="#go_to_message4320">the original conversation</a></div>'
                    f'{media_html}'
                    '</div></div></div>'
                )
                fakes.write_export(self.folder.name, [forwarded])

                native_calls = []

                async def native_forward(_client, _channel, peer_hint, source_msg_id,
                                         archive_source_id=None):
                    native_calls.append((peer_hint, source_msg_id))
                    return 777

                with patch.object(pipeline.sending, "_try_native_forward", new=native_forward):
                    client, _, _ = self.run_once()

                self.assertEqual(native_calls, [])
                calls = client.kinds("send_file")
                self.assertEqual(len(calls), 1)
                kwargs = calls[0][2]
                self.assertIn("Forwarded from News Channel", kwargs["caption"])
                self.assertNotIn("In reply to the original conversation", kwargs["caption"])
                self.assertIsNone(kwargs["reply_to"])
                self.assertEqual(client.kinds("send_message"), [])
                if expected_kind == "voice":
                    self.assertTrue(kwargs["voice_note"])
                if expected_kind == "round_video":
                    self.assertNotIn("video_note", kwargs)
                self.assertTrue(
                    any(type(entity).__name__ == "MessageEntityBlockquote"
                        for entity in kwargs["formatting_entities"])
                )

    def test_forwarded_original_reply_takes_priority_over_outer_reply(self):
        media_path = "audio_files/forwarded-track.mp3"
        fakes.write_file(self.folder.name, media_path)
        forwarded = (
            '<div class="message default clearfix" id="message2">'
            '<div class="body">'
            '<div class="reply_to details">In reply to '
            '<a href="#go_to_message1">this destination-channel message</a></div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name"><a href="https://t.me/NewsChannel/4321">'
            'News Channel</a></div>'
            '<div class="reply_to details">In reply to '
            '<a href="#go_to_message4320">the original conversation</a></div>'
            '<div class="media_wrap clearfix">'
            f'<a class="media_audio_file" href="{media_path}"></a>'
            '</div></div></div></div>'
        )
        fakes.write_export(self.folder.name, [
            fakes.message(1, fakes.text("destination reply target")),
            forwarded,
        ])

        client, _, _ = self.run_once()
        calls = client.kinds("send_file")
        self.assertEqual(len(calls), 1)
        kwargs = calls[0][2]
        self.assertIsNone(kwargs["reply_to"])
        self.assertNotIn("In reply to the original conversation", kwargs["caption"])
        self.assertNotIn("destination-channel message", kwargs["caption"])
        self.assertNotIn("Original reply target #4320 is unavailable", kwargs["caption"])

    def test_forwarded_voice_note_does_not_render_reply_preview_as_caption(self):
        media_path = "voice_messages/forwarded-note.ogg"
        fakes.write_file(self.folder.name, media_path)
        forwarded = (
            '<div class="message default clearfix" id="message800">'
            '<div class="body">'
            '<div class="pull_right date details" '
            'title="08.11.2026 09:28:59 UTC+02:00">09:28</div>'
            '<div class="from_name">Archive owner</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name">Original voice author '
            '<span class="date details" title="05.11.2026 19:35:11 UTC+02:00">'
            '05.11.2026 19:35:11</span></div>'
            '<div class="reply_to details">In reply to '
            '<a href="#go_to_message799">the earlier conversation</a></div>'
            '<div class="media_wrap clearfix">'
            '<a class="media_voice_message" href="' + media_path + '"></a>'
            '</div></div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])

        with config.use_settings(self.settings(native_forward=False)):
            document = html.fromstring(
                open(self.path("messages.html"), encoding="utf-8").read()
            )
            parsed = htmlexport.extract_message_data(
                document.xpath('//*[@id="message800"]')[0]
            )
        self.assertIsNone(parsed["reply_src_id"])
        self.assertEqual(parsed["reply_preview"], "")
        self.assertEqual(parsed["forward_reply_src_id"], "799")
        self.assertEqual(parsed["forward_reply_preview"], "In reply to the earlier conversation")

        client, _, _ = self.run_once(native_forward=False)
        self.assertEqual(client.kinds("send_message"), [])
        calls = client.kinds("send_file")
        self.assertEqual(len(calls), 1)
        kwargs = calls[0][2]
        self.assertTrue(kwargs["voice_note"])
        self.assertIsNone(kwargs["reply_to"])
        self.assertIn("Forwarded from Original voice author", kwargs["caption"])
        self.assertNotIn("In reply to the earlier conversation", kwargs["caption"])
        self.assertTrue(any(
            type(entity).__name__ == "MessageEntityBlockquote"
            for entity in kwargs["formatting_entities"]
        ))  # The remaining blockquote is the forwarded-from attribution.

    def test_oversize_file_stops_cleanly_and_the_next_run_skips_it(self):
        fakes.write_file(self.folder.name, "video_files/big.mp4", size=2 * 1024 * 1024)
        fakes.write_export(self.folder.name, [
            fakes.message(1, fakes.text("before")),
            fakes.message(2, fakes.text("has a big video") + fakes.video("video_files/big.mp4")),
            fakes.message(3, fakes.text("after")),
            fakes.message(4, fakes.reply_to(2) + fakes.text("reply to the big one")),
        ])
        client, output, _ = self.run_once()
        self.assertEqual(len(client.texts()), 1)
        self.assertEqual(client.kinds("send_file"), [])
        self.assertTrue(client.disconnected)
        self.assertIn("STOPPED: message id=2", output)
        self.assertIn(os.path.abspath(self.path("video_files/big.mp4")), output)
        with open(self.path("failed_messages.txt"), encoding="utf-8") as handle:
            self.assertTrue(handle.read().startswith("2\tFILE_TOO_LARGE"))
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"1": 1001})
            self.assertEqual(bigfiles.load_manual_uploads()[0], {"2"})
        self.assertFalse(os.path.exists(self.path("migration_reply_audit.jsonl")))
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

    def test_inline_custom_emoji_stays_inside_original_media_caption(self):
        sticker_rel = "stickers/flower.webp"
        photo_rel = "photos/post.jpg"
        fakes.write_file(self.folder.name, sticker_rel)
        fakes.write_file(self.folder.name, photo_rel)
        inner = (
            fakes.text(
                'caption before <a href="stickers/flower.webp">🌸</a> after'
            )
            + fakes.photo(photo_rel)
        )
        fakes.write_export(self.folder.name, [
            fakes.message(901, inner, sender="Alice", minute=10)
        ])
        sticker_path = os.path.abspath(self.path(sticker_rel))

        async def prepared_ids(_client, _files, *, peer, dry_run=False):
            self.assertFalse(dry_run)
            self.assertIsNotNone(peer)
            return {
                customemoji.reference_key(sticker_path, "sticker_static", "🌸"): 987654321
            }

        with patch.object(
            pipeline.customemoji, "prepare_inline_custom_emojis", new=prepared_ids
        ):
            client, _, _ = self.run_once()

        self.assertEqual(client.kinds("send_message"), [])
        calls = client.kinds("send_file")
        self.assertEqual(len(calls), 1)
        caption = calls[0][2]["caption"]
        entities = calls[0][2]["formatting_entities"]
        custom_entities = [
            entity for entity in entities
            if isinstance(entity, tl.MessageEntityCustomEmoji)
        ]
        self.assertEqual(len(custom_entities), 1)
        entity = custom_entities[0]
        self.assertEqual(entity.document_id, 987654321)
        self.assertEqual(
            caption.encode("utf-16-le")[2 * entity.offset:2 * (entity.offset + entity.length)].decode("utf-16-le"),
            "🌸",
        )
        self.assertIn("caption before 🌸 after", caption)

    def test_forwarded_voice_note_keeps_attribution_in_the_voice_message(self):
        media_path = "voice_messages/forwarded-note.ogg"
        fakes.write_file(self.folder.name, media_path)
        forwarded = (
            '<div class="message default clearfix" id="message800">'
            '<div class="body">'
            '<div class="pull_right date details" '
            'title="08.11.2026 09:28:59 UTC+02:00">09:28</div>'
            '<div class="from_name">Archive owner</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name">Original voice author '
            '<span class="date details" title="05.11.2026 19:35:11 UTC+02:00">'
            '05.11.2026 19:35:11</span></div>'
            '<div class="media_wrap clearfix">'
            '<a class="media_voice_message" href="' + media_path + '"></a>'
            '</div></div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])

        client, _, _ = self.run_once(native_forward=False)
        self.assertEqual(client.kinds("send_message"), [])
        calls = client.kinds("send_file")
        self.assertEqual(len(calls), 1)
        kwargs = calls[0][2]
        self.assertTrue(kwargs["voice_note"])
        self.assertIn("Forwarded from Original voice author", kwargs["caption"])
        self.assertIn("5 November 2026 · 19:35:11", kwargs["caption"])
        self.assertIsNone(kwargs["reply_to"])

    def test_mp3_with_audio_export_markup_is_sent_as_audio_not_forced_document(self):
        relative = "files/voice-track.mp3"
        fakes.write_file(self.folder.name, relative)
        markup = (
            '<div class="media_wrap clearfix">'
            f'<a class="media clearfix pull_left block_link media_audio_file" href="{relative}">'
            '<div class="title bold">voice-track.mp3</div>'
            '<div class="status details">00:30</div>'
            '</a></div>'
        )
        fakes.write_export(self.folder.name, [
            fakes.message(801, fakes.text("audio caption") + markup, sender="Alice", minute=4)
        ])

        client, _, _ = self.run_once()
        calls = client.kinds("send_file")
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0][2].get("force_document", False))
        self.assertTrue(calls[0][2]["caption"].endswith("audio caption"))

    def test_batch_limit_stops_the_run(self):
        fakes.write_export(self.folder.name, [fakes.message(i, fakes.text(f"m{i}")) for i in range(1, 6)])
        client, output, _ = self.run_once(batch_limit=2)
        self.assertEqual(len(client.texts()), 2)
        self.assertIn("Batch limit reached", output)

    def test_missing_credentials_raise_a_config_error(self):
        with self.assertRaises(config.ConfigError):
            asyncio.run(pipeline.run_async(Settings(base_dir=self.folder.name)))

    def test_run_async_returns_true_on_success(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("hello"))])
        settings = self.settings()
        with contextlib.redirect_stdout(io.StringIO()):
            with config.use_settings(settings):
                result = asyncio.run(pipeline.run_async(settings))
        self.assertIs(result, True)

    def test_run_async_returns_false_when_export_folder_is_empty(self):
        settings = self.settings()
        with contextlib.redirect_stdout(io.StringIO()):
            with config.use_settings(settings):
                result = asyncio.run(pipeline.run_async(settings))
        self.assertIs(result, False)

    def test_run_async_returns_false_when_channel_cannot_be_resolved(self):
        fakes.write_export(self.folder.name, [fakes.message(1, fakes.text("hello"))])
        settings = self.settings()

        async def broken_get_entity(self, peer):
            raise ValueError("channel not found")

        with patch.object(FakeTelegram, "get_entity", broken_get_entity):
            with contextlib.redirect_stdout(io.StringIO()):
                with config.use_settings(settings):
                    result = asyncio.run(pipeline.run_async(settings))
        self.assertIs(result, False)


    def test_forwarded_joined_file_groups_of_every_valid_size_stay_separate_from_prelude(self):
        def forwarded_file(message_id, original_second, relative, joined=False,
                           archive_sender=False):
            outer_time = f"01.07.2026 21:46:{original_second:02d} UTC+02:00"
            original_time = f"04.06.2026 09:46:{original_second:02d} UTC+02:00"
            classes = "message default clearfix" + (" joined" if joined else "")
            sender = '<div class="from_name">Archive owner</div>' if archive_sender else ""
            return (
                f'<div class="{classes}" id="message{message_id}"><div class="body">'
                f'<div class="pull_right date details" title="{outer_time}">21:46</div>'
                f'{sender}'
                '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
                '<div class="forwarded body"><div class="from_name">🗝 '
                f'<span class="date details" title="{original_time}">{original_time}</span>'
                '</div><div class="media_wrap clearfix">'
                f'<a class="media clearfix pull_left block_link media_file" href="{relative}">'
                '<div class="fill pull_left"></div><div class="body">'
                f'<div class="title bold">{os.path.basename(relative)}</div>'
                '<div class="status details">250 KB</div></div></a></div></div></div></div>'
            )

        # Telegram media albums support 2–10 items. Exercise every valid size:
        # the preceding non-joined forwarded file must never be absorbed.
        for album_size in range(2, 11):
            with self.subTest(album_size=album_size):
                self.setUp()
                try:
                    # A real 31-second source-time gap, not merely a missing
                    # joined marker, separates this standalone prelude.
                    rows = [(319, 10, "files/preceding.jpg", False)]
                    rows.extend(
                        (320 + index, 41 + index // 2,
                         f"files/group-{index:02d}.jpg", True)
                        for index in range(album_size)
                    )
                    html_rows = []
                    expected_album = []
                    for index, (message_id, second, relative, joined) in enumerate(rows):
                        fakes.write_file(self.folder.name, relative)
                        html_rows.append(forwarded_file(
                            message_id, second, relative, joined=joined,
                            archive_sender=(index == 0),
                        ))
                        if joined:
                            expected_album.append(self.path(relative))
                    fakes.write_export(self.folder.name, html_rows)

                    client, _output, _ = self.run_once()
                    media_calls = client.kinds("send_file")
                    self.assertEqual(len(media_calls), 2)
                    self.assertEqual(media_calls[0][1], self.path("files/preceding.jpg"))
                    self.assertEqual(media_calls[1][1], expected_album)
                    self.assertEqual(len(media_calls[1][2]["caption"]), album_size)
                    self.assertTrue(all(
                        not caption for caption in media_calls[1][2]["caption"][:-1]
                    ))
                    self.assertIn(
                        "Forwarded from 🗝", media_calls[1][2]["caption"][-1]
                    )
                finally:
                    self.tearDown()

    def test_eleven_forwarded_jpg_files_keep_first_ten_together_in_source_order(self):
        # Reproduce the user's actual Desktop HTML shape: messages 319-329
        # are one compatible source-order run. Telegram permits ten media
        # items per album, so 319-328 must stay together and 329 follows alone.
        # Desktop exports these JPGs as media_file (documents), not photo_wrap.
        def forwarded_file(message_id, export_second, original_second, relative, joined=False,
                           archive_sender=False):
            outer_minute, outer_second = divmod(export_second, 60)
            source_minute, source_second = divmod(original_second, 60)
            outer_time = (
                f"01.07.2026 21:{46 + outer_minute:02d}:{outer_second:02d} UTC+02:00"
            )
            original_time = (
                f"04.06.2026 09:{46 + source_minute:02d}:{source_second:02d} UTC+02:00"
            )
            classes = "message default clearfix" + (" joined" if joined else "")
            sender = '<div class="from_name">Archive owner</div>' if archive_sender else ""
            return (
                f'<div class="{classes}" id="message{message_id}"><div class="body">'
                f'<div class="pull_right date details" title="{outer_time}">21:46</div>'
                f'{sender}'
                '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
                '<div class="forwarded body">'
                '<div class="from_name">🗝 '
                f'<span class="date details" title="{original_time}">{original_time}</span>'
                '</div>'
                '<div class="media_wrap clearfix">'
                f'<a class="media clearfix pull_left block_link media_file" href="{relative}">'
                '<div class="fill pull_left"></div><div class="body">'
                f'<div class="title bold">{os.path.basename(relative)}</div>'
                '<div class="status details">250 KB</div></div></a>'
                '</div></div></div></div>'
            )

        source_rows = [
            (319, 50, 55, "files/preceding.jpg", False),
            (320, 50, 56, "files/group-01.jpg", True),
            (321, 51, 56, "files/group-02.jpg", True),
            (322, 51, 57, "files/group-03.jpg", True),
            (323, 52, 57, "files/group-04.jpg", True),
            (324, 52, 58, "files/group-05.jpg", True),
            (325, 53, 58, "files/group-06.jpg", True),
            (326, 54, 59, "files/group-07.jpg", True),
            (327, 55, 59, "files/group-08.jpg", True),
            (328, 55, 59, "files/group-09.jpg", True),
            (329, 55, 60, "files/group-10.jpg", True),
        ]
        html_rows = []
        expected_group_files = []
        final_file = None
        for index, (message_id, export_second, original_second, relative, joined) in enumerate(source_rows):
            fakes.write_file(self.folder.name, relative)
            html_rows.append(forwarded_file(
                message_id, export_second, original_second, relative,
                joined=joined, archive_sender=(index == 0),
            ))
            if index < 10:
                expected_group_files.append(self.path(relative))
            else:
                final_file = self.path(relative)
        fakes.write_export(self.folder.name, html_rows)

        client, output, _ = self.run_once()
        media_calls = client.kinds("send_file")
        self.assertEqual(len(media_calls), 2)

        # The first ten HTML records remain together in their original order.
        album_files, album_kwargs = media_calls[0][1], media_calls[0][2]
        self.assertEqual(album_files, expected_group_files)
        self.assertTrue(album_kwargs["force_document"])
        self.assertEqual(len(album_kwargs["caption"]), 10)
        self.assertEqual(album_kwargs["caption"][:-1], [""] * 9)
        self.assertIn("Forwarded from 🗝", album_kwargs["caption"][-1])
        self.assertIn("4 June 2026", album_kwargs["caption"][-1])

        # The eleventh file is not moved in front of the album or absorbed.
        self.assertEqual(media_calls[1][1], final_file)
        self.assertIn("Forwarded from 🗝", media_calls[1][2]["caption"])
        self.assertEqual(client.kinds("send_message"), [])
        self.assertIn("album 319–328", output)

        with config.use_settings(self.settings()):
            mapped = progress.load_id_map()
        self.assertEqual(set(mapped), {str(row[0]) for row in source_rows})
        self.assertEqual(len({mapped[str(i)] for i in range(319, 329)}), 10)

    def test_eleven_consecutive_file_media_are_chunked_first_ten_then_one(self):
        # A non-joined first record can be the first album item. Ten items
        # stay together in source order; item eleven becomes the next
        # one-item remainder rather than causing the first item to be dropped.
        preceding = "files/prelude.jpg"
        fakes.write_file(self.folder.name, preceding)
        preceding_markup = (
            '<div class="media_wrap clearfix">'
            f'<a class="media clearfix pull_left block_link media_file" href="{preceding}"></a>'
            '</div>'
        )
        records = [
            fakes.message(701, preceding_markup, sender="Alice", minute=2)
        ]
        expected_files = [os.path.join(self.folder.name, preceding)]
        final_file = None
        for source_id in range(702, 712):
            relative = f"files/document-{source_id}.pdf"
            fakes.write_file(self.folder.name, relative)
            if source_id <= 710:
                expected_files.append(os.path.join(self.folder.name, relative))
            else:
                final_file = os.path.join(self.folder.name, relative)
            attachment = (
                '<div class="media_wrap clearfix">'
                f'<a class="media clearfix pull_left block_link media_file" href="{relative}">'
                f'<div class="title bold">document-{source_id}.pdf</div>'
                '</a></div>'
            )
            records.append(
                fakes.message(source_id, attachment, sender="", classes="joined", minute=2)
            )
        records.append(fakes.message(712, fakes.text("after the album"), sender="Alice", minute=3))
        fakes.write_export(self.folder.name, records)

        client, output, _ = self.run_once()

        media_calls = client.kinds("send_file")
        self.assertEqual(len(media_calls), 2)
        album_files, album_kwargs = media_calls[0][1], media_calls[0][2]
        self.assertEqual(album_files, expected_files)
        self.assertTrue(album_kwargs["force_document"])
        self.assertEqual(len(album_kwargs["caption"]), 10)
        self.assertEqual(album_kwargs["caption"][:-1], [""] * 9)
        self.assertTrue(album_kwargs["caption"][-1].startswith("Alice\n12 March 2023"))

        self.assertEqual(media_calls[1][1], final_file)
        self.assertEqual(len(client.texts()), 1)
        self.assertIn("after the album", client.texts()[0])

        with config.use_settings(self.settings()):
            mapped = progress.load_id_map()
        self.assertEqual(set(mapped), {str(i) for i in range(701, 713)})
        self.assertEqual(len({mapped[str(i)] for i in range(701, 711)}), 10)
        self.assertIn("album 701–710", output)
        self.assertIn("sent=12", output)

    def test_source_group_inference_is_type_agnostic_for_every_supported_size(self):
        records = []
        expected = {}
        next_id = 2000
        media_builders = (
            ("photo", lambda p: fakes.photo(p), "photos"),
            ("video", lambda p: fakes.video(p), "video_files"),
            ("gif", lambda p: (
                '<div class="media_wrap clearfix"><a class="animated_wrap" '
                f'href="{p}"><div class="gif_play">GIF</div></a></div>'
            ), "video_files"),
            ("voice", lambda p: (
                '<div class="media_wrap clearfix"><a class="media_voice_message" '
                f'href="{p}"></a></div>'
            ), "voice_messages"),
            ("sticker", lambda p: (
                '<div class="sticker_wrap"><a '
                f'href="{p}"></a></div>'
            ), "stickers"),
        )

        for count in range(2, 11):
            first_id = next_id
            expected[str(first_id)] = count
            for index in range(count):
                kind, builder, folder = media_builders[index % len(media_builders)]
                extension = {
                    "photo": ".jpg", "video": ".mp4", "gif": ".mp4",
                    "voice": ".ogg", "round_video": ".mp4", "sticker": ".webp",
                }[kind]
                relative = f"{folder}/group_{count}_{index}{extension}"
                fakes.write_file(self.folder.name, relative)
                joined = "joined" if index else ""
                caption = fakes.text(f"caption for {count}-item source group") if index == 0 else ""
                records.append(fakes.message(
                    next_id, builder(relative) + caption,
                    sender="Alice", classes=joined,
                ))
                next_id += 1
            # A text-only record is an explicit boundary between source groups.
            records.append(fakes.message(next_id, fakes.text("group boundary"), sender="Alice"))
            next_id += 1

        fakes.write_export(self.folder.name, records)
        with config.use_settings(self.settings()):
            groups = htmlexport.infer_media_groups([self.path("messages.html")])

        self.assertEqual(set(groups), set(expected))
        for first_id, count in expected.items():
            self.assertEqual(len(groups[first_id]), count)
            self.assertTrue(all(len(member["media"]) == 1 for member in groups[first_id]))

    def test_long_compatible_runs_are_chunked_in_source_order_at_ten_items(self):
        records = []
        expected = {}
        next_id = 3000
        for count in (11, 15, 16, 20):
            first_id = next_id
            expected_sizes = [10] + ([count - 10] if count - 10 >= 2 else [])
            expected[first_id] = expected_sizes
            for index in range(count):
                relative = f"files/long-run-{count}-{index}.jpg"
                fakes.write_file(self.folder.name, relative)
                records.append(fakes.message(
                    next_id, fakes.photo(relative), sender="Alice",
                    classes="" if index == 0 else "joined", minute=5,
                ))
                next_id += 1
            records.append(fakes.message(
                next_id, fakes.text("group boundary"), sender="Alice", minute=6,
            ))
            next_id += 1

        fakes.write_export(self.folder.name, records)
        with config.use_settings(self.settings()):
            groups = htmlexport.infer_media_groups([self.path("messages.html")])

        expected_groups = {}
        for first_id, sizes in expected.items():
            expected_groups[str(first_id)] = sizes[0]
            if len(sizes) > 1:
                expected_groups[str(first_id + sizes[0])] = sizes[1]
        self.assertEqual(set(groups), set(expected_groups))
        for first_id, size in expected_groups.items():
            self.assertEqual(len(groups[first_id]), size)
            self.assertEqual(
                [row["msg_id"] for row in groups[first_id]],
                [str(i) for i in range(int(first_id), int(first_id) + size)],
            )

    def test_joined_jpg_documents_keep_owner_and_caption_in_one_album(self):
        records = []
        expected_files = []
        clocks = ("10:00:00", "10:31:00", "10:32:00")
        for index, source_id in enumerate((1201, 1202, 1203)):
            relative = f"files/grouped-photo-{source_id}.jpg"
            fakes.write_file(self.folder.name, relative)
            expected_files.append(os.path.join(self.folder.name, relative))
            joined = " joined" if index else ""
            sender = '<div class="from_name">Archive owner</div>' if index == 0 else ""
            caption = '<div class="text">Original grouped JPG caption</div>' if index == 0 else ""
            records.append(
                f'<div class="message default clearfix{joined}" id="message{source_id}">'
                '<div class="body">'
                f'<div class="pull_right date details" title="12.03.2023 {clocks[index]} UTC+02:00">t</div>'
                f'{sender}<div class="media_wrap clearfix">'
                f'<a class="media clearfix pull_left block_link media_file" href="{relative}"></a>'
                f'</div>{caption}</div></div>'
            )
        records.append(fakes.message(1204, fakes.text("after JPG album"), sender="Archive owner"))
        fakes.write_export(self.folder.name, records)

        with config.use_settings(self.settings()):
            groups = htmlexport.infer_media_groups([self.path("messages.html")])
        self.assertEqual([row["msg_id"] for row in groups["1201"]], ["1201", "1202", "1203"])

        client, _output, _ = self.run_once()
        calls = client.kinds("send_file")
        self.assertEqual(len(calls), 1)
        sent_files, kwargs = calls[0][1], calls[0][2]
        self.assertEqual(sent_files, expected_files)
        self.assertTrue(kwargs["force_document"])
        self.assertEqual(len(kwargs["caption"]), 3)
        self.assertEqual(kwargs["caption"][:-1], ["", ""])
        self.assertIn("Archive owner", kwargs["caption"][-1])
        self.assertIn("Original grouped JPG caption", kwargs["caption"][-1])
        self.assertFalse(any("Original grouped JPG caption" in text for text in client.texts()))

    def test_partially_mapped_inferred_album_stops_instead_of_scattering_remaining_media(self):
        records = []
        for source_id in (2, 3):
            relative = f"files/document-{source_id}.png"
            fakes.write_file(self.folder.name, relative)
            attachment = (
                '<div class="media_wrap clearfix">'
                f'<a class="media clearfix pull_left block_link media_file" href="{relative}">'
                '</a></div>'
            )
            records.append(
                fakes.message(source_id, attachment, sender="Alice",
                              classes="" if source_id == 2 else "joined", minute=2)
            )
        fakes.write_export(self.folder.name, records)

        # Simulate an interrupted/older run that already mapped only the first
        # source member. The remaining member must not be sent as a standalone.
        with config.use_settings(self.settings()):
            progress.append_id_map("2", 1001)

        client, output, _ = self.run_once()

        self.assertEqual(client.kinds("send_file"), [])
        self.assertIn("album recovery conflict", output)
        self.assertIn("Refusing to scatter the remaining media", output)
        self.assertIn("failed=1", output)
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {"2": 1001})

    def test_dry_run_counts_inferred_album_members_only_once(self):
        records = [fakes.message(1, fakes.text("before"), sender="Alice", minute=1)]
        for source_id in (2, 3):
            relative = f"files/document-{source_id}.png"
            fakes.write_file(self.folder.name, relative)
            attachment = (
                '<div class="media_wrap clearfix">'
                f'<a class="media clearfix pull_left block_link media_file" href="{relative}">'
                '</a></div>'
            )
            records.append(
                fakes.message(source_id, attachment, sender="", classes="joined", minute=2)
            )
        records.append(fakes.message(4, fakes.text("after"), sender="Alice", minute=3))
        fakes.write_export(self.folder.name, records)

        client, output, _ = self.run_once(dry_run=True)

        self.assertEqual(client.kinds("send_message"), [])
        self.assertEqual(client.kinds("send_file"), [])
        self.assertEqual(output.count("[dry] album ids=2,3"), 1)
        self.assertIn("sent=4", output)

    def test_forwarded_album_uses_original_timestamps_and_stops_cleanly_on_failure(self):
        def forwarded_media(source_id, outer_time, forward_time, relative,
                            *, joined=False, outer_sender=True, caption=""):
            classes = " joined" if joined else ""
            sender = '<div class="from_name">⛯⛯</div>' if outer_sender else ""
            caption_html = f'<div class="text">{caption}</div>' if caption else ""
            return (
                f'<div class="message default clearfix{classes}" id="message{source_id}">'
                '<div class="body">'
                f'<div class="pull_right date details" title="{outer_time} UTC+02:00">t</div>'
                f'{sender}'
                '<div class="forwarded body">'
                f'<div class="from_name">espritzeit '
                f'<span class="date details" title="{forward_time} UTC+02:00">{forward_time}</span>'
                '</div>'
                '<div class="media_wrap clearfix">'
                f'<a class="media clearfix pull_left block_link media_file" href="{relative}">'
                '</a></div>'
                f'{caption_html}'
                '</div></div></div>'
            )

        files = []
        forward_times = (
            "01.06.2026 22:39:10",
            "01.06.2026 22:39:11",
            "01.06.2026 22:39:11",
            "01.06.2026 22:39:12",
        )
        outer_times = (
            "01.07.2026 21:46:36",
            "01.07.2026 21:46:45",
            "01.07.2026 21:46:45",
            "01.07.2026 21:46:46",
        )
        records = []
        long_caption = "A long description. " * 90
        for index, source_id in enumerate(range(309, 313)):
            relative = f"files/photo-{source_id}.jpg"
            fakes.write_file(self.folder.name, relative)
            files.append(os.path.join(self.folder.name, relative))
            records.append(forwarded_media(
                source_id, outer_times[index], forward_times[index], relative,
                joined=source_id >= 311, outer_sender=source_id <= 310,
                caption=long_caption if source_id == 312 else "",
            ))
        records.append(fakes.message(313, fakes.text("message after the album"), minute=47))
        fakes.write_export(self.folder.name, records)

        with config.use_settings(self.settings()):
            groups = htmlexport.infer_media_groups([self.path("messages.html")])
        self.assertEqual(list(groups), ["309"])
        self.assertEqual([row["msg_id"] for row in groups["309"]], ["309", "310", "311", "312"])
        self.assertEqual(groups["309"][-1]["body_text"].strip(), long_caption.strip())

        # A failed album is an atomic source unit. The migration must stop at
        # that point so its caption and all later messages remain for retry.
        with patch.object(
            pipeline.sending.uploads, "send_file",
            side_effect=RuntimeError("synthetic album upload failure"),
        ):
            failed_client, failed_output, _ = self.run_once(max_retries=1)
        self.assertEqual(failed_client.kinds("send_file"), [])
        self.assertIn("Stopped at failed inferred album beginning with id=309", failed_output)
        self.assertNotIn("message after the album", failed_client.texts())
        with config.use_settings(self.settings()):
            self.assertEqual(progress.load_id_map(), {})

        # A fresh run retries the complete four-item album, its long caption,
        # and then the following source message in order.
        client, output, _ = self.run_once(max_retries=1)
        album_calls = client.kinds("send_file")
        self.assertEqual(len(album_calls), 1)
        sent_files, album_kwargs = album_calls[0][1], album_calls[0][2]
        self.assertEqual(sent_files, files)
        self.assertTrue(album_kwargs.get("force_document", False))
        expected_caption, expected_entities = album.caption_for_group(groups["309"])
        first_caption, continuation = sending.inferred_album_caption_parts(
            expected_caption, expected_entities,
        )
        self.assertEqual(album_kwargs["caption"], [""] * 3 + [first_caption[0]])
        self.assertEqual(album_kwargs["formatting_entities"][-1], first_caption[1])
        self.assertEqual(
            [text for text in client.texts() if "message after the album" not in text],
            [chunk[0] for chunk in continuation],
        )
        self.assertIn("message after the album", client.texts()[-1])
        self.assertIn("sent=5", output)
        with config.use_settings(self.settings()):
            self.assertEqual(set(progress.load_id_map()), {"309", "310", "311", "312", "313"})

    def test_identical_captions_group_separate_visual_files_without_two_second_limit(self):
        caption = "Shared album description"
        records = []
        expected = []
        for source_id, minute, filename in (
            (401, 1, "photos/first.jpg"),
            (402, 15, "photos/second.png"),
            (403, 35, "video_files/third.mkv"),
        ):
            fakes.write_file(self.folder.name, filename)
            expected.append(os.path.join(self.folder.name, filename))
            media = (
                fakes.video(filename) if filename.startswith("video_files/")
                else fakes.photo(filename)
            )
            records.append(fakes.message(
                source_id, media + fakes.text(caption),
                sender="Alice", minute=minute,
            ))
        records.append(fakes.message(404, fakes.text("after shared-caption album"),
                                     sender="Alice", minute=40))
        fakes.write_export(self.folder.name, records)

        with config.use_settings(self.settings()):
            groups = htmlexport.infer_media_groups([self.path("messages.html")])
        self.assertEqual(list(groups), ["401"])
        self.assertEqual([row["msg_id"] for row in groups["401"]], ["401", "402", "403"])

        client, output, _ = self.run_once()
        album_calls = client.kinds("send_file")
        self.assertEqual(len(album_calls), 1)
        sent_files, album_kwargs = album_calls[0][1], album_calls[0][2]
        self.assertEqual(sent_files, expected)
        self.assertFalse(album_kwargs.get("force_document", False))
        expected_caption, _entities = album.caption_for_group(groups["401"])
        self.assertEqual(album_kwargs["caption"], ["", "", expected_caption])
        self.assertNotIn("Shared album description", client.texts())
        self.assertIn("after shared-caption album", client.texts()[-1])
        self.assertIn("sent=4", output)

    def test_arbitrary_forwarded_media_caption_is_preserved_verbatim(self):
        """Captions are payload data; preservation must not depend on their words."""
        source_caption = "Résumé — 東京\nLine 2: 100% & every word stays."
        media_path = "video_files/arbitrary-caption.mp4"
        fakes.write_file(self.folder.name, media_path)
        forwarded = (
            '<div class="message default clearfix" id="message790">'
            '<div class="body">'
            '<div class="pull_right date details" '
            'title="08.11.2026 09:28:59 UTC+02:00">09:28</div>'
            '<div class="from_name">Archive owner</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name">Original author '
            '<span class="date details" title="05.11.2026 19:35:11 UTC+02:00">'
            '05.11.2026 19:35:11</span></div>'
            '<div class="media_wrap clearfix">'
            f'<a class="video_file_wrap clearfix pull_left" href="{media_path}"></a>'
            '</div>'
            '<div class="text">Résumé — 東京<br>Line 2: 100% &amp; every word stays.</div>'
            '</div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])

        with config.use_settings(self.settings()):
            document = html.fromstring(
                open(self.path("messages.html"), encoding="utf-8").read()
            )
            parsed = htmlexport.extract_message_data(document.xpath('//*[@id="message790"]')[0])
        self.assertEqual(parsed["body_text"], source_caption)

        client, _, _ = self.run_once()
        calls = client.kinds("send_file")
        self.assertEqual(len(calls), 1)
        delivered_caption = calls[0][2]["caption"]
        self.assertIn(source_caption, delivered_caption)
        self.assertIn("Forwarded from Original author", delivered_caption)


    def test_actual_export_forwarded_video_groups_keep_caption_and_boundary(self):
        """Reproduce the actual 448–464 export structure and its 197-second boundary."""
        records = [fakes.message(447, fakes.text("previous album context"),
                                 sender="⛯⛯", minute=1)]
        expected_first, expected_second = [], []
        first_forward_clock = (
            "14:35:07", "14:35:08", "14:35:08", "14:35:09", "14:35:09",
            "14:35:10", "14:35:10", "14:35:11", "14:35:11", "14:35:14",
        )
        second_forward_clock = (
            "14:38:31", "14:38:32", "14:38:32", "14:38:33",
            "14:38:33", "14:38:34", "14:38:34",
        )
        outer_clock = (
            "18:25:32", "18:25:32", "18:25:33", "18:25:33", "18:25:34",
            "18:25:34", "18:25:37", "18:25:37", "18:25:38", "18:25:38",
            "18:25:39", "18:25:39", "18:25:40", "18:25:40", "18:25:41",
            "18:25:45", "18:25:46",
        )
        for index, source_id in enumerate(range(448, 465)):
            if source_id <= 457:
                forward_clock = first_forward_clock[source_id - 448]
                relative = f"video_files/video ({source_id - 416}).mp4"
                expected_first.append(os.path.join(self.folder.name, relative))
            else:
                forward_clock = second_forward_clock[source_id - 458]
                relative = f"video_files/video ({source_id - 416}).mp4"
                expected_second.append(os.path.join(self.folder.name, relative))
            fakes.write_file(self.folder.name, relative)
            joined = source_id not in (454, 463)
            classes = " joined" if joined else ""
            outer_sender = (
                '<div class="from_name">⛯⛯</div>'
                if source_id in (454, 463) else ""
            )
            caption_html = (
                '<div class="text">Paranoid schizophrenia<br>You want more?</div>'
                if source_id == 458 else (
                    f'<div class="text">Different caption on item {source_id}</div>'
                    if source_id == 459 else ""
                )
            )
            records.append(
                f'<div class="message default clearfix{classes}" id="message{source_id}">'
                '<div class="body">'
                f'<div class="pull_right date details" '
                f'title="09.07.2026 {outer_clock[index]} UTC+02:00">18:25</div>'
                f'{outer_sender}'
                '<div class="pull_left forwarded userpic_wrap">'
                '<div class="userpic userpic2"><div class="initials">e</div></div></div>'
                '<div class="forwarded body">'
                f'<div class="from_name">espritzeit '
                f'<span class="date details" title="08.07.2026 {forward_clock} UTC+02:00">'
                f'08.07.2026 {forward_clock}</span></div>'
                '<div class="media_wrap clearfix">'
                f'<a class="video_file_wrap clearfix pull_left" href="{relative}">'
                '<div class="video_play_bg"><div class="video_play"></div></div>'
                '<div class="video_duration">00:06</div>'
                f'<img class="video_file" src="{relative}_thumb.jpg" '
                'style="width: 260px; height: 260px"/>'
                '</a></div>'
                f'{caption_html}'
                '</div></div></div>'
            )
        fakes.write_export(self.folder.name, records)

        with config.use_settings(self.settings()):
            groups = htmlexport.infer_media_groups([self.path("messages.html")])
        self.assertEqual([row["msg_id"] for row in groups["448"]], list(map(str, range(448, 458))))
        self.assertEqual([row["msg_id"] for row in groups["458"]], list(map(str, range(458, 465))))
        self.assertEqual(groups["458"][0]["body_text"], "Paranoid schizophrenia\nYou want more?")
        self.assertEqual(groups["458"][1]["body_text"], "Different caption on item 459")
        caption, _entities = album.caption_for_group(groups["458"])
        self.assertIn("Paranoid schizophrenia\nYou want more?", caption)
        self.assertIn("Forwarded from espritzeit", caption)

        client, output, _ = self.run_once()
        album_calls = client.kinds("send_file")
        self.assertEqual(len(album_calls), 2)
        first_files, first_kwargs = album_calls[0][1], album_calls[0][2]
        second_files, second_kwargs = album_calls[1][1], album_calls[1][2]
        self.assertEqual(first_files, expected_first)
        self.assertEqual(second_files, expected_second)
        first_caption, _first_entities = album.caption_for_group(groups["448"])
        second_caption, _second_entities = album.caption_for_group(groups["458"])
        self.assertEqual(first_kwargs["caption"], [""] * 9 + [first_caption])
        self.assertEqual(second_kwargs["caption"], [""] * 6 + [second_caption])
        self.assertIn("Paranoid schizophrenia\nYou want more?", second_kwargs["caption"][-1])
        self.assertIn("Different caption on item 459", second_kwargs["caption"][-1])
        self.assertIn("Forwarded from espritzeit", second_kwargs["caption"][-1])
        self.assertFalse(any("Paranoid schizophrenia" in text for text in client.texts()))
        grouped_ids = {message.grouped_id for message in client.messages.values()
                       if message.grouped_id is not None}
        self.assertEqual(len(grouped_ids), 2)
        self.assertIn("album 448–457", output)
        self.assertIn("album 458–464", output)
        self.assertIn("verified Telegram album", output)
        with config.use_settings(self.settings()):
            self.assertEqual(set(progress.load_id_map()), {str(i) for i in range(447, 465)})

    def test_forwarded_album_with_real_source_links_is_grouped_not_native_forwarded_item_by_item(self):
        records = []
        expected = []
        for source_id, clock in ((801, "09:00:00"), (802, "09:00:01")):
            relative = f"video_files/linked-{source_id}.mp4"
            fakes.write_file(self.folder.name, relative)
            expected.append(os.path.join(self.folder.name, relative))
            joined = " joined" if source_id == 802 else ""
            records.append(
                f'<div class="message default clearfix{joined}" id="message{source_id}">'
                '<div class="body">'
                '<div class="pull_right date details" title="12.03.2023 10:01:00 UTC+02:00">10:01</div>'
                '<div class="from_name">Alice</div>'
                '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
                '<div class="forwarded body">'
                f'<div class="from_name"><a href="https://t.me/NewsChannel/{source_id}">News Channel</a>'
                f'<span class="date details" title="12.03.2023 {clock} UTC+02:00">{clock}</span></div>'
                '<div class="media_wrap clearfix">'
                f'<a class="video_file_wrap" href="{relative}"></a>'
                '</div>'
                + ('<div class="text">caption 801</div>' if source_id == 801 else '')
                + '</div></div></div>'
            )
        fakes.write_export(self.folder.name, records)

        with config.use_settings(self.settings()):
            groups = htmlexport.infer_media_groups([self.path("messages.html")])
        self.assertEqual([row["msg_id"] for row in groups["801"]], ["801", "802"])

        client, _, _ = self.run_once()
        calls = client.kinds("send_file")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], expected)
        self.assertEqual(calls[0][2]["caption"][0], "")
        self.assertIn("caption 801", calls[0][2]["caption"][1])
        self.assertIn("Forwarded from News Channel", calls[0][2]["caption"][1])
        self.assertFalse(any("caption 801" in text for text in client.texts()))


    def test_forwarded_round_video_keeps_attribution_in_same_media_message(self):
        media_path = "round_video_messages/forwarded-round-video.mp4"
        fakes.write_file(self.folder.name, media_path)
        forwarded = (
            '<div class="message default clearfix" id="message154">'
            '<div class="body">'
            '<div class="pull_right date details" '
            'title="08.11.2026 09:28:59 UTC+02:00">09:28</div>'
            '<div class="from_name">jbvjhvjhvjh</div>'
            '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
            '<div class="forwarded body">'
            '<div class="from_name">HLI8IOS '
            '<span class="date details" title="05.11.2026 19:35:11 UTC+02:00">'
            '05.11.2026 19:35:11</span></div>'
            '<div class="media_wrap clearfix">'
            f'<a class="video_file_wrap clearfix pull_left" href="{media_path}"></a>'
            '</div>'
            '<div class="text">forwarded round-video caption</div>'
            '</div></div></div>'
        )
        fakes.write_export(self.folder.name, [forwarded])

        client, _, _ = self.run_once()

        self.assertEqual(client.kinds("send_message"), [])
        media_calls = client.kinds("send_file")
        self.assertEqual(len(media_calls), 1)
        kwargs = media_calls[0][2]
        self.assertNotIn("video_note", kwargs)
        self.assertIn("Forwarded from HLI8IOS", kwargs["caption"])
        self.assertIn("forwarded round-video caption", kwargs["caption"])

    def test_inferred_run_longer_than_ten_is_split_into_ordered_albums(self):
        records = [fakes.message(1, fakes.text("sender"), sender="Alice", minute=1)]
        for source_id in range(2, 14):
            relative = f"files/document-{source_id}.png"
            fakes.write_file(self.folder.name, relative)
            attachment = (
                '<div class="media_wrap clearfix">'
                f'<a class="media clearfix pull_left block_link media_file" href="{relative}">'
                '</a></div>'
            )
            records.append(
                fakes.message(source_id, attachment, sender="", classes="joined", minute=2)
            )
        fakes.write_export(self.folder.name, records)
        with config.use_settings(self.settings()):
            groups = htmlexport.infer_media_groups([self.path("messages.html")])
        self.assertEqual(list(groups), ["2", "12"])
        self.assertEqual([row["msg_id"] for row in groups["2"]],
                         [str(i) for i in range(2, 12)])
        self.assertEqual([row["msg_id"] for row in groups["12"]], ["12", "13"])


    def test_forwarded_static_and_video_stickers_keep_attribution_in_media_caption(self):
        # Exercise HTML reconstruction through the actual send_file call for
        # both sticker formats that previously emitted a separate text message.
        for extension in (".webp", ".tgs", ".webm"):
            with self.subTest(extension=extension):
                self.folder.cleanup()
                self.folder = tempfile.TemporaryDirectory()
                self.addCleanup(self.folder.cleanup)
                relative = "stickers/forwarded" + extension
                fakes.write_file(self.folder.name, relative)
                forwarded = (
                    '<div class="message default clearfix" id="message701">'
                    '<div class="body">'
                    '<div class="pull_right date details" '
                    'title="08.11.2026 09:28:59 UTC+02:00">09:28</div>'
                    '<div class="from_name">Archive sender</div>'
                    '<div class="pull_left forwarded userpic_wrap"><div class="userpic"></div></div>'
                    '<div class="forwarded body">'
                    '<div class="from_name">Original sender '
                    '<span class="date details" '
                    'title="05.11.2026 19:35:11 UTC+02:00">05.11.2026 19:35:11</span>'
                    '</div>'
                    '<div class="sticker_wrap">'
                    f'<a href="{relative}"></a>'
                    '</div>'
                    '</div></div></div>'
                )
                fakes.write_export(self.folder.name, [forwarded])
                client, _, _ = self.run_once()

                self.assertEqual(client.kinds("send_message"), [])
                calls = client.kinds("send_file")
                self.assertEqual(len(calls), 1)
                sent_path, kwargs = calls[0][1], calls[0][2]
                self.assertEqual(sent_path, os.path.join(self.folder.name, relative))
                self.assertIn("Forwarded from Original sender", kwargs["caption"])
                self.assertIn("5 November 2026 · 19:35:11", kwargs["caption"])
                self.assertIsNone(kwargs["reply_to"])
                self.assertTrue(kwargs["force_document"])
                self.assertTrue(any(
                    isinstance(attribute, tl.DocumentAttributeSticker)
                    for attribute in kwargs["attributes"]
                ))
                with config.use_settings(self.settings()):
                    self.assertEqual(progress.load_id_map(), {"701": 1001})

    def test_multiple_media_with_sticker_first_attaches_context_to_final_media(self):
        async def scenario():
            calls = []

            async def send_file(channel, path, **kwargs):
                calls.append((path, kwargs))
                return type("Sent", (), {"id": 800 + len(calls)})()

            async def send_message(channel, text, **kwargs):
                raise AssertionError("caption must not be emitted as a separate text message")

            client = type("Client", (), {})()
            client.send_file = send_file
            client.send_message = send_message
            state = {"src": "702"}
            media = [
                ("stickers/first.webp", "sticker_static"),
                ("photos/second.jpg", "photo"),
            ]
            with patch.object(progress, "save_state"):
                result = await sending._send_multiple_media(
                    client, "channel",
                    [("Forwarded from Original sender\\n5 November 2026", [])],
                    media, None, state, "702",
                )
            self.assertEqual(result, 801)
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][1]["caption"], "")
            self.assertEqual(calls[1][1]["caption"],
                             "Forwarded from Original sender\\n5 November 2026")
            self.assertTrue(any(
                isinstance(attribute, tl.DocumentAttributeSticker)
                for attribute in calls[0][1]["attributes"]
            ))
        asyncio.run(scenario())


    def test_single_html_record_keeps_mixed_photo_video_media_together_in_source_order(self):
        # Each source message contains multiple media anchors inside one
        # media_wrap. This is distinct from inferring an album across adjacent
        # HTML message records: each record must become its own native album.
        records = []
        expected_groups = [
            [
                ("photos/first.png", "photo"),
                ("video_files/second.mov", "video"),
            ],
            [
                ("video_files/third.mp4", "video"),
                ("photos/fourth.jpg", "photo"),
            ],
        ]
        for index, group in enumerate(expected_groups, start=1):
            anchors = []
            for relative, kind in group:
                fakes.write_file(self.folder.name, relative)
                if kind == "photo":
                    anchors.append(
                        f'<a class="photo_wrap clearfix pull_left" href="{relative}">'
                        f'<img class="photo" src="{relative}"></a>'
                    )
                else:
                    anchors.append(
                        f'<a class="video_file_wrap clearfix pull_left" href="{relative}">'
                        '<div class="video_play_bg"></div></a>'
                    )
            inner = (
                '<div class="media_wrap clearfix">'
                + "".join(anchors)
                + '</div>'
                + fakes.text(f"mixed media message {index}")
            )
            records.append(fakes.message(880 + index, inner, sender="Alice", minute=10 + index))

        fakes.write_export(self.folder.name, records)
        client, output, _ = self.run_once()

        album_calls = client.kinds("send_file")
        self.assertEqual(len(album_calls), 2)
        for call, expected in zip(album_calls, expected_groups):
            files, kwargs = call[1], call[2]
            self.assertEqual(
                files,
                [os.path.join(self.folder.name, relative) for relative, _kind in expected],
            )
            self.assertFalse(kwargs.get("force_document", False))
            self.assertEqual(len(kwargs["caption"]), 2)
            self.assertEqual(kwargs["caption"][0], "")
            self.assertTrue(kwargs["caption"][1].startswith("Alice\n12 March 2023"))

        self.assertEqual(client.kinds("send_message"), [])
        self.assertIn("sent=2", output)
        self.assertIn("failed=0", output)
        with config.use_settings(self.settings()):
            mapped = progress.load_id_map()
        self.assertEqual(set(mapped), {"881", "882"})


if __name__ == "__main__":
    unittest.main()
