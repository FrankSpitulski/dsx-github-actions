# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import unittest

from test_promotion import charts

ROOT = Path(__file__).resolve().parents[3]


class WorkflowTests(unittest.TestCase):
    def test_only_manual_version_input_and_fixed_publishing_gate(self):
        workflow = charts.yaml_object((ROOT / "workflows/promote-rc.yml").read_bytes())
        self.assertEqual(set(workflow["on"]), {"workflow_call"})
        call = workflow["on"]["workflow_call"]
        self.assertEqual(set(call), {"inputs"})
        self.assertEqual(set(call["inputs"]), {"rc_version"})
        self.assertEqual(workflow["concurrency"], {
            "group": "dsx-stable-release-${{ github.repository }}", "cancel-in-progress": False,
        })
        admission = workflow["jobs"]["admission"]
        self.assertNotIn("environment", admission)
        self.assertNotIn("permissions", admission)
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        gate = admission["steps"][0]
        self.assertEqual(gate["env"]["RELEASE_MODE"], "${{ vars.DSX_RELEASE_MODE }}")
        for check in ['"$EVENT" == workflow_dispatch', '"$PROTECTED" == true',
                      '"$REF" == "refs/heads/$DEFAULT_BRANCH"', '"$RELEASE_MODE" == rc-promotion']:
            self.assertIn(check, gate["run"])
        publish = workflow["jobs"]["promote"]
        self.assertEqual(publish["needs"], "admission")
        self.assertEqual(publish["environment"], "stable-release")
        step = next(step for step in publish["steps"] if step.get("name") == "Verify and promote the selected RC")
        for key in ("RC_PROMOTION_SOURCE_NGC_KEY", "RC_PROMOTION_DESTINATION_NGC_KEY"):
            self.assertEqual(step["env"][key], "${{ secrets." + key + " }}")
        self.assertEqual(step["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertEqual(step["env"]["DSX_RELEASE_MODE"], "${{ needs.admission.outputs.release-mode }}")

    def test_rc_evidence_is_additive_opt_in_and_waits_for_all_artifacts(self):
        workflow = charts.yaml_object((ROOT / "workflows/release-candidate-publish.yml").read_bytes())
        option = workflow["on"]["workflow_call"]["inputs"]["promotion-manifest"]
        self.assertEqual(option["type"], "boolean")
        self.assertIs(option["default"], False)
        job = workflow["jobs"]["promotion-manifest"]
        self.assertEqual(job["needs"], ["plan", "release", "images", "charts"])
        for condition in [
            "inputs.promotion-manifest", "needs.release.outputs.should-publish == 'true'",
            "needs.images.result == 'success' || needs.plan.outputs.image-count == '0'",
            "needs.charts.result == 'success' || needs.plan.outputs.chart-count == '0'",
        ]:
            self.assertIn(condition, job["if"])
        upload = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/upload-artifact@"))
        self.assertEqual(upload["with"]["path"], "rc-promotion-manifest.json")
        self.assertEqual(upload["with"]["if-no-files-found"], "error")
        self.assertEqual(upload["with"]["name"],
                         "rc-promotion-manifest-${{ needs.release.outputs.version }}-${{ github.run_attempt }}")


if __name__ == "__main__":
    unittest.main()
