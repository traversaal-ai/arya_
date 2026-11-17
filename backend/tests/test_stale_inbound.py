"""Late deliveries. Meta retries a webhook it could not deliver for up to
seven days, so after a dead tunnel or a rebuild the bridge gets messages
from the previous day. Seen live: two greetings sent in reply to a "Hi"
from twenty hours earlier. Those are acknowledged and recorded, never
answered."""

from whatsapp_bridge import STALE_INBOUND_S, is_stale

NOW = 1_800_000_000.0


def test_a_message_from_yesterday_is_stale():
    assert is_stale(int(NOW - 20 * 3600), NOW)


def test_a_message_from_a_minute_ago_is_fresh():
    assert not is_stale(int(NOW - 60), NOW)


def test_the_line_is_the_configured_age():
    assert not is_stale(int(NOW - STALE_INBOUND_S + 1), NOW)
    assert is_stale(int(NOW - STALE_INBOUND_S - 1), NOW)


def test_no_timestamp_means_fresh():
    # Meta always sends one; a fixture or a future change that drops it
    # must fail open, since ignoring live messages is the worse mistake.
    assert not is_stale(0, NOW)
