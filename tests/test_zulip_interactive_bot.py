import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from author_whitelist import AuthorWhitelist, WhitelistedAuthor
from zulip_interactive_bot import (
    interactive_cursor_key,
    process_interactive_message,
    should_handle_message,
)


def _stream_msg(*, mid=10, stream="science", topic="papers", content="@bot list", sender="alice@x.com"):
    return {
        "id": mid,
        "timestamp": int(time.time()),
        "type": "stream",
        "display_recipient": stream,
        "subject": topic,
        "content": content,
        "sender_email": sender,
    }


def _dm_msg(*, mid=11, content="list", sender="alice@x.com"):
    return {
        "id": mid,
        "timestamp": int(time.time()),
        "type": "private",
        "display_recipient": [
            {"email": "bot@x.com", "full_name": "Bot"},
            {"email": sender, "full_name": "Alice"},
        ],
        "content": content,
        "sender_email": sender,
    }


class TestShouldHandleMessage(unittest.TestCase):
    def test_stream_mentioned_in_configured_stream(self):
        msg = _stream_msg()
        self.assertTrue(
            should_handle_message(
                msg,
                bot_email="bot@x.com",
                stream="science",
                dms=True,
                flags=["mentioned"],
            )
        )

    def test_stream_without_mention_ignored(self):
        self.assertFalse(
            should_handle_message(
                _stream_msg(content="list"),
                bot_email="bot@x.com",
                stream="science",
                dms=True,
                flags=[],
            )
        )

    def test_other_stream_ignored(self):
        self.assertFalse(
            should_handle_message(
                _stream_msg(stream="offtopic"),
                bot_email="bot@x.com",
                stream="science",
                dms=True,
                flags=["mentioned"],
            )
        )

    def test_wildcard_without_mention_ignored(self):
        self.assertFalse(
            should_handle_message(
                _stream_msg(),
                bot_email="bot@x.com",
                stream="science",
                dms=True,
                flags=["wildcard_mentioned"],
            )
        )

    def test_own_message_ignored(self):
        self.assertFalse(
            should_handle_message(
                _stream_msg(sender="bot@x.com"),
                bot_email="bot@x.com",
                stream="science",
                dms=True,
                flags=["mentioned"],
            )
        )

    def test_dm_without_mention(self):
        self.assertTrue(
            should_handle_message(
                _dm_msg(),
                bot_email="bot@x.com",
                stream="science",
                dms=True,
                flags=[],
            )
        )

    def test_dm_disabled(self):
        self.assertFalse(
            should_handle_message(
                _dm_msg(),
                bot_email="bot@x.com",
                stream="science",
                dms=False,
                flags=[],
            )
        )


class TestCursorKey(unittest.TestCase):
    def test_keys(self):
        self.assertEqual(
            interactive_cursor_key("myrealm", "stream", "science"),
            "interactive:myrealm:science",
        )
        self.assertEqual(
            interactive_cursor_key("myrealm", "dm"),
            "interactive:myrealm:dm",
        )


class TestProcessInteractiveMessage(unittest.TestCase):
    def test_list_replies_in_same_topic(self):
        client = MagicMock()
        client.send_message.return_value = {"result": "success"}
        client.add_reaction.return_value = {"result": "success"}
        wl = AuthorWhitelist()
        wl.add(
            WhitelistedAuthor(
                id="https://orcid.org/0000-0002-1825-0097",
                display_name="Josiah Carberry",
                orcid="0000-0002-1825-0097",
            )
        )
        with TemporaryDirectory() as td:
            path = Path(td) / "wl.json"
            wl.save(path)
            process_interactive_message(
                client,
                _stream_msg(content="@bot list"),
                flags=["mentioned"],
                bot_email="bot@x.com",
                realm="myrealm",
                stream="science",
                dms=True,
                whitelist=wl,
                wl_path=path,
                mailto=None,
            )
        sent = client.send_message.call_args[0][0]
        self.assertEqual(sent["type"], "stream")
        self.assertEqual(sent["to"], "science")
        self.assertEqual(sent["topic"], "papers")
        self.assertIn("Josiah Carberry", sent["content"])
        self.assertIn("0000-0002-1825-0097", sent["content"])
        self.assertEqual(
            wl.get_cursor("interactive:myrealm:science"), 10
        )

    def test_unknown_mention_sends_help(self):
        client = MagicMock()
        client.send_message.return_value = {"result": "success"}
        client.add_reaction.return_value = {"result": "success"}
        wl = AuthorWhitelist()
        with TemporaryDirectory() as td:
            path = Path(td) / "wl.json"
            process_interactive_message(
                client,
                _stream_msg(content="@bot what can you do"),
                flags=["mentioned"],
                bot_email="bot@x.com",
                realm="myrealm",
                stream="science",
                dms=True,
                whitelist=wl,
                wl_path=path,
                mailto=None,
            )
        sent = client.send_message.call_args[0][0]
        self.assertIn("`help`", sent["content"])
        self.assertIn("`list`", sent["content"])

    def test_dm_list_is_private_reply(self):
        client = MagicMock()
        client.send_message.return_value = {"result": "success"}
        client.add_reaction.return_value = {"result": "success"}
        wl = AuthorWhitelist()
        with TemporaryDirectory() as td:
            path = Path(td) / "wl.json"
            process_interactive_message(
                client,
                _dm_msg(content="list"),
                flags=[],
                bot_email="bot@x.com",
                realm="myrealm",
                stream="science",
                dms=True,
                whitelist=wl,
                wl_path=path,
                mailto=None,
            )
        sent = client.send_message.call_args[0][0]
        self.assertEqual(sent["type"], "private")
        self.assertEqual(sent["to"], ["alice@x.com"])

    def test_skips_already_processed_id(self):
        client = MagicMock()
        wl = AuthorWhitelist()
        wl.set_cursor("interactive:myrealm:science", 10)
        with TemporaryDirectory() as td:
            path = Path(td) / "wl.json"
            process_interactive_message(
                client,
                _stream_msg(mid=10, content="@bot list"),
                flags=["mentioned"],
                bot_email="bot@x.com",
                realm="myrealm",
                stream="science",
                dms=True,
                whitelist=wl,
                wl_path=path,
                mailto=None,
            )
        client.send_message.assert_not_called()

    @patch("author_whitelist_bot.resolve")
    def test_add_persists_json(self, mock_resolve):
        mock_resolve.return_value = WhitelistedAuthor(
            id="https://orcid.org/0000-0002-1825-0097",
            display_name="Josiah Carberry",
            orcid="0000-0002-1825-0097",
        )
        client = MagicMock()
        client.send_message.return_value = {"result": "success"}
        client.add_reaction.return_value = {"result": "success"}
        with TemporaryDirectory() as td:
            path = Path(td) / "wl.json"
            wl = AuthorWhitelist()
            process_interactive_message(
                client,
                _stream_msg(
                    content="@bot add https://orcid.org/0000-0002-1825-0097"
                ),
                flags=["mentioned"],
                bot_email="bot@x.com",
                realm="myrealm",
                stream="science",
                dms=True,
                whitelist=wl,
                wl_path=path,
                mailto="me@x.com",
            )
            saved = AuthorWhitelist.load(path)
        self.assertEqual(len(saved.authors), 1)
        self.assertEqual(saved.authors[0].orcid, "0000-0002-1825-0097")
        client.add_reaction.assert_called_once_with(
            {"message_id": 10, "emoji_name": "+1"}
        )


if __name__ == "__main__":
    unittest.main()
