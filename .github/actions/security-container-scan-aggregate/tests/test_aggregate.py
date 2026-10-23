# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ACTION = Path(__file__).resolve().parents[1]


class AggregateTests(unittest.TestCase):
    def render(self, reports):
        with tempfile.TemporaryDirectory() as root:
            for name, severities in reports.items():
                directory = Path(root) / name
                directory.mkdir()
                (directory / "grype-results.json").write_text(json.dumps({
                    "matches": [{"vulnerability": {"severity": severity}}
                                for severity in severities],
                }))
            result = subprocess.run(
                [sys.executable, str(ACTION / "aggregate.py"), "--dir", root],
                check=True, capture_output=True, text=True,
            )
            return result.stdout

    def test_no_artifacts(self):
        self.assertIn("No Grype artifacts were found", self.render({}))

    def test_single_artifact_keeps_service_and_high_findings(self):
        output = self.render({"grype-api-123-1": ["High", "High"]})
        self.assertIn("| api | 2 | 0 | 2 | 0 | 0 | 0 |", output)
        self.assertIn("| **TOTAL** | **2** | **0** | **2**", output)
        self.assertNotIn("No Grype artifacts", output)

    def test_multiple_artifacts_keep_each_service(self):
        output = self.render({
            "grype-worker-123-1": ["High", "Medium"],
            "grype-api-123-1": ["Critical"],
        })
        self.assertIn("| api | 1 | 1 | 0 | 0 | 0 | 0 |", output)
        self.assertIn("| worker | 2 | 0 | 1 | 1 | 0 | 0 |", output)
        self.assertIn("| **TOTAL** | **3** | **1** | **1** | **1**", output)
        self.assertLess(output.index("| api |"), output.index("| worker |"))

    def test_download_and_upload_keep_the_directory_layout_contract(self):
        def action(path):
            result = subprocess.run(
                ["yq", "-o=json", ".", str(path)],
                check=True, capture_output=True, text=True,
            )
            return json.loads(result.stdout)

        download = action(ACTION / "action.yml")["runs"]["steps"][0]
        self.assertEqual(download["uses"],
                         "actions/download-artifact@d3f86a106a0bac45b974a628896c90dbdf5c8093")
        self.assertNotIn("merge-multiple", download["with"])
        producer = action(ACTION.parent / "security-container-scan/action.yml")
        upload = next(step for step in producer["runs"]["steps"]
                      if step.get("name") == "Upload Grype reports")
        self.assertEqual(upload["uses"],
                         "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02")


if __name__ == "__main__":
    unittest.main()
