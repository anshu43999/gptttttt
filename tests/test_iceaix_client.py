from platforms.chatgpt.iceaix_client import configure_utf8_stdio


def test_configure_utf8_stdio_is_idempotent():
    configure_utf8_stdio()
    configure_utf8_stdio()
