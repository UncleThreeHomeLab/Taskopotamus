import io
import json
import unittest
from unittest.mock import Mock
from unittest.mock import patch

from src.agent.profile_client import NoRedirects, ProfileClient
from src.profiles.auth import ProfileTokens
from src.profiles.instructions import DEFAULT_PROFILE


SECRET = "synthetic-test-profile-signing-key-only-123456"


class ProfileClientTests(unittest.TestCase):
    def test_profile_is_loaded_each_time_and_credentials_stay_out_of_prompt_and_config(self):
        client = ProfileClient("http://mcp:8080", SECRET)
        client.opener = Mock()
        client.opener.open.side_effect = [io.BytesIO(json.dumps({**DEFAULT_PROFILE, "persona": value}).encode())
                                          for value in ("First preference", "Updated preference")]
        first, config, token = client.prepare("42")
        second, _, _ = client.prepare("42")
        self.assertEqual(first["persona"], "First preference")
        self.assertEqual(second["persona"], "Updated preference")
        self.assertEqual(ProfileTokens(SECRET).verify("Bearer " + token), "42")
        self.assertNotIn(token, json.dumps([first, config]))
        self.assertNotIn(SECRET, json.dumps([first, config]))
        self.assertEqual(config["mcp_servers.profiles.enabled_tools"], ["get_agent_profile", "update_agent_profile"])
        self.assertEqual(client.opener.open.call_count, 2)

    def test_invalid_or_oversized_response_fails_closed(self):
        client = ProfileClient("http://mcp:8080", SECRET)
        client.opener = Mock()
        for body in (b"not json", b"[]", b"{}", b"x" * 65537,
                     json.dumps({**DEFAULT_PROFILE, "behavior": True}).encode(),
                     json.dumps({**DEFAULT_PROFILE, "persona": "x" * 2001}).encode()):
            client.opener.open.return_value = io.BytesIO(body)
            with patch("sys.stderr", new_callable=io.StringIO) as log:
                with self.assertRaisesRegex(RuntimeError, "Profile service unavailable"):
                    client.prepare("42")
            self.assertNotIn(body.decode(), log.getvalue())
            self.assertNotIn(SECRET, log.getvalue())

    def test_credentials_are_not_forwarded_through_redirects_or_url_userinfo(self):
        self.assertIsNone(NoRedirects().redirect_request(None, None, None, None, None, None))
        for url in ("file:///etc/private", "http://user:pass@mcp:8080", "http://mcp:8080?token=bad"):
            with self.assertRaises(ValueError):
                ProfileClient(url, SECRET)
