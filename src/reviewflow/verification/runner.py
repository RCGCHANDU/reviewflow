"""Verification runner: deterministic checks plus the semantic checks' plumbing."""

from __future__ import annotations

from dataclasses import dataclass

from reviewflow.models.contracts import VerificationBatch, VerificationReport, VerificationResult
from reviewflow.models.enums import VerificationKind
from reviewflow.verification.checks import CHECKS, DEFAULT_CHECK_NAMES, CheckContext, run_check

#: Semantic check name -> the instruction the verifier agent must answer.
SEMANTIC_CHECKS: dict[str, str] = {
    "edit_addresses_accepted_issues": (
        "For each accepted issue listed, state whether the current text of the target visibly implements "
        "the recommended fix. Quote the text that implements it, or state that it is not implemented."
    ),
    "no_new_unsupported_claims": (
        "Read the added lines in the diff. State whether any added line introduces a factual, quantitative "
        "or causal claim that the material does not support. Quote each such line, or state that none exists."
    ),
    "uncertainty_preserved": (
        "State whether the edit removed or weakened any hedge, caveat, limitation or uncertainty marker that "
        "was present before the edit. Quote what was removed, or state that nothing was removed."
    ),
    "no_scope_inflation": (
        "State whether the revision now claims more than the original did (broader population, stronger "
        "causality, wider generality). Quote the specific text, or state that no inflation occurred."
    ),
}

#: Ran whenever an edit was applied, unless the caller overrides the list.
DEFAULT_SEMANTIC_CHECKS: tuple[str, ...] = (
    "edit_addresses_accepted_issues",
    "no_new_unsupported_claims",
    "uncertainty_preserved",
)


@dataclass
class VerificationRunner:
    """Runs checks and assembles the verification report."""

    check_names: tuple[str, ...] = DEFAULT_CHECK_NAMES
    semantic_checks: tuple[str, ...] = ()

    def run_deterministic(self, check_context: CheckContext) -> list[VerificationResult]:
        """Run every configured deterministic check. Unknown names fail loudly."""
        results: list[VerificationResult] = []
        for name in self.check_names:
            spec = CHECKS.get(name)
            if spec is None:
                raise KeyError(f"unknown verification check {name!r}")
            results.append(run_check(spec, check_context))
        return results

    def semantic_check_payload(self) -> list[dict[str, str]]:
        """The semantic checks as {name, instruction} pairs for the verifier packet."""
        return [{"name": name, "instruction": SEMANTIC_CHECKS[name]} for name in self.semantic_checks]

    def semantic_results(self, batch: VerificationBatch) -> list[VerificationResult]:
        """Convert the verifier agent's batch into results.

        A requested check the model failed to report is recorded as a *failed*
        mandatory result, not silently dropped: an unanswered check is not a pass.
        """
        answered = {item.check: item for item in batch.results}
        results: list[VerificationResult] = []
        for name in self.semantic_checks:
            item = answered.get(name)
            if item is None:
                results.append(
                    VerificationResult(
                        check=name,
                        command_or_method=SEMANTIC_CHECKS[name],
                        passed=False,
                        evidence="",
                        error="the verifier did not report this check",
                        kind=VerificationKind.SEMANTIC,
                        mandatory=True,
                    )
                )
                continue
            results.append(
                VerificationResult(
                    check=name,
                    command_or_method=item.method,
                    passed=item.passed,
                    evidence=item.evidence,
                    error=item.error,
                    kind=VerificationKind.SEMANTIC,
                    mandatory=True,
                )
            )
        return results

    @staticmethod
    def build_report(results: list[VerificationResult]) -> VerificationReport:
        return VerificationReport(results=results)
