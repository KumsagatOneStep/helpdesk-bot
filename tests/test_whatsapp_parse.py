from helpdesk.whatsapp.parse import IncomingMessage, parse_notification


def _body(message_data, chat_id="77011234567@c.us", type_webhook="incomingMessageReceived"):
    return {
        "typeWebhook": type_webhook,
        "instanceData": {"idInstance": 1101, "wid": "77000000000@c.us"},
        "timestamp": 1791364800,
        "idMessage": "BAE5F4886F6F2D05",
        "senderData": {"chatId": chat_id, "sender": chat_id, "senderName": "Aigerim"},
        "messageData": message_data,
    }


def test_text_message():
    m = parse_notification(
        _body({"typeMessage": "textMessage", "textMessageData": {"textMessage": "Не печатает"}})
    )
    assert m == IncomingMessage(
        wa_message_id="BAE5F4886F6F2D05",
        chat_id="77011234567@c.us",
        phone="77011234567",
        sender_name="Aigerim",
        kind="text",
        text="Не печатает",
    )


def test_extended_and_quoted_text():
    for tm in ("extendedTextMessage", "quotedMessage"):
        m = parse_notification(
            _body({"typeMessage": tm, "extendedTextMessageData": {"text": "см. выше"}})
        )
        assert m.kind == "text" and m.text == "см. выше"


def test_file_messages():
    cases = {
        "imageMessage": "image",
        "documentMessage": "document",
        "audioMessage": "audio",
        "videoMessage": "video",
    }
    for tm, kind in cases.items():
        m = parse_notification(
            _body(
                {
                    "typeMessage": tm,
                    "fileMessageData": {
                        "downloadUrl": "https://sw-media-out.example/f.bin",
                        "caption": "ошибка",
                        "fileName": "f.bin",
                        "mimeType": "application/octet-stream",
                    },
                }
            )
        )
        assert (m.kind, m.text, m.download_url, m.file_name) == (
            kind,
            "ошибка",
            "https://sw-media-out.example/f.bin",
            "f.bin",
        )


def test_empty_caption_is_none():
    m = parse_notification(
        _body(
            {"typeMessage": "imageMessage", "fileMessageData": {"downloadUrl": "u", "caption": ""}}
        )
    )
    assert m.text is None


def test_unsupported_types_become_other():
    m = parse_notification(_body({"typeMessage": "stickerMessage"}))
    assert (m.kind, m.text) == ("other", "[стикер]")
    m = parse_notification(_body({"typeMessage": "somethingNew"}))
    assert (m.kind, m.text) == ("other", "[somethingNew]")


def test_ignored_notifications():
    assert parse_notification(_body({"typeMessage": "reactionMessage"})) is None
    assert parse_notification(_body({"typeMessage": "editedMessage"})) is None
    assert parse_notification(_body({"typeMessage": "textMessage"}, chat_id="1203@g.us")) is None
    assert parse_notification(_body({}, type_webhook="outgoingMessageReceived")) is None
    assert parse_notification({"typeWebhook": "stateInstanceChanged"}) is None


def test_malformed_body_returns_none():
    text = {"typeMessage": "textMessage", "textMessageData": {"textMessage": "x"}}
    no_id = _body(text)
    del no_id["idMessage"]
    assert parse_notification(no_id) is None
    assert parse_notification(_body(text, chat_id=None)) is None
