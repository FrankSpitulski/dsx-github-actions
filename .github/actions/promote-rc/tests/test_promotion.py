# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import charts
import promotion as p

COMMIT = "a" * 40
IMAGE = "sha256:" + "b" * 64
RC = "1.2.3-rc.7"


def config():
    return {
        "schema": 1, "enabled": True,
        "source_workflow": ".github/workflows/release-rc.yml",
        "source_ngc_path": "example/components-dev",
        "destination_ngc_path": "example/components-dev",
        "images": ["service"],
        "charts": [{"name": "service", "version_value_paths": [["image", "tag"]]}],
        "competing_workflows": [".github/workflows/release.yml"],
    }


def packed(files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
    return data.getvalue()


def archive(files=None):
    default = {
        "service/Chart.yaml": charts.encoded({
            "apiVersion": "v2", "name": "service", "version": RC, "appVersion": RC,
            "annotations": {"dsx.nvidia.com/source-revision": COMMIT, "existing": "kept"},
            "dependencies": [{"name": "child", "version": RC, "repository": "file://../child"}],
        }),
        "service/values.yaml": charts.encoded({
            "image": {"repository": "nvcr.io/example/components-dev/service", "tag": RC},
            "unrelated": RC,
        }),
        "service/templates/pod.yaml": (
            b"apiVersion: v1\nkind: Pod\nmetadata:\n  name: service\n"
            b"spec:\n  containers:\n    - name: service\n"
            b"      image: {{ .Values.image.repository }}:{{ .Values.image.tag }}\n"
        ),
        "service/Chart.lock": b'{"dependencies":[{"name":"child","version":"1.2.3-rc.7"}]}\n',
        "service/charts/child-1.2.3-rc.7.tgz": packed({
            "child/Chart.yaml": charts.encoded({
                "apiVersion": "v2", "name": "child", "version": RC, "appVersion": RC,
            }),
            "child/values.yaml": charts.encoded({
                "image": {"repository": "nvcr.io/example/components-dev/service", "tag": RC},
            }),
            "child/templates/pod.yaml": (
                b"apiVersion: v1\nkind: Pod\nmetadata:\n  name: child\n"
                b"spec:\n  containers:\n    - name: child\n"
                b"      image: {{ .Values.image.repository }}:{{ .Values.image.tag }}\n"
            ),
        }),
        "service/README.md": b"release notes\n",
    }
    return packed(files or default)


def evidence(data):
    return {
        "schema": 1, "repository": "example/product", "rc_version": RC, "commit": COMMIT,
        "run_id": 12, "run_attempt": 1, "source_workflow": ".github/workflows/release-rc.yml",
        "source_ngc_path": "example/components-dev",
        "images": [{"name": "service", "digest": IMAGE}],
        "charts": [{"name": "service", "digest": charts.digest(data),
                    "version_value_paths": [["image", "tag"]]}],
    }


class MemoryRegistry:
    def __init__(self, data):
        self.source_chart = data
        self.source_image = IMAGE
        self.images = {}
        self.charts = {}
        self.writes = []
        self.fail_chart = False
        self.bad_copy = False
        self.fail_read = False

    def image_digest(self, namespace, name, version, absent=False):
        if self.fail_read:
            raise p.PromotionError("registry unavailable")
        if version == RC:
            return self.source_image
        value = self.images.get((name, version))
        if value is None and not absent:
            raise p.PromotionError("missing image")
        return value

    def verify_image(self, name, digest, commit):
        p.require(digest == self.source_image and commit == COMMIT, "wrong image provenance")

    def copy_image(self, name, digest, stable):
        self.writes.append(("image", name, digest, stable))
        self.images[name, stable] = "sha256:" + "c" * 64 if self.bad_copy else digest

    def chart(self, namespace, name, version, absent=False):
        if version == RC:
            return self.source_chart
        value = self.charts.get((name, version))
        if value is None and not absent:
            raise p.PromotionError("missing chart")
        return value

    def chart_destination(self, name):
        pass

    def publish_chart(self, name, stable, data):
        if self.fail_chart:
            raise p.PromotionError("chart upload failed")
        self.writes.append(("chart", name, stable))
        self.charts[name, stable] = data


class MemoryGitHub:
    def __init__(self):
        self.reservation = None
        self.reports = []
        self.finished = False
        self.rc_commit = COMMIT

    def reserve(self, stable, commit, intent):
        if self.reservation:
            p.require(self.reservation == intent, "different promotion intent")
        self.reservation = copy.deepcopy(intent)
        return {"tag_name": f"v{stable}"}

    def upload(self, release, name, report):
        self.reports.append((name, copy.deepcopy(report)))

    def tag(self, name):
        return self.rc_commit

    def finish(self, release, commit):
        self.finished = True


class VersionTests(unittest.TestCase):
    def test_exact_rc(self):
        for value, stable in [("0.0.0-rc.1", "0.0.0"), (RC, "1.2.3"), ("12.34.56-rc.123", "12.34.56")]:
            self.assertEqual(p.version(value), stable)

    def test_noncanonical_versions(self):
        for value in ["v" + RC, "1.2.3", "1.2.3-rc.0", "01.2.3-rc.1", "1.2.3-rc.01",
                      RC + "\n", RC + "+build", "1.2.3-beta.1", "../1.2.3-rc.1", "", None]:
            with self.subTest(value=value), self.assertRaises(p.PromotionError):
                p.version(value)


class PolicyTests(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(p.policy(config()), config())

    def test_charts_reject_namespace_changes_including_no_declared_version_paths(self):
        for paths in ([["image", "tag"]], []):
            candidate = config()
            candidate["destination_ngc_path"] = "example/components"
            candidate["charts"][0]["version_value_paths"] = paths
            with self.subTest(paths=paths), self.assertRaisesRegex(
                p.PromotionError, "identical source and destination NGC paths"
            ):
                p.policy(candidate)

    def test_image_only_promotion_allows_distinct_namespaces(self):
        candidate = config()
        candidate["destination_ngc_path"] = "example/components"
        candidate["charts"] = []
        self.assertEqual(p.policy(candidate), candidate)

    def test_disabled_or_unknown_inputs(self):
        for field, value in [("enabled", False), ("enabled", "true"), ("schema", True),
                             ("environment", "ungated"), ("source_ngc_path", "../escape"),
                             ("source_workflow", "../release.yml"),
                             ("images", ["service", "service"]),
                             ("images", ["service;command"]), ("competing_workflows", ["../x"])]:
            with self.subTest(field=field):
                value_ = config()
                value_[field] = value
                with self.assertRaises(p.PromotionError):
                    p.policy(value_)

    def test_bad_value_paths(self):
        for value in [[[]], [["image", "tag"], ["image", "tag"]], [["a.b"]], [["x\n"]]]:
            with self.subTest(value=value), self.assertRaises(p.PromotionError):
                p.value_paths(value)

    def test_incomplete_manifest(self):
        data = archive()
        original = evidence(data)
        run = {"id": 12, "run_attempt": 1}
        self.assertEqual(p.manifest(original, config(), "example/product", RC, COMMIT, run), original)
        for field, value in [("images", []), ("charts", []), ("run_id", 99),
                             ("run_attempt", 2), ("repository", "fork/product"),
                             ("commit", "b" * 40), ("source_ngc_path", "other/team")]:
            candidate = copy.deepcopy(original)
            candidate[field] = value
            with self.subTest(field=field), self.assertRaises(p.PromotionError):
                p.manifest(candidate, config(), "example/product", RC, COMMIT, run)

    def test_manifest_unexpected_duplicate_and_changed_chart_policy(self):
        for change in ("extra", "duplicate", "paths"):
            candidate = evidence(archive())
            if change == "extra":
                candidate["images"].append({"name": "unexpected", "digest": IMAGE})
            elif change == "duplicate":
                candidate["images"] *= 2
            else:
                candidate["charts"][0]["version_value_paths"] = []
            with self.subTest(change=change), self.assertRaises(p.PromotionError):
                p.manifest(candidate, config(), "example/product", RC, COMMIT,
                           {"id": 12, "run_attempt": 1})


class ChartTests(unittest.TestCase):
    def test_metadata_only_promotion_and_deterministic_bytes(self):
        source = archive()
        before = charts.unpack(source, "service")
        changed = charts.promote(source, "service", RC, "1.2.3", COMMIT,
                                 [["image", "tag"]], IMAGE)
        self.assertNotEqual(charts.digest(source), charts.digest(changed))
        self.assertEqual(changed, charts.promote(source, "service", RC, "1.2.3", COMMIT,
                                               [["image", "tag"]], IMAGE))
        after = charts.unpack(changed, "service")
        self.assertEqual(set(before), set(after))
        for path in before:
            if path not in {"service/Chart.yaml", "service/values.yaml"}:
                self.assertEqual(before[path], after[path], path)
        metadata = charts.yaml_object(after["service/Chart.yaml"][0])
        previous = charts.yaml_object(before["service/Chart.yaml"][0])
        self.assertEqual(metadata["version"], "1.2.3")
        self.assertEqual(metadata["appVersion"], "1.2.3")
        self.assertEqual(metadata["dependencies"], previous["dependencies"])
        self.assertEqual(metadata["annotations"]["dsx.nvidia.com/source-revision"], COMMIT)
        self.assertEqual(metadata["annotations"]["dsx.nvidia.com/source-chart-digest"], charts.digest(source))
        self.assertEqual(metadata["annotations"]["existing"], "kept")
        values = charts.yaml_object(after["service/values.yaml"][0])
        self.assertEqual(values["image"]["tag"], "1.2.3")
        self.assertEqual(values["unrelated"], RC)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service-1.2.3.tgz"
            path.write_bytes(changed)
            result = subprocess.run(["helm", "show", "chart", str(path)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(charts.yaml_object(result.stdout)["version"], "1.2.3")

    def test_render_retains_vendored_rc_image_and_root_repository(self):
        changed = charts.promote(archive(), "service", RC, "1.2.3", COMMIT,
                                 [["image", "tag"]], IMAGE)
        after = charts.unpack(changed, "service")
        values = charts.yaml_object(after["service/values.yaml"][0])
        self.assertEqual(values["image"]["repository"], "nvcr.io/example/components-dev/service")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "service-1.2.3.tgz"
            path.write_bytes(changed)
            result = subprocess.run(
                ["helm", "template", "promoted", str(path)], capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            references = [line.strip() for line in result.stdout.decode().splitlines()
                          if line.strip().startswith("image:")]
            self.assertCountEqual(references, [
                "image: nvcr.io/example/components-dev/service:1.2.3",
                f"image: nvcr.io/example/components-dev/service:{RC}",
            ])

    def test_missing_or_wrong_version_path(self):
        for paths in [[["missing"]], [["image", "repository"]], [["image", "tag", "child"]]]:
            with self.subTest(paths=paths), self.assertRaises(charts.InvalidChart):
                charts.promote(archive(), "service", RC, "1.2.3", COMMIT, paths, IMAGE)

    def test_wrong_source_revision_or_version(self):
        for rc, commit in [(RC, "b" * 40), ("1.2.3-rc.8", COMMIT)]:
            with self.subTest(rc=rc), self.assertRaises(charts.InvalidChart):
                charts.promote(archive(), "service", rc, "1.2.3", commit, [], IMAGE)

    def test_unsafe_paths_links_duplicates(self):
        for name in ["../Chart.yaml", "/service/Chart.yaml", "service/../Chart.yaml",
                     "service/./Chart.yaml", "other/Chart.yaml", "service\\Chart.yaml"]:
            with self.subTest(name=name), self.assertRaises(charts.InvalidChart):
                charts.unpack(archive({name: b"bad"}), "service")
        for mode in ["link", "duplicate"]:
            data = io.BytesIO()
            with tarfile.open(fileobj=data, mode="w:gz") as tar:
                info = tarfile.TarInfo("service/Chart.yaml")
                if mode == "link":
                    info.type = tarfile.SYMTYPE
                    info.linkname = "../../outside"
                tar.addfile(info)
                if mode == "duplicate":
                    tar.addfile(info)
            with self.subTest(mode=mode), self.assertRaises(charts.InvalidChart):
                charts.unpack(data.getvalue(), "service")


class PromotionTests(unittest.TestCase):
    def setUp(self):
        self.data = archive()
        self.evidence = evidence(self.data)
        self.registry = MemoryRegistry(self.data)
        self.github = MemoryGitHub()

    def promote(self, attempt="23-1", path=None):
        return p.promote(config(), self.evidence, self.github, self.registry, attempt, path)

    def test_complete_promotion_and_matching_rerun(self):
        first = self.promote()
        self.assertEqual(first["status"], "completed")
        self.assertTrue(self.github.finished)
        self.assertEqual(self.registry.images["service", "1.2.3"], IMAGE)
        self.assertEqual(len(self.registry.writes), 2)
        self.assertEqual(self.promote("23-2")["intent"], first["intent"])
        self.assertEqual(len(self.registry.writes), 2)

    def test_cross_namespace_charts_fail_before_any_io(self):
        candidate = config()
        candidate["destination_ngc_path"] = "example/components"
        with self.assertRaisesRegex(p.PromotionError, "identical source and destination NGC paths"):
            p.promote(candidate, self.evidence, None, None, "23-1")

    def test_partial_failure_retains_intent_and_resumes(self):
        self.registry.fail_chart = True
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "result.json"
            with self.assertRaises(p.PromotionError):
                self.promote(path=report)
            saved = json.loads(report.read_text())
            self.assertEqual(saved["status"], "partial")
            self.assertEqual(len(saved["artifacts"]), 1)
        self.assertFalse(self.github.finished)
        self.assertIsNotNone(self.github.reservation)
        self.assertEqual(self.github.reports[-1][1]["status"], "partial")
        self.registry.fail_chart = False
        self.assertEqual(self.promote("23-2")["status"], "completed")
        self.assertEqual([item[0] for item in self.registry.writes], ["image", "chart"])

    def test_preflight_failure_never_reserves_or_writes(self):
        for failure in ["source_image", "source_chart", "stable_image", "stable_chart", "registry"]:
            with self.subTest(failure=failure):
                self.setUp()
                if failure == "source_image":
                    self.registry.source_image = "sha256:" + "c" * 64
                elif failure == "source_chart":
                    self.registry.source_chart = b"changed"
                elif failure == "stable_image":
                    self.registry.images["service", "1.2.3"] = "sha256:" + "c" * 64
                elif failure == "stable_chart":
                    self.registry.charts["service", "1.2.3"] = self.data
                else:
                    self.registry.fail_read = True
                with self.assertRaises(p.PromotionError):
                    self.promote()
                self.assertIsNone(self.github.reservation)
                self.assertFalse(self.registry.writes)

    def test_digest_conversion_never_finishes_release(self):
        self.registry.bad_copy = True
        with self.assertRaises(p.PromotionError):
            self.promote()
        self.assertFalse(self.github.finished)
        self.assertFalse(self.registry.charts)

    def test_rc_tag_movement_never_finishes_release(self):
        self.github.rc_commit = "b" * 40
        with self.assertRaises(p.PromotionError):
            self.promote()
        self.assertFalse(self.github.finished)

    def test_conflicting_reserved_intent_rejects_without_write(self):
        self.github.reservation = {"other": "RC"}
        with self.assertRaises(p.PromotionError):
            self.promote()
        self.assertFalse(self.registry.writes)


class CaptureTests(unittest.TestCase):
    def test_captures_exact_inventory_without_publishing(self):
        data = archive()
        expected = evidence(data)
        context = {key: expected[key] for key in (
            "repository", "rc_version", "commit", "run_id", "run_attempt", "source_workflow",
        )}
        registry = MemoryRegistry(data)
        self.assertEqual(p.capture(config(), registry, context), expected)
        self.assertEqual(registry.writes, [])

    def test_changed_chart_metadata_prevents_capture(self):
        context = {
            "repository": "example/product", "rc_version": RC, "commit": COMMIT,
            "run_id": 12, "run_attempt": 1, "source_workflow": ".github/workflows/release-rc.yml",
        }
        registry = MemoryRegistry(archive())
        registry.source_chart = archive({"service/Chart.yaml": b"name: wrong\n"})
        with self.assertRaises(charts.InvalidChart):
            p.capture(config(), registry, context)
        self.assertEqual(registry.writes, [])


class GateTests(unittest.TestCase):
    def setUp(self):
        self.github = p.GitHub("example/product")
        self.context = {"event": "workflow_dispatch", "ref": "refs/heads/main",
                        "sha": COMMIT, "protected": "true"}
        self.rules = {
            "can_admins_bypass": False,
            "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
            "protection_rules": [{"type": "required_reviewers", "prevent_self_review": True,
                                  "reviewers": [{"type": "Team", "reviewer": {"slug": "example-team"}}]}],
        }
        self.workflow = {"state": "disabled_manually"}
        self.definition = {
            "concurrency": {"group": "dsx-stable-release-${{ github.repository }}",
                            "cancel-in-progress": False},
            "jobs": {"release": {"if": "vars.DSX_RELEASE_MODE != 'rc-promotion'"}},
        }
        self.branch = {"protected": True, "commit": {"sha": COMMIT}}
        self.policies = [{"name": "main", "type": "branch"}]
        self.github.request = self.request
        self.github.pages = lambda *args: self.policies

    def request(self, path, *args, **kwargs):
        if path.endswith("/product"):
            return {"default_branch": "main"}
        if "/branches/" in path:
            return self.branch
        if "/environments/" in path:
            return self.rules
        if "/actions/workflows/" in path:
            return self.workflow
        if "/contents/" in path:
            import base64
            return {"encoding": "base64",
                    "content": base64.b64encode(json.dumps(self.definition).encode()).decode()}
        raise AssertionError(path)

    def test_valid_gate(self):
        self.github.gates(config(), self.context)

    def test_reject_events_and_source_refs(self):
        for key, value in [("event", "push"), ("event", "pull_request"),
                           ("ref", "refs/heads/release/1.2.3"), ("protected", "false")]:
            context = {**self.context, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(p.PromotionError):
                self.github.gates(config(), context)

    def test_reject_incomplete_environment_protection(self):
        for mode in ["none", "bypass", "self", "user", "extra_branch"]:
            with self.subTest(mode=mode):
                self.setUp()
                if mode == "none":
                    self.rules["protection_rules"] = []
                elif mode == "bypass":
                    self.rules["can_admins_bypass"] = True
                elif mode == "self":
                    self.rules["protection_rules"][0]["prevent_self_review"] = False
                elif mode == "user":
                    self.rules["protection_rules"][0]["reviewers"][0]["type"] = "User"
                else:
                    self.policies.append({"name": "release/*", "type": "branch"})
                with self.assertRaises(p.PromotionError):
                    self.github.gates(config(), self.context)

    def test_enabled_competing_release_and_stale_default(self):
        self.workflow["state"] = "active"
        self.definition["jobs"]["release"].pop("if")
        with self.assertRaises(p.PromotionError):
            self.github.gates(config(), self.context)
        self.workflow["state"] = "disabled_manually"
        self.branch["commit"]["sha"] = "b" * 40
        with self.assertRaises(p.PromotionError):
            self.github.gates(config(), self.context)

    def test_active_legacy_publisher_requires_common_guard_and_concurrency(self):
        self.workflow["state"] = "active"
        self.github.gates(config(), self.context)
        for section, value in [
            ("jobs", {"release": {"if": "true"}}),
            ("jobs", {"release": self.definition["jobs"]["release"], "unguarded": {}}),
            ("concurrency", {"group": "different", "cancel-in-progress": False}),
            ("concurrency", {"group": "dsx-stable-release-${{ github.repository }}",
                             "cancel-in-progress": True}),
        ]:
            before = copy.deepcopy(self.definition)
            self.definition[section] = value
            with self.subTest(section=section, value=value), self.assertRaises(p.PromotionError):
                self.github.gates(config(), self.context)
            self.definition = before


class ToolTests(unittest.TestCase):
    def test_registry_absence_is_narrow(self):
        registry = p.Registry("example/components-dev", "example/components")
        ref = "nvcr.io/example/components/service:1.2.3"
        env = {"DESTINATION_DOCKER_CONFIG": "/tmp/no-credentials"}
        with patch.dict(os.environ, env):
            for message, absent in [(f"ERROR: {ref}: not found", True),
                                    ("unauthorized: not found", False),
                                    ("proxy HTTP 404", False)]:
                result = subprocess.CompletedProcess([], 1, b"", message.encode())
                with patch("subprocess.run", return_value=result), self.subTest(message=message):
                    if absent:
                        self.assertIsNone(registry.image_digest("example/components", "service", "1.2.3", True))
                    else:
                        with self.assertRaises(p.PromotionError):
                            registry.image_digest("example/components", "service", "1.2.3", True)

    def test_registry_digest_uses_descriptor_not_display_newline(self):
        with patch.dict(os.environ, {"SOURCE_DOCKER_CONFIG": "/tmp/no-credentials"}):
            outputs = [
                subprocess.CompletedProcess([], 0, b'{"schemaVersion":2}\n', b""),
                subprocess.CompletedProcess([], 0, json.dumps({"digest": IMAGE}).encode(), b""),
            ]
            with patch("subprocess.run", side_effect=outputs):
                self.assertEqual(p.Registry("example/components-dev").image_digest(
                    "example/components-dev", "service", RC), IMAGE)

    def test_warning_is_not_absence(self):
        result = subprocess.CompletedProcess([], 0, b"[]", b"cannot load index")
        with patch("subprocess.run", return_value=result), self.assertRaises(p.PromotionError):
            p.Registry("example/components-dev").chart("example/components-dev", "service", RC, True)


if __name__ == "__main__":
    unittest.main()
