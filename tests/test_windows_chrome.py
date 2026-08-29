import builtins
import importlib
import sys
import unittest
from unittest.mock import MagicMock, patch


class WindowsChromeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        real_import = builtins.__import__

        def import_without_readline(name, *args, **kwargs):
            if name == "readline":
                raise ImportError("readline is unavailable on Windows")
            return real_import(name, *args, **kwargs)

        sys.modules.pop("royale.ui", None)
        with patch.object(builtins, "__import__", side_effect=import_without_readline):
            cls.ui = importlib.import_module("royale.ui")

    def test_player_picker_works_without_readline(self):
        players = {"AAA": {"player_name": "Alice"}}

        with patch("builtins.input", side_effect=["1", ""]), \
                patch.object(self.ui.console, "print"):
            chosen = self.ui.pick_players(players, ["AAA"])

        self.assertEqual(chosen, ["AAA"])
        self.assertIsNone(self.ui.readline)

    def test_unreadable_chrome_cookie_uses_browser_login(self):
        pages = MagicMock()
        curl = MagicMock()
        curl.logged_in.return_value = True
        players = {"AAA": {"player_name": "Alice"}}
        session = self.ui.Session("this browser", "session-cookie")

        with patch.object(self.ui, "pick_session", return_value=None), \
                patch.object(self.ui, "Pages", return_value=pages), \
                patch.object(self.ui, "browser_login", return_value=session) as login, \
                patch.object(self.ui, "Curl", return_value=curl) as curl_type, \
                patch.object(self.ui, "pick_settings", return_value=4), \
                patch.object(self.ui, "roster", return_value=(players, {}, ["AAA"])), \
                patch.object(self.ui, "pick_players", return_value=["AAA"]), \
                patch.object(self.ui, "pick_depth", return_value=1), \
                patch.object(self.ui, "crawl", return_value=1), \
                patch.object(self.ui.console, "print"), \
                patch.object(self.ui.console, "status", return_value=MagicMock()):
            result = self.ui.app()

        self.assertEqual(result, 1)
        login.assert_called_once_with(pages)
        curl_type.assert_called_once_with(pages, session)
        pages.close.assert_called_once_with()

    def test_windows_uses_installed_chrome_for_cloudflare(self):
        from royale import transport

        events = []
        starter = MagicMock()
        playwright = starter.start.return_value
        browser = playwright.chromium.launch.return_value
        context = browser.new_context.return_value
        page = context.new_page.return_value
        page.evaluate.return_value = "Chrome user agent"
        page.title.return_value = "RoyaleAPI"
        page.goto.side_effect = lambda *args, **kwargs: events.append("goto")
        page.route.side_effect = lambda *args, **kwargs: events.append("route")
        context.cookies.return_value = [{"name": "cf_clearance", "value": "clearance"}]

        with patch.object(transport.sys, "platform", "win32"), \
                patch.object(transport, "sync_playwright", return_value=starter):
            pages = transport.Pages()

        playwright.chromium.launch.assert_called_once_with(
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        self.assertEqual(events, ["goto", "route"])
        pages.close()

    def test_selftest_replaces_an_expired_browser_session(self):
        import scrape

        stale_session = self.ui.Session("chrome", "expired")
        stale_curl = MagicMock()
        stale_curl.logged_in.return_value = False
        fresh_curl = MagicMock()
        fresh_curl.logged_in.return_value = True
        fresh_curl.limiter.sent = 11
        fresh_curl.limiter.rate = 2.0
        fresh_curl.limiter.hits = 1
        fresh_curl.limiter.ceiling = 10.0
        fresh_curl.limiter.floor = 0.5
        pages = MagicMock()
        pages.login.return_value = "fresh"
        deck = self.ui.SEED.replace("cannon-ev1", "cannon")
        player = {"player_tag": "TAG", "rating": "1"}
        page1 = [{"replay_tag": "one", "battle_timestamp": "2"}]
        paged = page1 + [{"replay_tag": "two", "battle_timestamp": "1"}]
        rows = [{"team_deck": self.ui.SEED, "result": "win"}]
        stats = {"team_elixir_total": 1, "oppo_elixir_leaked": 1}
        plays = [{"side": "blue", "card": "cannon", "tick": tick} for tick in range(11)]

        with patch.object(scrape, "find_sessions", return_value=[stale_session]), \
                patch.object(scrape, "Pages", return_value=pages), \
                patch.object(scrape, "Curl", side_effect=[stale_curl, fresh_curl]) as curl_type, \
                patch.object(scrape.pipeline, "similar_decks",
                             return_value=[self.ui.SEED, deck]), \
                patch.object(scrape.pipeline, "rated_players",
                             return_value=({"TAG": player}, {"TAG": self.ui.SEED})), \
                patch.object(scrape.pipeline, "player_battles",
                             side_effect=[page1, paged]), \
                patch.object(scrape.pipeline, "battles", return_value=(rows, 0)), \
                patch.object(scrape.pipeline, "replays",
                             return_value=[("battle", (stats, plays))]), \
                patch.object(scrape.console, "print"):
            scrape.selftest()

        pages.login.assert_called_once_with()
        self.assertEqual(curl_type.call_args_list[1].args[1].browser, "this browser")
        pages.close.assert_called_once_with()

    def test_curl_replaces_undecodable_windows_output(self):
        from royale import transport

        process = MagicMock(returncode=0, stdout=b"payload\x81\n200", stderr=b"")
        limiter = MagicMock()
        pages = MagicMock(clearance="clearance", ua="Chrome user agent")
        curl = transport.Curl(
            pages,
            self.ui.Session("chrome", "session"),
            limiter=limiter,
        )

        with patch.object(transport.subprocess, "run", return_value=process):
            body = curl.get("/me", auth=True)

        self.assertEqual(body, "payload\ufffd")
        limiter.ok.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
