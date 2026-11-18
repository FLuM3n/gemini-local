import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import bootstrap
import gemini_auth as auth
import gemini_browser_link as browser
import gemini_terminal as terminal


class PlatformTests(unittest.TestCase):
    def test_mac_application_data_path(self):
        with patch.object(terminal.sys, "platform", "darwin"):
            self.assertEqual(terminal.default_home(), Path.home()/"Library/Application Support/GeminiTerminal")

    def test_mac_credentials_use_keychain_not_plaintext_files(self):
        store = {}
        backend = NS(set_password=lambda service, account, value: store.update({(service, account): value}),
                     get_password=lambda service, account: store.get((service, account)))
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(auth.sys, "platform", "darwin"), patch.object(auth, "_mac_keyring", return_value=backend):
                cookies = {"__Secure-1PSID": "dummy-cookie-value-for-test"}
                auth.save_session(home, cookies)
                self.assertEqual(auth.load_session(home), cookies)
                self.assertFalse(auth.session_path(home).exists())
                self.assertNotEqual(auth._keychain_account(home), auth._keychain_account(home/"another-profile"))

    def test_keychain_failure_does_not_create_plaintext_session(self):
        def fail(*args): raise RuntimeError("dummy-sensitive-value")
        backend = NS(set_password=fail, get_password=fail)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(auth.sys, "platform", "darwin"), patch.object(auth, "_mac_keyring", return_value=backend):
                with self.assertRaises(auth.LoginError) as result:
                    auth.save_session(home, {"__Secure-1PSID": "dummy-sensitive-value"})
                self.assertNotIn("dummy-sensitive-value", str(result.exception))
                self.assertFalse(list(home.iterdir()))
                with self.assertRaises(auth.LoginError): auth.load_session(home)

    def test_mac_opens_a_browser_compatible_with_the_extension(self):
        with patch.object(browser.sys, "platform", "darwin"), patch.object(Path, "is_dir", side_effect=lambda: True), patch.object(browser.subprocess, "run", return_value=NS(returncode=0)) as run:
            self.assertTrue(browser.open_login_browser("http://127.0.0.1:1234/gemini-auth#test"))
            self.assertEqual(run.call_args.args[0][:3], ["/usr/bin/open", "-a", str(Path("/Applications")/"Google Chrome.app")])

    @unittest.skipUnless(sys.platform == "win32", "Windows native DPAPI")
    def test_native_windows_session_roundtrip_preserves_legacy_format(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)/"session with spaces"
            cookies = {"__Secure-1PSID": "dummy-windows-session-cookie"}
            auth.save_session(home, cookies)
            self.assertNotIn(cookies["__Secure-1PSID"].encode(), auth.session_path(home).read_bytes())
            self.assertEqual(auth.load_session(home), cookies)

    @unittest.skipUnless(sys.platform == "darwin" and os.environ.get("GEMINI_TEST_KEYCHAIN"), "macOS native test keychain")
    def test_native_macos_keychain_session_roundtrip(self):
        backend = auth._mac_keyring()
        backend.keychain = os.environ["GEMINI_TEST_KEYCHAIN"]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.object(auth, "_mac_keyring", return_value=backend):
                try:
                    cookies = {"__Secure-1PSID": "dummy-macos-session-cookie"}
                    auth.save_session(home, cookies)
                    self.assertEqual(auth.load_session(home), cookies)
                    self.assertFalse(auth.session_path(home).exists())
                finally:
                    try: backend.delete_password("GeminiLocal", auth._keychain_account(home))
                    except Exception: pass

    def test_dependency_bootstrap_runs_once_and_passes_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/"requirements.txt").write_text("dummy-requirement==1\n", encoding="utf-8")
            interpreter = root/".venv"/("Scripts/python.exe" if os.name == "nt" else "bin/python")
            def create(environment):
                interpreter.parent.mkdir(parents=True)
                interpreter.write_text("test interpreter", encoding="utf-8")
            with patch.object(bootstrap, "__file__", str(root/"bootstrap.py")), patch.object(bootstrap.venv, "EnvBuilder", return_value=NS(create=create)), patch.object(bootstrap.subprocess, "run", return_value=NS(returncode=0)) as install, patch.object(bootstrap.os, "execv") as execute:
                self.assertEqual(bootstrap.main(["--port", "8101"]), 0)
                self.assertEqual(bootstrap.main(["--port", "8101"]), 0)
                self.assertEqual(install.call_count, 1)
                self.assertEqual(execute.call_args.args[1][-2:], ["--port", "8101"])

    def test_vendored_sdk_has_expected_model_and_image_interfaces(self):
        import gemini_webapi
        from gemini_webapi import GeminiClient
        self.assertIn("vendor", Path(gemini_webapi.__file__).parts)
        self.assertTrue(callable(GeminiClient.generate_content_stream))
        self.assertTrue(callable(GeminiClient.list_models))


if __name__ == "__main__": unittest.main(verbosity=2)
