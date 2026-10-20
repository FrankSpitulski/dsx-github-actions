# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import copy
import io
import json
import unittest
from unittest.mock import Mock
import zipfile

from test_promotion import COMMIT, RC, archive, charts, config, evidence, p


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.github = p.GitHub("example/product")
        self.github.tag = Mock(return_value=COMMIT)
        self.github.release = Mock(return_value=None)
        self.manifest = evidence(archive())
        self.run = {
            "id": 12, "run_attempt": 1, "head_sha": COMMIT,
            "head_branch": "release/1.2.3", "event": "push", "status": "completed",
            "conclusion": "success", "path": ".github/workflows/release-rc.yml",
            "head_repository": {"full_name": "example/product"},
        }
        self.artifact = {
            "id": 42, "name": f"rc-promotion-manifest-{RC}-1", "expired": False,
            "workflow_run": {"id": 12, "head_sha": COMMIT},
        }
        self.repack()
        self.release = {"tag_name": f"v{RC}", "draft": False, "prerelease": True}
        self.github.request = Mock(side_effect=self.request)
        self.github.pages = Mock(side_effect=self.pages)

    def repack(self, extra=False):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as zipped:
            zipped.writestr("rc-promotion-manifest.json", p.canonical(self.manifest))
            if extra:
                zipped.writestr("unexpected", b"extra")
        self.raw = output.getvalue()
        self.artifact.update(digest=charts.digest(self.raw), size_in_bytes=len(self.raw))

    def request(self, path, **kwargs):
        if "/releases/tags/" in path:
            return self.release
        if path.endswith("/actions/artifacts/42/zip"):
            self.assertTrue(kwargs["binary"])
            return self.raw
        if path.endswith("/actions/runs/12/attempts/1"):
            return self.run
        self.fail(path)

    def pages(self, path, key):
        if path.startswith("actions/workflows/release-rc.yml/runs?"):
            return [self.run]
        if path == "actions/runs/12/artifacts":
            return [self.artifact]
        self.fail(path)

    def test_exact_successful_run_and_manifest(self):
        self.assertEqual(self.github.evidence(config(), RC), self.manifest)

    def test_wrong_run_identity_status_or_origin(self):
        for field, value in [
            ("head_sha", "b" * 40), ("head_branch", "main"), ("event", "pull_request"),
            ("status", "in_progress"), ("conclusion", "failure"),
            ("path", ".github/workflows/other.yml"),
            ("head_repository", {"full_name": "fork/product"}),
        ]:
            with self.subTest(field=field):
                self.setUp()
                self.run[field] = value
                with self.assertRaises(p.PromotionError):
                    self.github.evidence(config(), RC)

    def test_artifact_provenance_size_expiry_and_digest(self):
        for field, value in [
            ("expired", True), ("name", "wrong"), ("digest", "sha256:" + "0" * 64),
            ("workflow_run", {"id": 99, "head_sha": COMMIT}),
            ("size_in_bytes", 1024 * 1024 + 1), ("size_in_bytes", -1),
        ]:
            with self.subTest(field=field):
                self.setUp()
                self.artifact[field] = value
                with self.assertRaises(p.PromotionError):
                    self.github.evidence(config(), RC)

    def test_extra_archive_members_and_wrong_manifest_are_rejected(self):
        self.repack(extra=True)
        with self.assertRaises(p.PromotionError):
            self.github.evidence(config(), RC)
        self.manifest["run_attempt"] = 99
        self.repack()
        with self.assertRaises(p.PromotionError):
            self.github.evidence(config(), RC)

    def test_resume_uses_original_run_attempt_not_a_newer_rc_run(self):
        intent = {"rc_version": RC, "commit": COMMIT, "source_run_id": 12, "source_run_attempt": 1}
        self.github.release.return_value = {
            "body": "Verified RC promotion intent.\n\n```json\n" +
                    p.canonical(intent).decode() + "```\n",
        }
        self.assertEqual(self.github.evidence(config(), RC), self.manifest)
        self.github.request.assert_any_call("repos/example/product/actions/runs/12/attempts/1")
        self.assertEqual(self.github.pages.call_count, 1)

    def test_missing_tag_or_nonprerelease_fails(self):
        self.github.tag.return_value = None
        with self.assertRaises(p.PromotionError):
            self.github.evidence(config(), RC)
        self.github.tag.return_value = COMMIT
        self.release["prerelease"] = False
        with self.assertRaises(p.PromotionError):
            self.github.evidence(config(), RC)


class ReservationTests(unittest.TestCase):
    def setUp(self):
        self.github = p.GitHub("example/product")
        self.intent = {"rc_version": RC, "commit": COMMIT, "source_run_id": 12}
        self.github.tag = Mock(return_value=None)
        self.github.release = Mock(return_value=None)
        self.github.read_asset = Mock(return_value=None)
        self.github.upload = Mock()
        self.github.request = Mock(side_effect=self.create)
        self.created = None

    def create(self, path, method, data):
        self.assertEqual(path, "repos/example/product/releases")
        self.assertEqual(method, "POST")
        self.created = {"id": 5, **copy.deepcopy(data)}
        return self.created

    def test_atomic_intent_survives_failed_asset_upload(self):
        self.github.upload.side_effect = p.PromotionError("upload failed")
        with self.assertRaises(p.PromotionError):
            self.github.reserve("1.2.3", COMMIT, self.intent)
        self.assertEqual(self.github.intent(self.created), self.intent)
        self.assertTrue(self.created["draft"])
        self.github.release.return_value = self.created
        self.github.upload.side_effect = None
        self.assertEqual(self.github.reserve("1.2.3", COMMIT, self.intent), self.created)
        self.assertEqual(self.github.request.call_count, 1)

    def test_conflicting_tag_or_stored_intent_never_writes(self):
        self.github.tag.return_value = COMMIT
        with self.assertRaises(p.PromotionError):
            self.github.reserve("1.2.3", COMMIT, self.intent)
        self.github.request.assert_not_called()
        self.github.tag.return_value = None
        self.github.reserve("1.2.3", COMMIT, self.intent)
        self.github.release.return_value = self.created
        self.github.upload.reset_mock()
        with self.assertRaises(p.PromotionError):
            self.github.reserve("1.2.3", COMMIT, {**self.intent, "rc_version": "1.2.3-rc.8"})
        self.github.upload.assert_not_called()

    def test_different_intent_asset_rejects(self):
        self.github.reserve("1.2.3", COMMIT, self.intent)
        self.github.release.return_value = self.created
        self.github.read_asset.return_value = {"different": "intent"}
        with self.assertRaises(p.PromotionError):
            self.github.reserve("1.2.3", COMMIT, self.intent)

    def test_finish_creates_exact_tag_before_publishing(self):
        self.github.request = Mock()
        self.github.finish({"id": 5, "tag_name": "v1.2.3", "draft": True}, COMMIT)
        calls = self.github.request.call_args_list
        self.assertEqual(calls[0].args, (
            "repos/example/product/git/refs", "POST",
            {"ref": "refs/tags/v1.2.3", "sha": COMMIT},
        ))
        self.assertEqual(calls[1].args, (
            "repos/example/product/releases/5", "PATCH",
            {"draft": False, "prerelease": False, "make_latest": "false"},
        ))


if __name__ == "__main__":
    unittest.main()
