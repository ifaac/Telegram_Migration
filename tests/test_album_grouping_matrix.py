"""Source album grouping regression matrix.

These tests use synthetic records and only group media types Telegram can
represent as native albums. Standalone media must not be inferred as albums.
"""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from telegram_migration import album, config, exportmedia


class AlbumGroupingMatrixTests(unittest.TestCase):
    def _record(self, index, kind, *, first=False, base=None):
        base = base or datetime(2026, 1, 1, 12, 0, 0)
        return {
            "msg_id": str(1000 + index),
            "media": [(f"synthetic/{index}.{kind}", kind)],
            "media_note": "",
            "sender": "Archive owner",
            "_album_sender": "Original forwarded author",
            "is_forwarded": True,
            "forward_name": "Original forwarded author",
            "forward_timestamp": base,
            "timestamp": base + timedelta(seconds=index),
            "joined": not first,
            "reply_src_id": None,
            "forward_reply_src_id": "900",
            "body_text": "synthetic caption" if first else "",
        }

    def test_source_grouping_is_independent_of_media_kind_and_group_size(self):
        media_runs = (
            ("photo",),
            ("video",),
            ("document",),
            ("audio",),
            ("photo", "video"),
        )
        for count in (2, 3, 8, 10):
            for kinds in media_runs:
                with self.subTest(count=count, kinds=kinds):
                    records = [
                        self._record(i, kinds[i % len(kinds)], first=(i == 0))
                        for i in range(count)
                    ]
                    self.assertTrue(all(
                        album._album_candidate(record, record["sender"])
                        for record in records
                    ))
                    for left, right in zip(records, records[1:]):
                        self.assertTrue(album._album_records_compatible(left, right))
                    self.assertEqual(records[0]["body_text"], "synthetic caption")
                    self.assertTrue(all(not row["body_text"] for row in records[1:]))

    def test_non_album_media_types_cannot_start_or_continue_source_groups(self):
        for kind in ("gif", "voice", "round_video", "sticker_static",
                     "sticker_animated", "sticker_video"):
            with self.subTest(kind=kind):
                record = self._record(0, kind, first=True)
                self.assertFalse(album._album_candidate(record, record["sender"]))

    def test_repeated_forwarded_reply_target_is_shared_group_context(self):
        first = self._record(0, "photo", first=True)
        continuation = self._record(1, "photo")
        self.assertTrue(album._album_records_compatible(first, continuation))

        continuation["forward_reply_src_id"] = "901"
        self.assertFalse(album._album_records_compatible(first, continuation))

    def test_forwarded_album_items_need_original_timestamps_individually(self):
        first = self._record(0, "photo", first=True)
        continuation = self._record(1, "photo")

        # The records otherwise match: same inherited author, forwarded
        # status, joined marker, and close export timestamps. A joined class
        # must not make up for missing original-source timestamp metadata.
        continuation["forward_timestamp"] = None
        self.assertFalse(album._album_records_compatible(first, continuation))

        first["forward_timestamp"] = None
        self.assertFalse(album._album_records_compatible(first, continuation))

    def test_native_album_compatibility_is_a_separate_destination_decision(self):
        self.assertTrue(exportmedia.album_compatible([
            ("a.jpg", "photo"), ("b.mp4", "video"),
        ]))
        self.assertTrue(exportmedia.album_compatible([
            ("a.pdf", "document"), ("b.pdf", "document"),
        ]))
        self.assertTrue(exportmedia.album_compatible([
            ("a.mp3", "audio"), ("b.ogg", "audio"),
        ]))
        for media in (
            [("a.gif", "gif"), ("b.jpg", "photo")],
            [("a.ogg", "voice"), ("b.jpg", "photo")],
            [("a.webm", "round_video"), ("b.mp4", "video")],
            [("a.webp", "sticker_static"), ("b.jpg", "photo")],
            [("a.mp3", "audio"), ("b.pdf", "document")],
        ):
            self.assertFalse(exportmedia.album_compatible(media))


