import time
import unittest
from unittest.mock import MagicMock, patch

from author_whitelist import AuthorWhitelist, WhitelistedAuthor
from author_whitelist_bot import parse_command, run_author_whitelist_bot

REALMS = {"tuesday": {"email": "bot@x.com", "api_key": "k", "site": "https://x"}}
SOURCE = {
    "realm": "tuesday",
    "stream": "science",
    "topic": "author whitelist",
    "lookback_hours": 168,
    "max_messages": 200,
}


def _msg(mid, content, sender="alice@x.com"):
    return {
        "id": mid,
        "timestamp": int(time.time()),
        "content": content,
        "sender_email": sender,
    }


def _fake_client(messages):
    c = MagicMock()
    c.get_messages.return_value = {"result": "success", "messages": messages}
    c.send_message.return_value = {"result": "success"}
    return c


def _author():
    return WhitelistedAuthor(
        id="https://orcid.org/0000-0002-1825-0097",
        display_name="Josiah Carberry",
        name_aliases=["Josiah Carberry"],
        orcid="0000-0002-1825-0097",
        openalex_id="A5023888391",
        affiliation="Brown University",
        works_count=142,
    )


class TestParseCommand(unittest.TestCase):
    def test_add(self):
        self.assertEqual(
            parse_command("add https://orcid.org/0000-0002-1825-0097"),
            ("add", "https://orcid.org/0000-0002-1825-0097"),
        )

    def test_add_with_mention(self):
        self.assertEqual(parse_command("@bot add 0000"), ("add", "0000"))

    def test_remove_and_list(self):
        self.assertEqual(
            parse_command("remove josiah carberry"), ("remove", "josiah carberry")
        )
        self.assertEqual(parse_command("list"), ("list", ""))

    def test_non_command(self):
        self.assertIsNone(parse_command("hello team, nice paper"))


class TestRunBot(unittest.TestCase):
    @patch("author_whitelist_bot._client_for_realm")
    @patch("author_whitelist_bot.resolve")
    def test_add_flow(self, mock_resolve, mock_client_for):
        mock_resolve.return_value = _author()
        client = _fake_client(
            [_msg(101, "add https://orcid.org/0000-0002-1825-0097")]
        )
        mock_client_for.return_value = client
        wl = AuthorWhitelist()
        changed = run_author_whitelist_bot(
            wl, command_source=SOURCE, realms=REALMS, mailto="me@x.com", dryrun=False
        )
        self.assertTrue(changed)
        self.assertEqual(len(wl.authors), 1)
        client.send_message.assert_called_once()
        sent = client.send_message.call_args[0][0]
        self.assertEqual(sent["topic"], "author whitelist")
        self.assertEqual(wl.get_cursor("tuesday:science:author whitelist"), 101)

    @patch("author_whitelist_bot._client_for_realm")
    @patch("author_whitelist_bot.resolve")
    def test_idempotent_second_run(self, mock_resolve, mock_client_for):
        mock_resolve.return_value = _author()
        client = _fake_client([_msg(101, "add 0000-0002-1825-0097")])
        mock_client_for.return_value = client
        wl = AuthorWhitelist()
        wl.set_cursor("tuesday:science:author whitelist", 101)
        changed = run_author_whitelist_bot(
            wl, command_source=SOURCE, realms=REALMS, mailto="me@x.com", dryrun=False
        )
        self.assertFalse(changed)
        self.assertEqual(wl.authors, [])
        client.send_message.assert_not_called()

    @patch("author_whitelist_bot._client_for_realm")
    @patch("author_whitelist_bot.resolve")
    def test_skips_bot_own_messages(self, mock_resolve, mock_client_for):
        client = _fake_client([_msg(102, "add 0000", sender="bot@x.com")])
        mock_client_for.return_value = client
        wl = AuthorWhitelist()
        run_author_whitelist_bot(
            wl, command_source=SOURCE, realms=REALMS, mailto="me@x.com", dryrun=False
        )
        mock_resolve.assert_not_called()
        self.assertEqual(wl.authors, [])

    @patch("author_whitelist_bot._client_for_realm")
    @patch("author_whitelist_bot.resolve")
    def test_dryrun_does_not_send(self, mock_resolve, mock_client_for):
        mock_resolve.return_value = _author()
        client = _fake_client([_msg(101, "add 0000")])
        mock_client_for.return_value = client
        wl = AuthorWhitelist()
        run_author_whitelist_bot(
            wl, command_source=SOURCE, realms=REALMS, mailto="me@x.com", dryrun=True
        )
        client.send_message.assert_not_called()
        self.assertEqual(len(wl.authors), 1)


if __name__ == "__main__":
    unittest.main()
