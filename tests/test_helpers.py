"""Offline regression tests for the telegram_migration package.

These tests never sign in to Telegram and never call the live API. They use
real Telethon entity objects when dependencies are installed by requirements.txt.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lxml import etree
from telethon.tl import types as tl

from telegram_migration import (
    bigfiles, config, customemoji, exportmedia, formatfallback, htmlexport, mediatools, progress,
    reaction_archive, richtext, sending, textutil, uploads,
)


class TextAndEntityTests(unittest.TestCase):
    def test_utf16_length_counts_non_bmp_as_two_units(self):
        self.assertEqual(textutil.u16len("A😀B"), 4)

    def test_sanitize_text_normalizes_newlines_and_removes_controls(self):
        self.assertEqual(textutil.sanitize_text("a\r\nb\rc\x00\x07d"), "a\nb\ncd")

    def test_rich_text_preserves_italics_bold_link_and_line_break(self):
        root = etree.fromstring(
            '<div>plain <i>italic</i> and <b>bold</b> '
            '<a href="https://example.test/path">site</a><br/>end</div>'
        )
        text, entities = richtext.parse_rich_text(root)
        self.assertEqual(text, "plain italic and bold site\nend")
        by_type = {type(entity).__name__: entity for entity in entities}
        self.assertIn("MessageEntityItalic", by_type)
        self.assertIn("MessageEntityBold", by_type)
        self.assertIn("MessageEntityTextUrl", by_type)
        italic = by_type["MessageEntityItalic"]
        bold = by_type["MessageEntityBold"]
        link = by_type["MessageEntityTextUrl"]
        self.assertEqual((italic.offset, italic.length), (6, 6))
        self.assertEqual((bold.offset, bold.length), (17, 4))
        self.assertEqual(link.url, "https://example.test/path")
        self.assertEqual(text[link.offset:link.offset + link.length], "site")

    def test_rich_text_measures_offsets_in_utf16_units(self):
        root = etree.fromstring("<div>😀<i>x</i></div>")
        text, entities = richtext.parse_rich_text(root)
        self.assertEqual(text, "😀x")
        italic = next(e for e in entities if isinstance(e, tl.MessageEntityItalic))
        self.assertEqual((italic.offset, italic.length), (2, 1))

    def test_rich_text_handles_nested_bold_and_italic(self):
        root = etree.fromstring("<div><b>bold <i>both</i></b></div>")
        text, entities = richtext.parse_rich_text(root)
        self.assertEqual(text, "bold both")
        bold = next(e for e in entities if isinstance(e, tl.MessageEntityBold))
        italic = next(e for e in entities if isinstance(e, tl.MessageEntityItalic))
        self.assertEqual((bold.offset, bold.length), (0, 9))
        self.assertEqual((italic.offset, italic.length), (5, 4))

    def test_strip_with_entities_rebases_offsets_using_utf16(self):
        entity = tl.MessageEntityItalic(offset=1, length=3)
        text, entities = textutil.strip_with_entities(" 😀x ", [entity])
        self.assertEqual(text, "😀x")
        self.assertEqual((entities[0].offset, entities[0].length), (0, 3))

    def test_clean_invalid_entities_drops_invalid_and_clips_overrun(self):
        good = tl.MessageEntityBold(offset=0, length=2)
        overrun = tl.MessageEntityItalic(offset=1, length=8)
        negative = tl.MessageEntityUnderline(offset=-1, length=1)
        empty = tl.MessageEntityStrike(offset=1, length=0)
        outside = tl.MessageEntityCode(offset=8, length=1)
        result = textutil.clean_invalid_entities("abc", [good, overrun, negative, empty, outside])
        self.assertEqual([type(item).__name__ for item in result], [type(good).__name__, type(overrun).__name__])
        self.assertEqual((result[0].offset, result[0].length), (0, 2))
        self.assertEqual((result[1].offset, result[1].length), (1, 2))

    def test_archive_header_formats_sender_and_timestamp_with_utf16_entities(self):
        timestamp = datetime(2025, 3, 15, 11, 39)
        header, entities = textutil.compose_message_header(timestamp, "esprít 😀")
        self.assertEqual(header, "esprít 😀\n15 March 2025 · 11:39\n\n")
        self.assertEqual(
            [(type(entity).__name__, entity.offset, entity.length) for entity in entities],
            [("MessageEntityBold", 0, 9), ("MessageEntityItalic", 10, 21)],
        )

    def test_archive_header_supports_date_only_service_events(self):
        header, entities = textutil.compose_message_header(
            datetime(2025, 3, 15), "Telegram service event", date_only=True
        )
        self.assertEqual(header, "Telegram service event\n15 March 2025\n\n")
        self.assertEqual([type(entity).__name__ for entity in entities],
                         ["MessageEntityBold", "MessageEntityItalic"])

    def test_compose_message_shifts_body_entity_after_prefix(self):
        body_entity = tl.MessageEntityItalic(offset=0, length=4)
        text, entities = textutil.compose_message(
            ("[date]\n", []), ("Alice:\n", []), ("", []), "word", [body_entity]
        )
        self.assertEqual(text, "[date]\nAlice:\nword")
        self.assertEqual((entities[0].offset, entities[0].length), (14, 4))

    def test_split_long_text_respects_limit_and_does_not_split_surrogate_pair(self):
        text = "abcdefgh😀ijklmnopqrs"
        chunks = textutil.split_with_entities(text, [], first_limit=9, limit=9)
        pieces = [chunk for chunk, _entities in chunks]
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), text)
        self.assertTrue(all(textutil.u16len(piece) <= 9 for piece in pieces))
        self.assertTrue(all("\ud800" not in piece and "\udc00" not in piece for piece in pieces))

    def test_split_clips_entity_offsets_to_each_chunk(self):
        text = "abcdefghijABCDEFGHIJ"
        entity = tl.MessageEntityItalic(offset=2, length=16)
        chunks = textutil.split_with_entities(text, [entity], first_limit=10, limit=10)
        self.assertGreaterEqual(len(chunks), 2)
        for piece, entities in chunks:
            for current in entities:
                self.assertGreater(current.length, 0)
                self.assertGreaterEqual(current.offset, 0)
                self.assertLessEqual(current.offset + current.length, textutil.u16len(piece))

    def test_split_preserves_spaces_at_boundaries(self):
        """Concatenating the chunks must reproduce the input exactly, including
        whitespace at chunk boundaries. Previously nxt=i+1 skipped the space."""
        text = "abc def ghi jkl mno"
        chunks = textutil.split_with_entities(text, [], first_limit=8, limit=8)
        pieces = [chunk for chunk, _entities in chunks]
        self.assertGreater(len(pieces), 1)
        self.assertEqual("".join(pieces), text)
        self.assertTrue(all(textutil.u16len(piece) <= 8 for piece in pieces))

    def test_split_advances_past_a_surrogate_pair_when_limit_is_one(self):
        """A limit of 1 with a surrogate pair at the boundary used to loop
        forever because nxt stayed equal to start. The forward-progress guard
        takes the whole pair (2 units) instead of hanging."""
        chunks = textutil.split_with_entities("😀x", [], first_limit=1, limit=1)
        pieces = [chunk for chunk, _entities in chunks]
        self.assertEqual("".join(pieces), "😀x")
        self.assertTrue(any(piece == "😀" for piece in pieces))

    def test_split_rejects_a_limit_below_one(self):
        with self.assertRaises(ValueError):
            textutil.split_with_entities("abc", [], first_limit=0, limit=1)

    def test_entity_error_detection_does_not_match_generic_word_entities(self):
        self.assertTrue(formatfallback._is_entity_error(ValueError("invalid bounds for formatting")))
        self.assertFalse(formatfallback._is_entity_error(RuntimeError("unrelated entities database failure")))

    def test_parse_export_date_accepts_desktop_timezone_suffix(self):
        self.assertEqual(
            htmlexport.parse_export_date("24.10.2026 12:34:56 UTC+03:00"),
            datetime(2026, 10, 24, 12, 34, 56),
        )

    def test_parse_export_date_returns_none_for_invalid_input(self):
        self.assertIsNone(htmlexport.parse_export_date("not a date"))


    def test_split_keeps_a_custom_emoji_entity_whole(self):
        fallback = "👩‍💻"
        text = "abc" + fallback + "xyz"
        entity = tl.MessageEntityCustomEmoji(
            offset=textutil.u16len("abc"),
            length=textutil.u16len(fallback),
            document_id=12345,
        )
        chunks = textutil.split_with_entities(text, [entity], first_limit=5, limit=5)
        self.assertEqual("".join(piece for piece, _entities in chunks), text)
        found = [
            (piece, item)
            for piece, entities in chunks
            for item in entities
            if isinstance(item, tl.MessageEntityCustomEmoji)
        ]
        self.assertEqual(len(found), 1)
        piece, item = found[0]
        self.assertEqual(
            piece.encode("utf-16-le")[2 * item.offset:2 * (item.offset + item.length)].decode("utf-16-le"),
            fallback,
        )

    def test_custom_emoji_longer_than_chunk_limit_is_kept_intact(self):
        fallback = "👩‍💻"
        entity = tl.MessageEntityCustomEmoji(
            offset=0, length=textutil.u16len(fallback), document_id=12345
        )
        chunks = textutil.split_with_entities(fallback + "x", [entity], first_limit=4, limit=4)
        self.assertEqual(chunks[0][0], fallback)
        self.assertEqual(textutil.u16len(chunks[0][0]), 5)
        self.assertEqual(len(chunks[0][1]), 1)


class InlineCustomEmojiTests(unittest.TestCase):
    def test_rich_text_keeps_fallback_and_extracts_custom_emoji_offset(self):
        with tempfile.TemporaryDirectory() as temp:
            sticker = Path(temp) / "stickers" / "flower.webp"
            sticker.parent.mkdir()
            sticker.write_bytes(b"static sticker bytes")
            root = etree.fromstring(
                '<div class="text">before '
                '<a href="stickers/flower.webp">🌸</a> after</div>'
            )
            with patch.object(config.get_settings(), "base_dir", temp):
                text, entities, stickers, missing = richtext.parse_rich_text_with_stickers(root)

            self.assertEqual(text, "before 🌸 after")
            self.assertEqual(entities, [])
            self.assertEqual(missing, [])
            self.assertEqual(len(stickers), 1)
            self.assertEqual(stickers[0]["alt"], "🌸")
            self.assertEqual(stickers[0]["offset"], textutil.u16len("before "))
            self.assertEqual(stickers[0]["length"], textutil.u16len("🌸"))

    def test_missing_inline_sticker_keeps_fallback_and_reports_reference(self):
        root = etree.fromstring(
            '<div class="text">before <a href="stickers/missing.tgs">🌸</a> after</div>'
        )
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(config.get_settings(), "base_dir", temp):
                text, _entities, stickers, missing = richtext.parse_rich_text_with_stickers(root)
        self.assertEqual(text, "before 🌸 after")
        self.assertEqual(stickers, [])
        self.assertEqual(missing, ["stickers/missing.tgs"])

    def test_custom_emoji_entity_uses_utf16_offset_and_original_document_id(self):
        with tempfile.TemporaryDirectory() as temp:
            sticker = Path(temp) / "stickers" / "developer.webp"
            sticker.parent.mkdir()
            sticker.write_bytes(b"static sticker bytes")
            record = {
                "path": str(sticker),
                "kind": "sticker_static",
                "alt": "👩‍💻",
                "offset": textutil.u16len("A "),
                "length": textutil.u16len("👩‍💻"),
            }
            data = {
                "body_text": "A 👩‍💻 B",
                "body_entities": [],
                "inline_stickers": [record],
            }
            key = customemoji.reference_key(record["path"], record["kind"], record["alt"])
            added = customemoji.apply_custom_emoji_entities(data, {key: 987654321})
            self.assertEqual(added, 1)
            entity = data["body_entities"][0]
            self.assertIsInstance(entity, tl.MessageEntityCustomEmoji)
            self.assertEqual((entity.offset, entity.length, entity.document_id),
                             (2, 5, 987654321))

    def test_bot_auth_skips_custom_emoji_sticker_set_creation(self):
        class BotClient:
            async def get_me(self):
                return SimpleNamespace(username="migration_bot", bot=True)

            async def get_input_entity(self, peer):
                raise AssertionError("bot authentication must not attempt sticker-set uploads")

            async def upload_file(self, *args, **kwargs):
                raise AssertionError("bot authentication must not upload custom emojis")

            async def __call__(self, request):
                raise AssertionError("bot authentication must not call sticker-set methods")

        with tempfile.TemporaryDirectory() as temp:
            sticker = Path(temp) / "stickers" / "flower.webp"
            sticker.parent.mkdir()
            sticker.write_bytes(b"static sticker bytes")
            page = Path(temp) / "messages.html"
            page.write_text(
                '<html><body><div class="message" id="message1">'
                '<div class="text"><a href="stickers/flower.webp">🌸</a></div>'
                '</div></body></html>',
                encoding="utf-8",
            )
            with patch.object(config.get_settings(), "base_dir", temp):
                mapping = asyncio.run(
                    customemoji.prepare_inline_custom_emojis(
                        BotClient(), [str(page)],
                        peer=tl.InputPeerChannel(channel_id=123, access_hash=456),
                    )
                )
            self.assertEqual(mapping, {})

    def test_oversize_inline_custom_emoji_is_left_as_fallback_text(self):
        class NoUploadClient:
            async def get_me(self):
                raise AssertionError("must not contact Telegram for an oversized asset")

            async def upload_file(self, *args, **kwargs):
                raise AssertionError("oversized inline emoji must not be uploaded")

        with tempfile.TemporaryDirectory() as temp:
            sticker = Path(temp) / "stickers" / "large.webp"
            sticker.parent.mkdir()
            sticker.write_bytes(b"sticker payload larger than the test limit")
            page = Path(temp) / "messages.html"
            page.write_text(
                '<html><body><div class="message" id="message1">'
                '<div class="text"><a href="stickers/large.webp">🌸</a></div>'
                '</div></body></html>',
                encoding="utf-8",
            )
            with config.use_settings(config.get_settings().replace(base_dir=temp, max_upload_mb=0.000001)):
                mapping = asyncio.run(
                    customemoji.prepare_inline_custom_emojis(
                        NoUploadClient(), [str(page)],
                        peer=tl.InputPeerChannel(channel_id=123, access_hash=456),
                    )
                )
            self.assertEqual(mapping, {})

    def test_video_custom_emoji_uses_video_sticker_attributes(self):
        asset = {
            "path": "/tmp/custom-emoji.webm",
            "kind": "sticker_video",
            "alt": "🫧",
            "filename": "custom-emoji.webm",
        }
        with patch.object(
            customemoji.mediatools, "probe_video_metadata",
            return_value={"width": 256, "height": 256, "duration": 2.5},
        ):
            mime_type, attributes, nosound = customemoji._mime_and_attributes(asset)

        self.assertEqual(mime_type, "video/webm")
        self.assertTrue(nosound)
        self.assertTrue(any(isinstance(a, tl.DocumentAttributeCustomEmoji) for a in attributes))
        video = next(a for a in attributes if isinstance(a, tl.DocumentAttributeVideo))
        self.assertEqual((video.w, video.h, video.duration), (256, 256, 2.5))
        self.assertFalse(video.supports_streaming)
        self.assertTrue(video.nosound)

    def test_static_png_custom_emoji_has_an_image_mime_type(self):
        asset = {
            "path": "/tmp/custom-emoji.png",
            "kind": "sticker_static",
            "alt": "🌼",
            "filename": "custom-emoji.png",
        }
        mime_type, attributes, nosound = customemoji._mime_and_attributes(asset)
        self.assertEqual(mime_type, "image/png")
        self.assertFalse(nosound)
        self.assertTrue(any(isinstance(a, tl.DocumentAttributeCustomEmoji) for a in attributes))

    def test_inline_asset_collection_deduplicates_references_and_prepares_reusable_pack(self):
        class FakeStickerClient:
            def __init__(self):
                self.username = "archive_migration_bot"
                self.next_document_id = 7000
                self.documents = {}
                self.pack = None
                self.uploads = []
                self.upload_peers = []
                self.creates = 0
                self.adds = 0

            async def get_me(self):
                return SimpleNamespace(username=self.username)

            async def get_input_entity(self, peer):
                self.resolved_peer = peer
                return peer

            async def upload_file(self, path, *, file_name=None):
                self.uploads.append((path, file_name))
                return SimpleNamespace(path=path, file_name=file_name)

            async def __call__(self, request):
                name = type(request).__name__
                if name == "GetStickerSetRequest":
                    if self.pack is None:
                        raise RuntimeError("STICKERSET_INVALID")
                    return self.pack
                if name == "UploadMediaRequest":
                    self.upload_peers.append(request.peer)
                    uploaded = request.media.file
                    self.next_document_id += 1
                    document = SimpleNamespace(
                        id=self.next_document_id,
                        access_hash=self.next_document_id * 10,
                        file_reference=b"file-reference",
                        attributes=request.media.attributes,
                    )
                    self.documents[uploaded.file_name] = document
                    return SimpleNamespace(document=document)
                if name == "CreateStickerSetRequest":
                    self.creates += 1
                    self.pack_name = request.short_name
                    first = request.stickers[0].document
                    document = next(
                        item for item in self.documents.values() if item.id == first.id
                    )
                    self.pack = SimpleNamespace(documents=[document])
                    return self.pack
                if name == "AddStickerToSetRequest":
                    self.adds += 1
                    added = request.sticker.document
                    document = next(
                        item for item in self.documents.values() if item.id == added.id
                    )
                    self.pack.documents.append(document)
                    return self.pack
                raise AssertionError(f"Unexpected MTProto request: {name}")

        with tempfile.TemporaryDirectory() as temp:
            webp = Path(temp) / "stickers" / "flower.webp"
            tgs = Path(temp) / "stickers" / "star.tgs"
            webp.parent.mkdir()
            webp.write_bytes(b"WEBP sticker")
            tgs.write_bytes(b"TGS sticker")
            html = (
                '<div class="message default clearfix" id="message1"><div class="body">'
                '<div class="text">A <a href="stickers/flower.webp">🌸</a> '
                'B <a href="stickers/star.tgs">⭐</a> '
                'C <a href="stickers/flower.webp">🌸</a></div>'
                '</div></div>'
            )
            page = Path(temp) / "messages.html"
            page.write_text("<html><body>" + html + "</body></html>", encoding="utf-8")
            client = FakeStickerClient()
            upload_peer = tl.InputPeerChannel(channel_id=123, access_hash=456)

            with patch.object(config.get_settings(), "base_dir", temp):
                assets, references = customemoji.collect_inline_custom_emojis([str(page)])
                self.assertEqual((references, len(assets)), (3, 2))
                mapping = asyncio.run(customemoji.prepare_inline_custom_emojis(client, [str(page)], peer=upload_peer))
                self.assertEqual(len(mapping), 2)
                first_id = mapping[customemoji.reference_key(
                    str(webp), "sticker_static", "🌸"
                )]
                second_id = mapping[customemoji.reference_key(
                    str(tgs), "sticker_animated", "⭐"
                )]
                self.assertNotEqual(first_id, second_id)
                self.assertEqual((client.creates, client.adds, len(client.uploads)), (1, 1, 2))
                self.assertEqual(client.upload_peers, [upload_peer, upload_peer])

                # A second run finds the same bot-owned pack and reuses document IDs.
                again = asyncio.run(customemoji.prepare_inline_custom_emojis(client, [str(page)], peer=upload_peer))
                self.assertEqual(again, mapping)
                self.assertEqual((client.creates, client.adds, len(client.uploads)), (1, 1, 2))


class ExportParsingTests(unittest.TestCase):
    def test_extract_message_data_reads_sender_time_text_and_reply(self):
        html = '''<div class="message default clearfix" id="message123">
          <div class="body">
            <div class="pull_right date details" title="24.10.2026 12:34:56 UTC+03:00">12:34</div>
            <div class="from_name">Alice</div>
            <div class="reply_to details"><a href="#go_to_message122">this message</a></div>
            <div class="text">hello <i>world</i></div>
          </div>
        </div>'''
        elem = etree.fromstring(html)
        with patch.object(config.get_settings(), "base_dir", tempfile.gettempdir()):
            data = htmlexport.extract_message_data(elem)
        self.assertEqual(data["msg_id"], "123")
        self.assertEqual(data["sender"], "Alice")
        self.assertEqual(data["timestamp"], datetime(2026, 10, 24, 12, 34, 56))
        self.assertEqual(data["body_text"], "hello world")
        self.assertEqual(data["reply_src_id"], "122")
        self.assertTrue(any(isinstance(e, tl.MessageEntityItalic) for e in data["body_entities"]))

    def test_extract_message_data_preserves_text_when_referenced_photo_is_missing(self):
        html = '''<div class="message default clearfix" id="message124">
          <div class="body">
            <div class="pull_right date details" title="24.10.2026 12:34:56 UTC+03:00">12:34</div>
            <div class="from_name">Alice</div>
            <div class="text">caption survives</div>
            <div class="media_wrap clearfix">
              <a class="photo_wrap" href="photos/missing.jpg">
                <img class="photo" src="photos/missing_thumb.jpg"/>
              </a>
            </div>
          </div>
        </div>'''
        elem = etree.fromstring(html)
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(config.get_settings(), "base_dir", temp):
                data = htmlexport.extract_message_data(elem)
        self.assertEqual(data["media"], [])
        self.assertEqual(
            data["body_text"],
            "caption survives\n\n[Media not found: photos/missing.jpg]",
        )

    def test_missing_photo_without_caption_becomes_a_text_message(self):
        html = '''<div class="message default clearfix" id="message125">
          <div class="body">
            <div class="from_name">Alice</div>
            <div class="media_wrap clearfix">
              <a class="photo_wrap" href="photos/only-photo.jpg">
                <img class="photo" src="photos/only-photo_thumb.jpg"/>
              </a>
            </div>
          </div>
        </div>'''
        elem = etree.fromstring(html)
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(config.get_settings(), "base_dir", temp):
                data = htmlexport.extract_message_data(elem)
        self.assertEqual(data["media"], [])
        self.assertEqual(data["body_text"], "[Media not found: photos/only-photo.jpg]")

    def test_missing_full_photo_does_not_fall_back_to_existing_thumbnail(self):
        with tempfile.TemporaryDirectory() as temp:
            photos = Path(temp) / "photos"
            photos.mkdir()
            (photos / "missing_thumb.jpg").write_bytes(b"thumbnail")
            elem = etree.fromstring(
                '<div class="message" id="message1"><div class="media_wrap">'
                '<a class="photo_wrap" href="photos/missing.jpg">'
                '<img class="photo" src="photos/missing_thumb.jpg"/></a>'
                '</div></div>'
            )
            with patch.object(config.get_settings(), "base_dir", temp):
                media = exportmedia.find_media(elem)
                missing = exportmedia.find_missing_media(elem)
            self.assertEqual(media, [])
            self.assertEqual(missing, ["photos/missing.jpg"])

    def test_message_with_existing_and_missing_attachments_keeps_both_outcomes(self):
        html = '''<div class="message default clearfix" id="message126">
          <div class="body">
            <div class="from_name">Alice</div>
            <div class="text">two attachments</div>
            <div class="media_wrap clearfix">
              <a class="photo_wrap" href="photos/available.jpg"><img src="photos/available_thumb.jpg"/></a>
              <a class="photo_wrap" href="photos/missing.jpg"><img src="photos/missing_thumb.jpg"/></a>
            </div>
          </div>
        </div>'''
        elem = etree.fromstring(html)
        with tempfile.TemporaryDirectory() as temp:
            photos = Path(temp) / "photos"
            photos.mkdir()
            (photos / "available.jpg").write_bytes(b"image")
            with patch.object(config.get_settings(), "base_dir", temp):
                data = htmlexport.extract_message_data(elem)
        self.assertEqual(len(data["media"]), 1)
        self.assertTrue(data["media"][0][0].endswith("available.jpg"))
        self.assertEqual(
            data["body_text"],
            "two attachments\n\n[Media not found: photos/missing.jpg]",
        )

    def test_extract_message_data_skips_service_records_but_service_parser_keeps_real_events(self):
        event = etree.fromstring(
            '<div class="message service" id="message123">'
            '<div class="body details">Alice invited Bob</div></div>'
        )
        with patch.object(config.get_settings(), "preserve_service_messages", True):
            data = htmlexport.extract_service_event_data(event, datetime(2026, 10, 24))
        self.assertIsNone(htmlexport.extract_message_data(event))
        self.assertEqual(data["msg_id"], "123")
        self.assertTrue(data["service_event"])
        self.assertEqual(data["body_text"], "Alice invited Bob")
        self.assertEqual(data["timestamp"], datetime(2026, 10, 24))
        self.assertTrue(data["timestamp_date_only"])

    def test_negative_service_message_id_is_a_date_separator_not_an_event(self):
        elem = etree.fromstring(
            '<div class="message service" id="message-123">'
            '<div class="body details">24 October 2026</div></div>'
        )
        self.assertEqual(htmlexport.extract_date_separator(elem), datetime(2026, 10, 24))
        self.assertIsNone(htmlexport.extract_service_event_data(elem, datetime(2026, 10, 24)))

    def test_extract_message_data_rejects_bad_message_id(self):
        elem = etree.fromstring('<div class="message default" id="messageabc"><div class="text">hi</div></div>')
        self.assertIsNone(htmlexport.extract_message_data(elem))

    def test_collect_source_ids_ignores_negative_date_dividers_but_keeps_real_service_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            page = Path(temp) / "messages.html"
            page.write_text(
                '<div class="message" id="message1"></div>'
                '<div class="message service" id="message-2"></div>'
                '<div class="message service" id=\'message30\'></div>', encoding="utf-8"
            )
            self.assertEqual(progress.collect_source_ids([str(page)]), {"1", "30"})

    def test_export_files_orders_split_html_pages_numerically(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ("messages10.html", "messages2.html", "messages.html", "not-messages.html"):
                (Path(temp) / name).write_text("", encoding="utf-8")
            with patch.object(config.get_settings(), "base_dir", temp):
                names = [Path(p).name for p in htmlexport.discover_export_files(temp)]
            self.assertEqual(names, ["messages.html", "messages2.html", "messages10.html"])


class MediaTests(unittest.TestCase):
    def test_media_classification_prefers_explicit_export_markup(self):
        # A generic media_file anchor means the source attachment was sent as
        # a file; a raster extension alone must not turn it into a photo.
        self.assertEqual(exportmedia._classify("files/photo.jpg", "media_wrap", "/tmp/photo.jpg", ("media_file",), ""), "document")
        self.assertEqual(exportmedia._classify("files/clip.mp4", "media_wrap", "/tmp/clip.mp4", ("media_file",), ""), "document")
        # Explicit photo/video/audio markup and export directories take priority.
        self.assertEqual(exportmedia._classify("files/photo.jpg", "media_wrap", "/tmp/photo.jpg", ("photo_wrap",), ""), "photo")
        self.assertEqual(exportmedia._classify("photos/photo.jpg", "media_wrap", "/tmp/photo.jpg", (), ""), "photo")
        self.assertEqual(exportmedia._classify("video_files/clip.mkv", "media_wrap", "/tmp/clip.mkv", (), ""), "video")
        self.assertEqual(exportmedia._classify("video_files/clip.webm", "media_wrap", "/tmp/clip.webm", (), ""), "video")
        self.assertEqual(exportmedia._classify("files/scan.png", "media_wrap", "/tmp/scan.png", (), ""), "photo")
        self.assertEqual(exportmedia._classify("files/report.pdf", "media_wrap", "/tmp/report.pdf", (), ""), "document")
        self.assertEqual(exportmedia._classify("image.png", "media_wrap", "/tmp/image.png", ("photo",), ""), "photo")

    def test_webm_marked_as_sticker_is_not_sent_as_ordinary_video(self):
        self.assertEqual(
            exportmedia._classify(
                "video_files/exported-sticker.webm", "media_wrap",
                "/tmp/exported-sticker.webm", ("media_photo",), "Sticker\n🧐",
            ),
            "sticker_video",
        )

    def test_audio_export_markup_is_not_classified_as_a_document(self):
        self.assertEqual(
            exportmedia._classify(
                "files/voice-track.mp3", "media_wrap",
                "/tmp/voice-track.mp3", ("media_audio_file",), "voice-track.mp3\n00:30",
            ),
            "audio",
        )

    def test_sticker_subtypes_are_classified_by_extension(self):
        self.assertEqual(exportmedia._classify("stickers/a.webp", "media_wrap", "/tmp/a.webp"), "sticker_static")
        self.assertEqual(exportmedia._classify("stickers/a.tgs", "media_wrap", "/tmp/a.tgs"), "sticker_animated")
        self.assertEqual(exportmedia._classify("stickers/a.webm", "media_wrap", "/tmp/a.webm"), "sticker_video")

    def test_gif_classification_requires_explicit_evidence(self):
        self.assertEqual(exportmedia._classify("video_files/a.gif", "media_wrap", "/tmp/a.gif"), "gif")
        self.assertEqual(exportmedia._classify("video_files/a.mp4", "media_wrap", "/tmp/a.mp4", (), ""), "video")
        self.assertEqual(exportmedia._classify("video_files/a.mp4", "media_wrap", "/tmp/a.mp4", (), "Animation, 00:04"), "gif")
        self.assertFalse(exportmedia._is_gif("/tmp/my GIF collection.mp4", (), "ordinary video"))

    def test_voice_and_round_video_classification(self):
        self.assertEqual(exportmedia._classify("voice_messages/a.ogg", "media_wrap", "/tmp/a.ogg"), "voice")
        self.assertEqual(
            exportmedia._classify("files/a.ogg", "media_wrap", "/tmp/a.ogg", ("media_voice_message",), ""),
            "voice",
        )
        self.assertEqual(exportmedia._classify("round_video_messages/a.mp4", "media_wrap", "/tmp/a.mp4"), "round_video")
        # media_video is a generic thumbnail class; a video note needs
        # its dedicated wrapper or round_video_messages export path.
        self.assertEqual(
            exportmedia._classify("files/a.mp4", "media_wrap", "/tmp/a.mp4", ("media_video",), ""),
            "video",
        )
        self.assertEqual(
            exportmedia._classify(
                "video_files/a.mp4", "media_wrap", "/tmp/a.mp4", ("media_video",), ""
            ),
            "video",
        )
        self.assertEqual(
            exportmedia._classify(
                "round_video_messages/a.mp4", "media_wrap", "/tmp/a.mp4", ("media_video",), ""
            ),
            "round_video",
        )
        self.assertEqual(
            exportmedia._classify("files/a.mp4", "media_wrap", "/tmp/a.mp4", ("video_file_wrap",), ""),
            "video",
        )

    def test_find_media_uses_export_root_and_finds_real_file(self):
        with tempfile.TemporaryDirectory() as temp:
            photos = Path(temp) / "photos"
            photos.mkdir()
            photo = photos / "photo.jpg"
            photo.write_bytes(b"fake image bytes")
            elem = etree.fromstring(
                '<div class="message" id="message1"><div class="media_wrap">'
                '<a href="photos/photo.jpg"><img class="photo" src="photos/photo_thumb.jpg"/></a>'
                '</div></div>'
            )
            with patch.object(config.get_settings(), "base_dir", temp):
                media = exportmedia.find_media(elem)
            self.assertEqual(len(media), 1)
            self.assertEqual(
                os.path.normcase(os.path.normpath(media[0][0])),
                os.path.normcase(os.path.normpath(str(photo))),
            )
            self.assertEqual(media[0][1], "photo")

    def test_find_media_accepts_file_names_written_raw_or_percent_encoded(self):
        with tempfile.TemporaryDirectory() as temp:
            files = Path(temp) / "files"
            files.mkdir()
            (files / "My File \u00e9.pdf").write_bytes(b"pdf bytes")
            for href in ("files/My File \u00e9.pdf", "files/My%20File%20%C3%A9.pdf"):
                elem = etree.fromstring(
                    '<div class="message" id="message1"><div class="media_wrap">'
                    f'<a class="media_file" href="{href}"><div class="title">x</div></a></div></div>'
                )
                with patch.object(config.get_settings(), "base_dir", temp):
                    media = exportmedia.find_media(elem)
                self.assertEqual([kind for _, kind in media], ["document"], href)

    def test_find_media_resolves_percent_encoded_image_src(self):
        """<img src> uses the same path helper as <a href>; verify the sticker
        fallback also unquotes. Stickers are the media type that relies
        entirely on <img src>."""
        with tempfile.TemporaryDirectory() as temp:
            sticker = Path(temp) / "stickers" / "My Sticker.webp"
            sticker.parent.mkdir()
            sticker.write_bytes(b"sticker bytes")
            elem = etree.fromstring(
                '<div class="message" id="message1"><div class="sticker_wrap">'
                '<img src="stickers/My%20Sticker.webp"/>'
                '</div></div>'
            )
            with patch.object(config.get_settings(), "base_dir", temp):
                media = exportmedia.find_media(elem)
            self.assertEqual(len(media), 1)
            self.assertEqual(
                os.path.normcase(os.path.normpath(media[0][0])),
                os.path.normcase(os.path.normpath(str(sticker))),
            )
            self.assertEqual(media[0][1], "sticker_static")

    def test_album_compatibility_only_allows_supported_media_groups(self):
        self.assertTrue(exportmedia.album_compatible([("a", "photo"), ("b", "video")]))
        self.assertTrue(exportmedia.album_compatible([("a", "document"), ("b", "document")]))
        self.assertFalse(exportmedia.album_compatible([("a", "photo"), ("b", "document")]))
        self.assertFalse(exportmedia.album_compatible([("a", "sticker_static"), ("b", "photo")]))

    def test_upload_size_guard_only_flags_files_strictly_over_limit(self):
        with patch.object(config.get_settings(), "max_upload_mb", 1):
            with patch.object(os.path, "getsize", side_effect=lambda path: {
                "exact": 1024 * 1024,
                "over": 1024 * 1024 + 1,
                "under": 1024 * 1024 - 1,
            }[path]):
                found = bigfiles.find_oversize_media([("exact", "document"), ("over", "document"), ("under", "photo")])
        self.assertEqual(found, [("over", "document", 1024 * 1024 + 1)])

    def test_sticker_upload_options_set_document_and_format_mime(self):
        options = uploads._sticker_upload_options("sticker.webp", "sticker_static")
        self.assertTrue(options["force_document"])
        self.assertEqual(options["mime_type"], "image/webp")
        self.assertEqual(len(options["attributes"]), 1)
        self.assertIsInstance(options["attributes"][0], tl.DocumentAttributeSticker)

    def test_video_sticker_metadata_is_used_when_available(self):
        with patch.object(mediatools, "probe_video_metadata", return_value={"width": 256, "height": 128, "duration": 2.5}):
            options = uploads._sticker_upload_options("sticker.webm", "sticker_video")
        self.assertEqual(options["mime_type"], "video/webm")
        self.assertTrue(options["nosound_video"])
        video = next(a for a in options["attributes"] if isinstance(a, tl.DocumentAttributeVideo))
        self.assertEqual((video.w, video.h, video.duration), (256, 128, 2.5))


class StateAndAuditTests(unittest.TestCase):
    def test_id_map_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "id-map.jsonl")
            with patch.object(config.get_settings(), "idmap_file", path):
                progress.append_id_map("10", 501)
                progress.append_id_map("11", 502)
                self.assertEqual(progress.load_id_map(), {"10": 501, "11": 502})

    def test_partial_send_state_round_trip_and_merges_latest_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "partial.jsonl")
            with patch.object(config.get_settings(), "partial_file", path):
                progress.save_state("10", {"src": "10", "text_prog": {"done": 1, "first": 90, "last": 90}})
                progress.save_state("10", {"src": "10", "text_prog": {"done": 2, "first": 90, "last": 91}, "media_id": 92})
                state = progress.load_partial()["10"]
            self.assertEqual(state["text_prog"]["done"], 2)
            self.assertEqual(state["text_prog"]["last"], 91)
            self.assertEqual(state["media_id"], 92)
            self.assertNotIn("src", state)

    def test_pending_reply_file_round_trip(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "pending.json")
            with patch.object(config.get_settings(), "pending_replies_file", path):
                self.assertTrue(progress.save_pending_replies({"20": "19", "21": "18"}))
                self.assertEqual(progress.load_pending_replies(), {"20": "19", "21": "18"})
                self.assertFalse(Path(path + ".tmp").exists())

    def test_manual_upload_ledger_reads_mapping_and_marks_once(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "manual.txt")
            with patch.object(config.get_settings(), "manual_uploads_file", path):
                Path(path).write_text("# comment\n123 456 # user uploaded\n", encoding="utf-8")
                done, mapped = bigfiles.load_manual_uploads()
                self.assertEqual(done, {"123"})
                self.assertEqual(mapped, {"123": 456})
                self.assertTrue(bigfiles.mark_manual_upload("124", ["files/large.bin"]))
                self.assertTrue(bigfiles.mark_manual_upload("124", ["files/large.bin"]))
                done, _ = bigfiles.load_manual_uploads()
            self.assertEqual(done, {"123", "124"})

    def test_failure_log_flattens_embedded_newlines(self):
        with tempfile.TemporaryDirectory() as temp:
            path = str(Path(temp) / "failed.txt")
            with patch.object(config.get_settings(), "failed_file", path):
                progress.log_failure("55", "bad\nmessage\twith whitespace")
            self.assertEqual(Path(path).read_text(encoding="utf-8"), "55\tbad message with whitespace\n")

    def test_manual_upload_instruction_includes_file_and_source_id(self):
        data = {"timestamp": datetime(2026, 10, 24, 12, 0), "sender": "Alice", "media": [("/tmp/video.mp4", "video")]}
        text = bigfiles.format_oversize_instructions("77", data, "caption", [("/tmp/video.mp4", "video", 3 * 1024 * 1024)], 500, 20, auto_marked=True)
        self.assertIn("message id=77", text)
        self.assertIn(os.path.abspath("/tmp/video.mp4"), text)
        self.assertIn("caption", text)
        self.assertIn("upload the file(s) now", text.lower())


class ReactionMetadataTests(unittest.TestCase):
    def test_normalize_reactions_supports_list_nested_object_and_actor_names(self):
        normalized = reaction_archive.normalize_reactions([
            {"emoji": "👍", "count": 3, "recent": [{"username": "alice"}, "Bob", "Bob"]},
            {"❤️": 2},
        ])
        self.assertEqual([item["label"] for item in normalized], ["👍", "❤️"])
        self.assertEqual(normalized[0]["count"], 3)
        self.assertEqual(normalized[0]["actors"], ["alice", "Bob"])
        self.assertEqual(normalized[1]["count"], 2)

    def test_load_json_reactions_matches_source_ids_and_ignores_unrelated_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            payload = {
                "name": "Example export",
                "messages": [
                    {"id": 10, "text": "hello", "reactions": [{"emoji": "👍", "count": 4}]},
                    {"id": 20, "text": "other", "reactions": [{"emoji": "❤️", "count": 9}]},
                ],
            }
            (Path(temp) / "result.json").write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(config.get_settings(), "base_dir", temp):
                reactions = reaction_archive.load_json_reactions([], {"10"})
            self.assertIn("10", reactions)
            self.assertNotIn("20", reactions)
            self.assertEqual(reactions["10"][0]["label"], "👍")
            self.assertEqual(reactions["10"][0]["count"], 4)

    def test_reaction_note_is_explicitly_archival_not_live(self):
        note = reaction_archive.format_reaction_archive_note([{"label": "👍", "count": 5, "actors": []}])
        self.assertIn("not live Telegram reaction totals", note)
        self.assertIn("👍 × 5", note)


class AsyncSendTests(unittest.TestCase):
    def test_send_text_chunks_tracks_successful_chunks_and_returns_first_id(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp:
                calls = []
                async def send_message(channel, text, **kwargs):
                    calls.append((channel, text, kwargs))
                    return SimpleNamespace(id=100 + len(calls))
                client = SimpleNamespace(send_message=send_message)
                state = {"src": "123"}
                with patch.object(config.get_settings(), "partial_file", str(Path(temp) / "partial.jsonl")):
                    result = await sending._send_text_chunks(
                        client, "channel", [("first", []), ("second", [])],
                        None, state, "text_prog", persist=True,
                    )
                    saved = progress.load_partial()["123"]
                self.assertEqual(result, 101)
                self.assertEqual([call[1] for call in calls], ["first", "second"])
                self.assertEqual(state["text_prog"]["done"], 2)
                self.assertEqual(state["text_prog"]["last"], 102)
                self.assertEqual(saved["text_prog"]["done"], 2)
                self.assertTrue(all(call[2]["parse_mode"] is None for call in calls))
        asyncio.run(scenario())

    def test_send_text_chunks_skips_already_checkpointed_chunks_on_resume(self):
        async def scenario():
            calls = []
            async def send_message(channel, text, **kwargs):
                calls.append(text)
                return SimpleNamespace(id=101 + len(calls))
            client = SimpleNamespace(send_message=send_message)
            state = {"src": "123", "text_prog": {"done": 2, "first": 50, "last": 51}}
            result = await sending._send_text_chunks(
                client, "channel", [("first", []), ("second", [])],
                None, state, "text_prog", persist=False,
            )
            self.assertEqual(result, 50)
            self.assertEqual(calls, [])
        asyncio.run(scenario())

    def test_send_text_only_persists_state_for_a_single_chunk(self):
        """A single-chunk text send must checkpoint to disk. Otherwise a crash
        between Telegram confirming the message and the id-map write would
        repeat that message on the next run. Media sends already save state;
        this test ensures plain text does too."""
        async def scenario():
            calls = []
            async def send_message(channel, text, **kwargs):
                calls.append(text)
                return SimpleNamespace(id=999)
            client = SimpleNamespace(send_message=send_message)
            state = {"src": "42"}
            with tempfile.TemporaryDirectory() as temp:
                with patch.object(config.get_settings(), "partial_file",
                                  str(Path(temp) / "partial.jsonl")):
                    result = await sending._send_text_only(
                        client, "channel", [("hello", [])], None, state, "42",
                    )
                    saved = progress.load_partial()
            self.assertEqual(result, 999)
            self.assertEqual(calls, ["hello"])
            self.assertIn("42", saved)
            self.assertEqual(saved["42"]["text_prog"]["done"], 1)
            self.assertEqual(saved["42"]["text_prog"]["first"], 999)
        asyncio.run(scenario())

    def test_document_upload_forces_document_and_disables_reparsing(self):
        async def scenario():
            calls = []
            async def send_file(channel, file, **kwargs):
                calls.append((channel, file, kwargs))
                return SimpleNamespace(id=777)
            client = SimpleNamespace(send_file=send_file)
            uploads._UNSUPPORTED_SEND_KWARGS.clear()
            result = await uploads.send_media_item(
                client, "channel", "files/scan.png", "document", None,
                caption="a *literal* caption", entities=None,
            )
            self.assertEqual(result.id, 777)
            self.assertEqual(len(calls), 1)
            kwargs = calls[0][2]
            self.assertTrue(kwargs["force_document"])
            self.assertIsNone(kwargs["parse_mode"])
            self.assertEqual(kwargs["caption"], "a *literal* caption")
        asyncio.run(scenario())

    def test_native_album_compatibility_is_separate_from_source_grouping(self):
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

    def test_inferred_incompatible_media_group_sends_in_order_and_returns_all_ids(self):
        async def scenario():
            calls = []
            async def send_file(channel, path, **kwargs):
                calls.append((path, dict(kwargs)))
                return SimpleNamespace(id=700 + len(calls))
            client = SimpleNamespace(send_file=send_file)
            state = {"src": "900"}
            data = {
                "_inferred_album": True,
                "_album_caption_text": "source caption",
                "_album_caption_entities": [],
            }
            media = [
                ("video_files/animation.mp4", "gif"),
                ("voice_messages/voice.ogg", "voice"),
            ]
            with patch.object(progress, "save_state"):
                result = await sending._do_send(
                    client, "channel", [("", [])], media, None, None,
                    state, None, "900", data,
                )
            self.assertEqual(result, [701, 702])
            self.assertEqual([call[0] for call in calls], [item[0] for item in media])
            self.assertEqual(calls[0][1]["caption"], "")
            self.assertTrue(calls[1][1]["voice_note"])
            self.assertEqual(calls[1][1]["caption"], "source caption")
            self.assertEqual(state["multi_media_ids"], [701, 702])
        asyncio.run(scenario())

    def test_gif_upload_uses_inline_animation_metadata_for_supported_gif_files(self):
        async def scenario():
            cases = (
                ("video_files/animation.mp4", "video/mp4", False),
                ("animations/animation.gif", "image/gif", None),
            )
            for index, (path, mime_type, nosound_video) in enumerate(cases, start=778):
                calls = []

                async def send_file(channel, file, **kwargs):
                    calls.append(kwargs)
                    return SimpleNamespace(id=index)

                client = SimpleNamespace(send_file=send_file)
                uploads._UNSUPPORTED_SEND_KWARGS.clear()
                await uploads.send_media_item(client, "channel", path, "gif", None)
                kwargs = calls[0]
                self.assertNotIn("force_document", kwargs)
                self.assertEqual(kwargs["mime_type"], mime_type)
                if nosound_video is None:
                    self.assertNotIn("nosound_video", kwargs)
                else:
                    self.assertIs(kwargs["nosound_video"], nosound_video)
                self.assertTrue(any(
                    isinstance(attr, tl.DocumentAttributeAnimated)
                    for attr in kwargs["attributes"]
                ))
        asyncio.run(scenario())

    def test_telegram_desktop_animation_markup_classifies_mp4_as_gif(self):
        with tempfile.TemporaryDirectory() as temp:
            media = Path(temp) / "video_files" / "animation.mp4"
            media.parent.mkdir()
            media.write_bytes(b"fake mp4 payload")
            page = Path(temp) / "messages.html"
            page.write_text(
                '<html><body><div class="message" id="message1">'
                '<div class="body"><div class="media_wrap clearfix">'
                '<a class="animated_wrap clearfix pull_left" '
                'href="video_files/animation.mp4">'
                '<div class="video_play_bg"><div class="gif_play">GIF</div></div>'
                '<img class="animated" src="video_files/animation.mp4_thumb.jpg"/>'
                '</a></div><div class="text">an example caption</div></div></div>'
                '</body></html>',
                encoding="utf-8",
            )
            with patch.object(config.get_settings(), "base_dir", temp):
                from lxml import html
                root = html.parse(str(page))
                message = root.xpath('//div[@id="message1"]')[0]
                data = htmlexport.extract_message_data(message)
            self.assertEqual(data["body_text"], "an example caption")
            self.assertEqual(len(data["media"]), 1)
            self.assertEqual(data["media"][0][1], "gif")

    def test_album_send_persists_first_message_id_and_does_not_resend_on_resume(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp:
                calls = []
                sent_messages = []
                async def send_file(channel, files, **kwargs):
                    calls.append((channel, files, kwargs))
                    captions = kwargs.get("caption") or ["", ""]
                    if not isinstance(captions, (list, tuple)):
                        captions = [captions, ""]
                    sent_messages[:] = [
                        SimpleNamespace(id=501, grouped_id=77, message=captions[0]),
                        SimpleNamespace(id=502, grouped_id=77, message=captions[1]),
                    ]
                    return sent_messages[:]
                async def get_messages(channel, ids=None):
                    wanted = set(ids if isinstance(ids, (list, tuple)) else [ids])
                    return [message for message in sent_messages if message.id in wanted]
                client = SimpleNamespace(send_file=send_file, get_messages=get_messages)
                state = {"src": "77"}
                with patch.object(config.get_settings(), "partial_file", str(Path(temp) / "partial.jsonl")):
                    result = await sending._do_send(
                        client, "channel", [("caption", [])],
                        [("photos/a.jpg", "photo"), ("photos/b.jpg", "photo")],
                        None, None, state, None,
                    )
                    persisted = progress.load_partial()["77"]
                    result_after_resume = await sending._do_send(
                        client, "channel", [("caption", [])],
                        [("photos/a.jpg", "photo"), ("photos/b.jpg", "photo")],
                        None, None, state, None,
                    )
                self.assertEqual(result, 501)
                self.assertEqual(result_after_resume, 501)
                self.assertEqual(len(calls), 1)
                self.assertEqual(state["album_id"], 501)
                self.assertEqual(persisted["album_id"], 501)
        asyncio.run(scenario())

    def test_album_send_passes_entities_aligned_with_captions(self):
        """Telethon accepts a flat entity list and normalizes it to a
        one-element list-of-lists, then pairs entity lists with captions.
        The flat form would associate every entity with the first caption
        only, and a caption mismatch trips the entity-bounds check and
        triggers the formatting fallback. Pass one list per caption
        instead — entities on the first caption, empty lists on the rest
        (empty lists, not None, so the value stays a proper list-of-lists
        and is never misread as a flat entity list)."""
        async def scenario():
            with tempfile.TemporaryDirectory() as temp:
                calls = []
                sent_messages = []
                async def send_file(channel, files, **kwargs):
                    calls.append((channel, files, kwargs))
                    captions = kwargs.get("caption") or ["", ""]
                    if not isinstance(captions, (list, tuple)):
                        captions = [captions, ""]
                    sent_messages[:] = [
                        SimpleNamespace(id=501, grouped_id=77, message=captions[0]),
                        SimpleNamespace(id=502, grouped_id=77, message=captions[1]),
                    ]
                    return sent_messages[:]
                async def get_messages(channel, ids=None):
                    wanted = set(ids if isinstance(ids, (list, tuple)) else [ids])
                    return [message for message in sent_messages if message.id in wanted]
                client = SimpleNamespace(send_file=send_file, get_messages=get_messages)
                state = {"src": "77"}
                bold = tl.MessageEntityBold(offset=0, length=5)
                with patch.object(config.get_settings(), "partial_file",
                                  str(Path(temp) / "partial.jsonl")):
                    await sending._do_send(
                        client, "channel", [("hello", [bold])],
                        [("photos/a.jpg", "photo"), ("photos/b.jpg", "photo")],
                        None, None, state, None,
                    )
                self.assertEqual(len(calls), 1)
                kwargs = calls[0][2]
                self.assertIn("formatting_entities", kwargs)
                ents = kwargs["formatting_entities"]
                self.assertEqual(len(ents), 2)
                self.assertEqual(ents[0], [])
                self.assertEqual(ents[1], [bold])
        asyncio.run(scenario())

    def test_inferred_album_attaches_caption_and_resumes_overflow_below(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as temp:
                album_calls = []
                caption_calls = []
                sent_messages = []
                fail_caption_once = [True]

                async def send_file(channel, files, **kwargs):
                    album_calls.append((files, kwargs))
                    self.assertEqual(kwargs.get("caption"), ["", "H" * 1024])
                    sent_messages[:] = [
                        SimpleNamespace(id=501, grouped_id=77, message=""),
                        SimpleNamespace(id=502, grouped_id=77, message="H" * 1024),
                    ]
                    return sent_messages[:]

                async def send_message(channel, text, **kwargs):
                    caption_calls.append((text, kwargs))
                    if fail_caption_once[0]:
                        fail_caption_once[0] = False
                        raise RuntimeError("temporary caption failure")
                    return SimpleNamespace(id=601 + len(caption_calls))

                async def get_messages(channel, ids=None):
                    wanted = set(ids if isinstance(ids, (list, tuple)) else [ids])
                    return [message for message in sent_messages if message.id in wanted]

                client = SimpleNamespace(
                    send_file=send_file, send_message=send_message,
                    get_messages=get_messages,
                )
                caption = "H" * 5000
                state = {"src": "77"}
                data = {
                    "msg_id": "77",
                    "_inferred_album": True,
                    "_album_caption_text": caption,
                    "_album_caption_entities": [],
                }
                media = [("photos/a.jpg", "photo"), ("photos/b.jpg", "photo")]
                with patch.object(config.get_settings(), "partial_file",
                                  str(Path(temp) / "partial.jsonl")):
                    with self.assertRaisesRegex(RuntimeError, "temporary caption failure"):
                        await sending._do_send(
                            client, "channel", [("", [])], media, None, None,
                            state, None, "77", data,
                        )
                    self.assertEqual(len(album_calls), 1)
                    self.assertEqual(album_calls[0][1]["caption"], ["", "H" * 1024])
                    self.assertEqual(state["album_message_ids"], [501, 502])

                    result = await sending._do_send(
                        client, "channel", [("", [])], media, None, None,
                        state, None, "77", data,
                    )

                self.assertEqual(result, [501, 502])
                self.assertEqual(len(album_calls), 1)
                self.assertEqual([text for text, _kwargs in caption_calls],
                                 ["H" * 3976, "H" * 3976])
                self.assertEqual(caption_calls[0][1]["reply_to"], 502)
                self.assertEqual(state["album_caption_continuation_prog"]["done"], 1)
        asyncio.run(scenario())

    def test_video_without_audio_probe_result_is_not_forced_to_silent_video(self):
        async def scenario():
            calls = []
            async def send_file(channel, file, **kwargs):
                calls.append(kwargs)
                return SimpleNamespace(id=779)
            client = SimpleNamespace(send_file=send_file)
            uploads._UNSUPPORTED_SEND_KWARGS.clear()
            with patch.object(mediatools, "video_has_audio", return_value=None):
                await uploads.send_media_item(client, "channel", "video_files/clip.mp4", "video", None)
            self.assertNotIn("nosound_video", calls[0])
        asyncio.run(scenario())

    def test_send_file_retries_without_unsupported_optional_keyword(self):
        async def scenario():
            calls = []
            async def send_file(channel, file, **kwargs):
                calls.append(dict(kwargs))
                if "mime_type" in kwargs:
                    raise TypeError("send_file() got an unexpected keyword argument 'mime_type'")
                return "sent"
            client = SimpleNamespace(send_file=send_file)
            uploads._UNSUPPORTED_SEND_KWARGS.clear()
            result = await uploads.send_file(client, "channel", "file.webp", mime_type="image/webp", caption="x")
            self.assertEqual(result, "sent")
            self.assertEqual(len(calls), 2)
            self.assertIn("mime_type", calls[0])
            self.assertNotIn("mime_type", calls[1])
            self.assertIn("mime_type", uploads._UNSUPPORTED_SEND_KWARGS)
        asyncio.run(scenario())

    def test_formatting_fallback_retries_only_without_entities(self):
        async def scenario():
            calls = []
            async def send(**kwargs):
                calls.append(dict(kwargs))
                if len(calls) == 1:
                    raise EntityBoundsError("invalid entity bounds")
                return "sent"
            class EntityBoundsError(Exception):
                pass
            with tempfile.TemporaryDirectory() as temp:
                with patch.object(config.get_settings(), "format_debug_file", str(Path(temp) / "format.jsonl")):
                    result = await formatfallback.with_formatting_fallback(
                        send, {"formatting_entities": [tl.MessageEntityItalic(offset=0, length=1)]},
                        "x", [tl.MessageEntityItalic(offset=0, length=1)], "1",
                    )
                self.assertEqual(result, "sent")
                self.assertEqual(len(calls), 2)
                self.assertIsNone(calls[1]["formatting_entities"])
                self.assertTrue(Path(temp, "format.jsonl").exists())
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
