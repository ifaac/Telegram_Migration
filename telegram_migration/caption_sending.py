"""Per-item captions for inferred media posts and albums."""

from . import config


def inferred_album_item_caption_parts(item_captions):
    """Split each source message's caption independently for its media item."""
    from .textutil import split_with_entities

    captions = []
    caption_entities = []
    continuations = []
    for text, entities in item_captions:
        chunks = split_with_entities(
            text, entities or [],
            config.settings.caption_limit, config.settings.text_limit,
        )
        first, *rest = chunks
        captions.append(first[0])
        caption_entities.append(first[1])
        continuations.append(rest)
    return captions, caption_entities, continuations


async def send_inferred_album_item_caption_continuations(
    client, channel, item_captions, state, source_id, album_message_ids,
):
    """Reply to each corresponding media item only when its caption overflows."""
    from .sending import _send_text_chunks

    _captions, _entities, continuations = inferred_album_item_caption_parts(
        item_captions
    )
    if len(album_message_ids) != len(continuations):
        raise RuntimeError(
            f"Cannot restore per-item audio captions for source id={source_id}: "
            "caption and destination item counts differ"
        )
    for index, (message_id, extra_chunks) in enumerate(
        zip(album_message_ids, continuations)
    ):
        if extra_chunks:
            await _send_text_chunks(
                client, channel, extra_chunks, int(message_id), state,
                f"album_item_caption_{index}_prog", source_id,
            )