if __name__ == "__main__":
    unittest.main()


class TelegramDesktopHtmlGroupingTests(unittest.TestCase):
    @staticmethod
    def _message_html(message_id, outer_second, forward_second, path, kind, *, joined=False):
        outer = f"01.07.2026 21:46:{outer_second:02d} UTC+02:00"
        forwarded = f"04.06.2026 09:46:{forward_second:02d} UTC+02:00"
        classes = "message default clearfix" + (" joined" if joined else "")
        if kind == "document":
            anchor = (
                f'<a class="media clearfix pull_left block_link media_file" href="{path}">'
                '<div class="fill pull_left"></div><div class="body">'
                '<div class="title bold">attachment.jpg</div>'
                '<div class="status details">100 KB</div></div></a>'
            )
        elif kind == "round_video":
            anchor = (
                f'<a class="media clearfix pull_left block_link media_video" href="{path}">'
                '<img class="thumb pull_left" src="round_video_thumb.jpg"/>'
                '<div class="body"><div class="title bold">Video message</div>'
                '<div class="status details">00:05</div></div></a>'
            )
        elif kind == "video":
            anchor = (
                f'<a class="video_file_wrap clearfix pull_left" href="{path}">'
                '<div class="video_play_bg"><div class="video_play"></div></div>'
                '<div class="video_duration">00:05</div>'
                '<img class="video_file" src="ordinary_video_thumb.jpg"/></a>'
            )
        elif kind == "photo":
            anchor = (
                f'<a class="photo_wrap clearfix pull_left" href="{path}">'
                '<img class="photo" src="photo_thumb.jpg"/></a>'
            )
        else:
            raise AssertionError(f"Unsupported test media kind: {kind}")
        return (
            f'<div class="{classes}" id="message{message_id}"><div class="body">'
            f'<div class="pull_right date details" title="{outer}">21:46</div>'
            '<div class="forwarded body"><div class="from_name">Original author '
            f'<span class="date details" title="{forwarded}">04.06.2026</span></div>'
            f'<div class="media_wrap clearfix">{anchor}</div></div></div></div>'
        )

    def _infer(self, messages):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for relative, _kind in messages:
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"synthetic media")
            html_path = root / "messages.html"
            chunks = []
            for index, (relative, kind) in enumerate(messages):
                message_id = 319 + index
                chunks.append(self._message_html(
                    message_id, index, index, relative, kind,
                    joined=index > 0,
                ))
            html_path.write_text(
                "<html><body>" + "".join(chunks) + "</body></html>",
                encoding="utf-8",
            )
            with patch.object(config.get_settings(), "base_dir", temp):
                return album.infer_media_groups([str(html_path)])

    def test_eleven_consecutive_file_attachments_keep_first_ten_in_one_group(self):
        messages = [("files/preceding.jpg", "document")]
        messages.extend(
            (f"files/group_{index}.jpg", "document")
            for index in range(10)
        )
        groups = self._infer(messages)
        self.assertEqual(list(groups), ["319"])
        self.assertEqual(
            [item["msg_id"] for item in groups["319"]],
            [str(value) for value in range(319, 329)],
        )
        # The eleventh record is the one-item remainder, not a prefix
        # discarded from the first album.


    def test_joined_round_video_notes_remain_independent_posts(self):
        groups = self._infer([
            ("round_video_messages/one.mp4", "round_video"),
            ("round_video_messages/two.mp4", "round_video"),
        ])
        self.assertEqual(groups, {})

    def test_ordinary_photo_video_album_is_still_inferred(self):
        groups = self._infer([
            ("photos/one.jpg", "photo"),
            ("video_files/two.mp4", "video"),
        ])
        self.assertEqual(list(groups), ["319"])
        self.assertEqual(
            [item["msg_id"] for item in groups["319"]],
            ["319", "320"],
        )
