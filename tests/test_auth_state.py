from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from paper_review_service.auth_state import auth_workspace, recover_auth_workspaces, sync_refreshed_auth


class AuthRefreshTests(unittest.TestCase):
    def test_refresh_preserved_and_existing_host_update_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory) / "auth.json"
            snapshot = Path(directory) / "snapshot.json"
            original = b'{"tokens":{"access_token":"synthetic-old"}}'
            refreshed = b'{"tokens":{"access_token":"synthetic-refreshed"}}'
            digest = hashlib.sha256(original).hexdigest()
            host.write_bytes(original)
            snapshot.write_bytes(original)
            self.assertEqual(sync_refreshed_auth(host, snapshot, digest), "unchanged")
            snapshot.write_bytes(refreshed)
            self.assertEqual(sync_refreshed_auth(host, snapshot, digest), "refreshed")
            self.assertEqual(host.read_bytes(), refreshed)
            self.assertEqual(host.stat().st_mode & 0o777, 0o600)
            concurrent = b'{"tokens":{"access_token":"synthetic-host-login"}}'
            host.write_bytes(concurrent)
            self.assertEqual(sync_refreshed_auth(host, snapshot, digest), "host_changed")
            self.assertEqual(host.read_bytes(), concurrent)

    def test_invalid_refreshed_document_does_not_replace_host(self):
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory) / "auth.json"
            snapshot = Path(directory) / "snapshot.json"
            original = b'{"tokens":{}}'
            host.write_bytes(original)
            snapshot.write_bytes(b"invalid")
            with self.assertRaises(ValueError):
                sync_refreshed_auth(host, snapshot, hashlib.sha256(original).hexdigest())
            self.assertEqual(host.read_bytes(), original)

    def test_crash_retained_workspace_recovers_refreshed_auth(self):
        class ContainerStillRunning(RuntimeError):
            preserve_auth = True

        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            host = parent / "auth.json"
            original = b'{"test":"old"}'
            refreshed = b'{"test":"new"}'
            host.write_bytes(original)
            identifier = "a" * 32
            with self.assertRaises(ContainerStillRunning):
                with auth_workspace(parent, identifier) as private:
                    (private / "auth.json").write_bytes(refreshed)
                    (private / "initial-sha256").write_text(hashlib.sha256(original).hexdigest())
                    raise ContainerStillRunning()
            self.assertTrue(private.exists())
            recover_auth_workspaces(parent, identifier, host)
            self.assertFalse(private.exists())
            self.assertEqual(host.read_bytes(), refreshed)


if __name__ == "__main__":
    unittest.main()
