"""Product pictures on WhatsApp: the bridge turns an option group's items
into photo bubbles before the list. Only items that carry a picture make
one, so a shop without image links changes nothing."""

from whatsapp_bridge import photo_bubbles


def test_each_pictured_item_becomes_a_captioned_bubble():
    group = {
        "kind": "orders", "title": "Which one do you mean?",
        "items": [
            {"n": 1, "label": "Samsung front load", "detail": "$749.00",
             "image": "https://img/a.jpg", "image_fallback": "https://cdn/a.jpg"},
            {"n": 2, "label": "Whirlpool top load", "detail": "$599.00",
             "image": "https://img/b.jpg", "image_fallback": None},
            {"n": 3, "label": "No picture", "detail": "$1.00"},
        ],
    }
    assert photo_bubbles(group) == [
        ("https://img/a.jpg", "https://cdn/a.jpg", "Samsung front load, $749.00"),
        ("https://img/b.jpg", None, "Whirlpool top load, $599.00"),
    ]


def test_a_group_without_pictures_makes_no_bubbles():
    assert photo_bubbles({"items": [{"n": 1, "label": "Option 1", "value": "x"}]}) == []
    assert photo_bubbles({}) == []


async def test_a_failed_delivery_receipt_sends_the_hosted_copy_once():
    # Meta accepts the send, then reports "Media upload error" in a receipt
    # when it cannot fetch the link (Unsplash served it AVIF). The thread
    # then sends the hosted copy, once, and the copy itself has no copy.
    import whatsapp_bridge as wb

    class FakeBridge:
        async def record_event(self, **kw):
            pass

    class FakeClient:
        def __init__(self):
            self.sent = []

        async def send_image(self, to, *, link=None, media_id=None, caption=None):
            self.sent.append((link, caption))
            return f"wamid.{len(self.sent)}"

    thread = wb.Thread(FakeBridge(), {"id": "chan"}, "923358370304", None)
    client = FakeClient()
    thread._client = lambda: client
    await thread._send_photo("https://img/a.jpg", "https://cdn/a.jpg", "Samsung, $749.00")
    assert client.sent == [("https://img/a.jpg", "Samsung, $749.00")]
    assert await thread.photo_failed("wamid.1") is True
    assert client.sent[-1] == ("https://cdn/a.jpg", "Samsung, $749.00")
    assert await thread.photo_failed("wamid.1") is False
    assert await thread.photo_failed("wamid.2") is False
    assert len(client.sent) == 2
