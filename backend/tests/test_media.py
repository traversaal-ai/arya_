"""Image handling for the WhatsApp channel.

Two things are worth pinning down here. The conversion has to survive what
phone cameras actually produce, which is large, rotated, and occasionally
not an image at all. And the accept-or-refuse rule has to be decidable
from settings alone, before anything is downloaded, because a refusal the
customer waits eight seconds for reads as a broken bot.
"""

import io

import pytest
from PIL import Image

import media
import whatsapp_bridge


def _png(width: int, height: int, mode: str = "RGB", color=(200, 30, 30)) -> bytes:
    img = Image.new(mode, (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg(width: int, height: int, exif: bytes | None = None) -> bytes:
    img = Image.new("RGB", (width, height), (10, 120, 200))
    buf = io.BytesIO()
    if exif is None:
        img.save(buf, format="JPEG")
    else:
        img.save(buf, format="JPEG", exif=exif)
    return buf.getvalue()


# ── shrink_image ─────────────────────────────────────────────────────────────


def test_a_large_photo_is_reduced_to_the_long_edge():
    out, w, h = media.shrink_image(_png(4000, 3000))
    assert (w, h) == (1500, 1125)
    assert out[:2] == b"\xff\xd8"  # JPEG, whatever came in
    assert len(out) < len(_png(4000, 3000))


def test_a_small_photo_is_left_at_its_own_size():
    # Upscaling a low-resolution photo would cost bytes and add nothing.
    _out, w, h = media.shrink_image(_png(640, 480))
    assert (w, h) == (640, 480)


def test_a_portrait_photo_scales_on_its_own_long_edge():
    _out, w, h = media.shrink_image(_png(2000, 4000))
    assert (w, h) == (750, 1500)


def test_transparency_is_flattened_onto_white_not_black():
    # A screenshot with an alpha channel otherwise reaches the model as
    # black text on black.
    img = Image.new("RGBA", (50, 50), (255, 255, 255, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    out, _w, _h = media.shrink_image(buf.getvalue())
    back = Image.open(io.BytesIO(out)).convert("RGB")
    assert back.getpixel((25, 25)) == (255, 255, 255)


def test_a_greyscale_photo_survives_the_conversion():
    out, w, h = media.shrink_image(_png(100, 100, mode="L", color=128))
    assert (w, h) == (100, 100)
    assert Image.open(io.BytesIO(out)).mode == "RGB"


def test_exif_rotation_is_applied_not_carried():
    # The tag is lost on re-encode, so a sideways photo of a serial number
    # would be unreadable if the pixels were left alone.
    exif = Image.Exif()
    exif[274] = 6  # rotate 90 degrees
    tall = media.shrink_image(_jpeg(800, 400, exif=exif.tobytes()))
    assert (tall[1], tall[2]) == (400, 800)


def test_junk_bytes_are_refused_with_a_reason():
    with pytest.raises(media.MediaError) as e:
        media.shrink_image(b"this is not a photograph")
    assert "not a readable image" in str(e.value)


def test_an_empty_download_is_refused():
    with pytest.raises(media.MediaError):
        media.shrink_image(b"")


def test_an_oversized_download_is_refused_before_decoding():
    with pytest.raises(media.MediaError) as e:
        media.shrink_image(b"\xff\xd8" + b"0" * media.MAX_IMAGE_BYTES)
    assert "larger than" in str(e.value)


# ── accept or refuse ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("setting", "vision", "expected"),
    [
        ("auto", True, "accept"),
        ("auto", False, "declined_model"),
        ("off", True, "declined_off"),
        ("off", False, "declined_off"),
        # An unset or unrecognised value behaves as auto rather than
        # silently refusing every photo.
        ("", True, "accept"),
        (None, True, "accept"),
        ("AUTO", True, "accept"),
        ("OFF", True, "declined_off"),
    ],
)
def test_the_image_decision_table(setting, vision, expected):
    assert whatsapp_bridge.decide_image(setting, vision) == expected


def test_off_beats_a_capable_model():
    # The operator's switch is an override, not a hint: a channel set to
    # off must refuse even where the model could have read the photo.
    assert whatsapp_bridge.decide_image("off", True) == "declined_off"


def test_the_two_refusals_say_different_things():
    # One is a setting the operator can change, the other is the model.
    # Telling a customer the wrong one sends them round a loop.
    assert whatsapp_bridge.IMAGE_OFF_TEXT != whatsapp_bridge.IMAGE_NO_VISION_TEXT
    for note in (whatsapp_bridge.IMAGE_OFF_TEXT,
                 whatsapp_bridge.IMAGE_NO_VISION_TEXT,
                 whatsapp_bridge.IMAGE_BROKEN_TEXT):
        assert note.strip()
        assert "—" not in note  # house rule: no em dashes in UI copy


def test_the_attachment_topic_matches_the_worker():
    # The bridge publishes and the worker subscribes; a typo on either
    # side loses every photo silently.
    assert whatsapp_bridge.ATTACH_TOPIC == "arya.attachments"
