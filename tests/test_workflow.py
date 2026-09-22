from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper_review_service.models import Report
from paper_review_service.workflow import STAGES, StageResult, run_workflow


STAGE_ORDER = (
    "summary", "literature_review", "literature_expansion", "historian",
    "baseline_scout", "novelty_questions", "technical_questions", "novelty_answers",
    "technical_answers", "countercheck", "synthesis",
)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        manuscript = b"%PDF-workflow-test"
        (self.work / "input.pdf").write_bytes(manuscript)
        self.pages = [
            {"page": 1, "text": "Our estimator is unbiased under assumption A.\n"
             "The theorem requires independent observations."},
            {"page": 2, "text": "We report results from one seed.\n"
             "No ablation study is included."},
        ]
        (self.work / "pages.json").write_text(json.dumps(self.pages), encoding="utf-8")
        self.job = {
            "id": "workflow-test", "filename": "paper.pdf", "focus": "FOCUS_SENTINEL",
            "cutoff_date": "2026-09-22", "model": "test-model", "reasoning": "high",
            "max_subagents": 0, "input_sha256": hashlib.sha256(manuscript).hexdigest(),
            "timeout_seconds": 3600,
        }
        self.prompts = self.work / "prompts"
        self.prompts.mkdir()
        (self.prompts / "common.md").write_text("COMMON_INSTRUCTIONS_SENTINEL", encoding="utf-8")
        for identifier in STAGE_ORDER:
            (self.prompts / f"{identifier}.md").write_text(
                f"ROLE::{identifier}::INSTRUCTION", encoding="utf-8",
            )
        prompt_directory = patch("paper_review_service.workflow.PROMPT_DIRECTORY", self.prompts)
        prompt_directory.start()
        self.addCleanup(prompt_directory.stop)
        self.calls = []
        self.payloads = self.make_payloads()

    def issue(self, identifier, *, page=1, evidence=None, category="claim"):
        return {
            "id": identifier, "title": f"Check {identifier}", "category": category,
            "severity": "major", "status": "confirmed", "confidence": "medium",
            "page": page, "evidence_kind": "text",
            "evidence": evidence or "Our estimator is unbiased under assumption A.",
            "problem": f"Check the supporting evidence for {identifier}.",
            "impact": "The central claim may need qualification.",
            "countercheck": "Checked the stated assumptions and the second page.",
            "next_action": "Provide the missing justification.", "source_urls": [],
        }

    def make_payloads(self):
        payloads = {
            name: {
                "stage": name, "status": "completed", "analysis": f"ANALYSIS::{name}",
                "claims": [], "questions": [], "findings": [], "counterchecks": [],
                "sources": [], "reviewed_pages": [1, 2], "visual_pages": [], "limitations": [],
            }
            for name in STAGE_ORDER[:-1]
        }
        payloads["summary"]["claims"] = [{
            "id": "C1", "claim": "The estimator is unbiased under assumption A.", "pages": [1],
        }]
        for name, identifier in (("novelty_questions", "NQ1"), ("technical_questions", "TQ1")):
            payloads[name]["questions"] = [{
                "id": identifier, "claim_id": "C1", "question": f"QUESTION::{identifier}",
                "pages": [1],
            }]

        kept = self.issue("F_KEEP")
        revised = self.issue("F_REVISE", page=2, evidence="We report results from one seed.",
                             category="experiment")
        removed = self.issue("F_REMOVE", evidence="The theorem requires independent observations.",
                             category="method")
        unresolved = self.issue("F_UNRESOLVED", page=2, evidence="No ablation study is included.",
                                category="experiment")
        payloads["novelty_answers"]["findings"] = [deepcopy(kept)]
        payloads["technical_answers"]["findings"] = [deepcopy(revised), removed, deepcopy(unresolved)]
        revised["severity"] = "minor"
        revised["problem"] = "Qualify the empirical estimate; the broader claim remains plausible."
        unresolved["status"] = "unresolved"
        checked = [kept, revised, unresolved]
        payloads["countercheck"]["findings"] = deepcopy(checked)
        payloads["countercheck"]["counterchecks"] = [
            {"issue_id": identifier, "decision": decision, "reason": f"DECISION::{decision}"}
            for identifier, decision in (
                ("F_KEEP", "keep"), ("F_REVISE", "revise"),
                ("F_REMOVE", "remove"), ("F_UNRESOLVED", "unresolved"),
            )
        ]
        payloads["synthesis"] = {
            "summary": "The evidence supports a limited review.", "strengths": [],
            "issues": deepcopy(checked),
            "coverage": [{"area": "claims and experiments", "status": "checked",
                          "details": "Both supplied pages were checked."}],
            "reviewed_pages": [1, 2], "visual_pages": [], "unreviewed_pages": [],
            "sources": [], "limitations": [],
        }
        return payloads

    def invoke(self, stage, prompt, output_dir, budget):
        trace = json.loads((self.work / "workflow.json").read_text())
        self.assertEqual(trace["status"], "running")
        self.assertEqual(trace["stages"][-1]["id"], stage.identifier)
        self.assertEqual(trace["stages"][-1]["status"], "running")
        self.assertEqual(set(trace["results"]), {call[0].identifier for call in self.calls})
        self.calls.append((stage, prompt, output_dir, budget))
        self.assertEqual(output_dir, self.work / "stages" / stage.identifier)
        self.assertGreater(budget, 0)
        self.assertLessEqual(budget, self.job["timeout_seconds"])
        self.assertTrue(output_dir.is_dir())
        (output_dir / "result.json").write_text(
            json.dumps(self.payloads[stage.identifier]), encoding="utf-8",
        )

    def run_review(self, *, timeout_seconds=3600):
        return run_workflow(self.work, self.job, self.pages, invoke=self.invoke,
                            timeout_seconds=timeout_seconds)

    def assert_stopped_at(self, stage):
        self.assertEqual([call[0].identifier for call in self.calls],
                         list(STAGE_ORDER[:STAGE_ORDER.index(stage) + 1]))
        for later in STAGE_ORDER[STAGE_ORDER.index(stage) + 1:]:
            self.assertFalse((self.work / "stages" / later / "result.json").exists())
        trace = json.loads((self.work / "workflow.json").read_text())
        self.assertEqual(trace["status"], "failed")
        self.assertEqual(trace["stages"][-1]["id"], stage)
        self.assertEqual(trace["stages"][-1]["status"], "failed")
        self.assertNotIn(stage, trace["results"])
        self.assertEqual(set(trace["results"]), set(STAGE_ORDER[:STAGE_ORDER.index(stage)]))
        self.assertTrue(all(record["status"] == "succeeded" for record in trace["stages"][:-1]))

    def test_success_runs_ordered_roles_with_upstream_evidence_and_budgets(self):
        report, trace = self.run_review()
        self.assertIsInstance(report, Report)
        self.assertEqual(tuple(stage.identifier for stage in STAGES), STAGE_ORDER)
        self.assertEqual([call[0].identifier for call in self.calls], list(STAGE_ORDER))
        self.assertEqual(report.model_dump(), self.payloads["synthesis"])
        self.assertEqual(trace["status"], "succeeded")
        self.assertEqual(trace["results"], self.payloads)
        self.assertRegex(trace["prompt_bundle_sha256"], r"^[a-f0-9]{64}$")
        for (stage, prompt, output_dir, _), record in zip(self.calls, trace["stages"]):
            with self.subTest(stage=stage.identifier):
                self.assertIn(stage.identifier, prompt)
                self.assertIn(self.job["focus"], prompt)
                self.assertIn(self.job["cutoff_date"], prompt)
                self.assertIn("COMMON_INSTRUCTIONS_SENTINEL", prompt)
                self.assertIn(f"ROLE::{stage.identifier}::INSTRUCTION", prompt)
                for other in STAGE_ORDER:
                    if other != stage.identifier:
                        self.assertNotIn(f"ROLE::{other}::INSTRUCTION", prompt)
                for dependency in stage.dependencies:
                    self.assertIn(f"ANALYSIS::{dependency}", prompt)
                    for field in ("claims", "questions", "findings", "counterchecks"):
                        for item in self.payloads[dependency][field]:
                            self.assertIn(json.dumps(item, ensure_ascii=False), prompt)
                self.assertEqual(record["status"], "succeeded")
                self.assertEqual(record["prompt_sha256"], hashlib.sha256(prompt.encode()).hexdigest())
                self.assertEqual(record["output_sha256"],
                                 hashlib.sha256((output_dir / "result.json").read_bytes()).hexdigest())
                self.assertEqual((output_dir / "prompt.md").read_text(), prompt)
        self.assertEqual(json.loads((self.work / "workflow.json").read_text()), trace)
        for stage in STAGES[:-1]:
            StageResult.model_validate_json(
                (self.work / "stages" / stage.identifier / "result.json").read_text(),
            )

    def test_technical_claim_can_be_introduced_and_referenced_by_the_next_stage(self):
        claim = {"id": "TC001", "claim": "The theorem requires independent observations.", "pages": [1]}
        self.payloads["technical_questions"]["claims"].append(claim)
        self.payloads["technical_questions"]["questions"].append({
            "id": "TQ2", "claim_id": "TC001", "question": "Where is independence used?", "pages": [1],
        })
        self.payloads["technical_answers"]["questions"] = [{
            "id": "TAQ1", "claim_id": "TC001", "question": "Does the proof justify independence?",
            "pages": [1],
        }]

        report, trace = self.run_review()

        self.assertEqual(trace["status"], "succeeded")
        self.assertEqual([call[0].identifier for call in self.calls], list(STAGE_ORDER))
        self.assertEqual(report.model_dump(), self.payloads["synthesis"])
        self.assertEqual(trace["results"]["technical_questions"]["claims"], [claim])
        self.assertEqual(trace["results"]["technical_answers"]["questions"][0]["claim_id"], "TC001")
        self.assertEqual(trace["results"]["technical_answers"]["claims"], [])
        answers_prompt = next(prompt for stage, prompt, _, _ in self.calls
                              if stage.identifier == "technical_answers")
        self.assertIn(json.dumps(claim), answers_prompt)

    def test_unviewable_visual_candidate_can_be_withdrawn_with_an_explicit_limitation(self):
        visual = self.issue("F_VISUAL", page=2, evidence="Figure 1 appears to repeat the same panel.",
                            category="experiment")
        visual["evidence_kind"] = "visual"
        visual["status"] = "unresolved"
        self.payloads["novelty_answers"]["findings"] = []
        self.payloads["technical_answers"]["findings"] = [visual]
        self.payloads["technical_answers"]["visual_pages"] = [2]
        limitation = (
            "Page 2 could not be visually inspected during countercheck. F_VISUAL is temporarily "
            "withdrawn pending inspection; this does not establish that the candidate is false."
        )
        self.payloads["countercheck"].update({
            "status": "partial", "analysis": "Visual verification remains incomplete.",
            "findings": [], "reviewed_pages": [], "visual_pages": [], "limitations": [limitation],
            "counterchecks": [{"issue_id": "F_VISUAL", "decision": "remove", "reason": limitation}],
        })
        self.payloads["synthesis"].update({
            "summary": "No retained findings; the visual candidate still needs independent inspection.",
            "issues": [], "visual_pages": [], "limitations": [limitation],
            "coverage": [{"area": "visual evidence", "status": "partial", "details": limitation}],
        })

        report, trace = self.run_review()

        self.assertEqual(trace["status"], "succeeded")
        self.assertEqual([call[0].identifier for call in self.calls], list(STAGE_ORDER))
        self.assertEqual(trace["results"]["technical_answers"]["visual_pages"], [2])
        self.assertEqual(trace["results"]["technical_answers"]["findings"], [visual])
        countercheck = trace["results"]["countercheck"]
        self.assertEqual(countercheck["status"], "partial")
        self.assertEqual(countercheck["reviewed_pages"], [])
        self.assertEqual(countercheck["visual_pages"], [])
        self.assertEqual(countercheck["findings"], [])
        self.assertEqual(countercheck["counterchecks"][0]["reason"], limitation)
        self.assertEqual(report.issues, [])
        self.assertEqual(report.visual_pages, [])
        self.assertEqual(report.limitations, [limitation])
        synthesis_prompt = self.calls[-1][1]
        self.assertIn(json.dumps(countercheck, ensure_ascii=False), synthesis_prompt)

    def test_fabricated_quote_stops_before_countercheck_and_synthesis(self):
        self.payloads["technical_answers"]["findings"][0]["evidence"] = "This quote is fabricated."
        with self.assertRaises(ValueError):
            self.run_review()
        self.assert_stopped_at("technical_answers")

    def test_out_of_range_claim_page_stops_before_downstream_review(self):
        self.payloads["summary"]["claims"][0]["pages"] = [3]
        with self.assertRaises(ValueError):
            self.run_review()
        self.assert_stopped_at("summary")

    def test_invalid_stage_schema_stops_before_downstream_review(self):
        self.payloads["literature_review"]["status"] = "looks_good"
        with self.assertRaises(ValueError):
            self.run_review()
        self.assert_stopped_at("literature_review")

    def test_countercheck_must_decide_every_upstream_finding(self):
        self.payloads["countercheck"]["counterchecks"] = [
            item for item in self.payloads["countercheck"]["counterchecks"]
            if item["issue_id"] != "F_KEEP"
        ]
        with self.assertRaises(ValueError):
            self.run_review()
        self.assert_stopped_at("countercheck")

    def test_countercheck_keep_cannot_silently_rewrite_a_finding(self):
        self.payloads["countercheck"]["findings"][0]["severity"] = "critical"
        with self.assertRaises(ValueError):
            self.run_review()
        self.assert_stopped_at("countercheck")

    def test_countercheck_unresolved_cannot_leave_confirmed_status(self):
        self.payloads["countercheck"]["findings"][2]["status"] = "confirmed"
        with self.assertRaises(ValueError):
            self.run_review()
        self.assert_stopped_at("countercheck")

    def test_synthesis_cannot_resurrect_a_removed_finding(self):
        removed = self.payloads["technical_answers"]["findings"][1]
        self.payloads["synthesis"]["issues"].append(deepcopy(removed))
        with self.assertRaises(ValueError):
            self.run_review()
        self.assert_stopped_at("synthesis")

    def test_synthesis_cannot_upgrade_a_counterchecked_severity(self):
        self.payloads["synthesis"]["issues"][1]["severity"] = "critical"
        with self.assertRaises(ValueError):
            self.run_review()
        self.assert_stopped_at("synthesis")

    def test_no_time_budget_fails_before_invoking_any_model(self):
        with self.assertRaises(TimeoutError):
            self.run_review(timeout_seconds=0)
        self.assertEqual(self.calls, [])
        trace = json.loads((self.work / "workflow.json").read_text())
        self.assertEqual(trace["status"], "failed")
        self.assertEqual(trace["results"], {})
        self.assertEqual(trace["stages"][0]["error_type"], "TimeoutError")


if __name__ == "__main__":
    unittest.main()
