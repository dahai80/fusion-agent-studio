from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .llm_gateway import LLMGateway

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

VERIFY_PROMPT = """\
You are a verification agent. Your job is to check whether the output of a previous step meets the specified criteria.

Original task: {task}
Output to verify: {output}
Verification criteria: {criteria}
Context: {context}

Evaluate the output against the criteria. Respond with a JSON object:
{{
  "passed": true/false,
  "score": 0.0-1.0,
  "issues": ["list of issues found, empty if passed"],
  "suggestion": "how to fix if not passed, empty string if passed"
}}

Return ONLY the JSON object, no other text."""

RE_VERIFY_PROMPT = """\
You are a verification agent performing re-verification after fixes.

Original task: {task}
Original output: {original_output}
Fix applied: {fix_description}
New output: {new_output}
Verification criteria: {criteria}

Evaluate whether the fix addressed the previous issues. Respond with a JSON object:
{{
  "passed": true/false,
  "score": 0.0-1.0,
  "issues": ["remaining issues, empty if passed"],
  "suggestion": "further fix if still not passed, empty string if passed"
}}

Return ONLY the JSON object, no other text."""


@dataclass
class VerificationResult:
    id: str = ""
    passed: bool = False
    score: float = 0.0
    issues: list[str] = field(default_factory=list)
    suggestion: str = ""
    attempt: int = 1
    max_attempts: int = 3
    verified_at: float = 0.0
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.id:
            self.id = f"verify_{uuid.uuid4().hex[:8]}"
        if not self.verified_at:
            self.verified_at = time.time()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "passed": self.passed,
            "score": self.score,
            "issues": self.issues,
            "suggestion": self.suggestion,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "verified_at": self.verified_at,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> VerificationResult:
        return cls(
            id=data.get("id", ""),
            passed=data.get("passed", False),
            score=data.get("score", 0.0),
            issues=data.get("issues", []),
            suggestion=data.get("suggestion", ""),
            attempt=data.get("attempt", 1),
            max_attempts=data.get("max_attempts", 3),
            verified_at=data.get("verified_at", 0.0),
            metadata=data.get("metadata", {}),
        )


class VerificationEngine:
    def __init__(self, gateway: LLMGateway | None = None, max_attempts: int = 3):
        self.gateway = gateway
        self.max_attempts = max_attempts
        logger.info("VerificationEngine initialized (max_attempts=%d)", max_attempts)

    async def verify(
        self,
        task: str,
        output: str,
        criteria: str = "",
        context: str = "",
        max_attempts: int | None = None,
    ) -> VerificationResult:
        attempts = max_attempts or self.max_attempts
        logger.info(
            "Starting verification: task=%s, max_attempts=%d", task[:60], attempts
        )

        result = None
        for attempt in range(1, attempts + 1):
            prompt = VERIFY_PROMPT.format(
                task=task,
                output=output,
                criteria=criteria
                or "Output should correctly and completely address the task",
                context=context,
            )

            result = await self._call_llm(prompt, attempt, attempts)
            if result is None:
                logger.warning("Verification attempt %d returned None", attempt)
                continue

            logger.info(
                "Verification attempt %d: passed=%s score=%.2f issues=%d",
                attempt,
                result.passed,
                result.score,
                len(result.issues),
            )

            if result.passed:
                return result

            output = f"{output}\n\n[Previous issues: {json.dumps(result.issues)}. Suggestion: {result.suggestion}]"

        if result is None:
            return VerificationResult(
                passed=False,
                score=0.0,
                issues=["All verification attempts failed to produce valid output"],
                attempt=attempts,
                max_attempts=attempts,
            )
        result.passed = False
        result.attempt = attempts
        result.max_attempts = attempts
        logger.warning("Verification failed after %d attempts", attempts)
        return result

    async def re_verify(
        self,
        task: str,
        original_output: str,
        new_output: str,
        fix_description: str,
        criteria: str = "",
        context: str = "",
    ) -> VerificationResult:
        logger.info("Starting re-verification for task: %s", task[:60])

        prompt = RE_VERIFY_PROMPT.format(
            task=task,
            original_output=original_output,
            fix_description=fix_description,
            new_output=new_output,
            criteria=criteria
            or "Output should correctly and completely address the task",
            context=context,
        )

        result = await self._call_llm(prompt, 1, 1)
        if result is None:
            return VerificationResult(
                passed=False,
                score=0.0,
                issues=["LLM call failed"],
                suggestion="Retry verification",
            )
        return result

    async def _call_llm(
        self, prompt: str, attempt: int, max_attempts: int
    ) -> VerificationResult | None:
        if not self.gateway:
            logger.error("No LLMGateway configured for verification")
            return VerificationResult(
                passed=False,
                score=0.0,
                issues=["No LLM gateway configured"],
                attempt=attempt,
                max_attempts=max_attempts,
            )

        try:
            messages = [{"role": "user", "content": prompt}]
            response = await self.gateway.chat(
                messages=messages,
                temperature=0.1,
                max_tokens=1024,
            )

            content = ""
            # 审计 E-12: gateway finish_reason=="error" 哨兵 content="" 不代表成功, 抛错走重试.
            if hasattr(response, "finish_reason") and response.finish_reason == "error":
                err = (getattr(response, "usage", None) or {}).get("error", "gateway error")
                raise RuntimeError(f"LLM gateway error: {err}")
            if hasattr(response, "content"):
                content = response.content or ""
            elif isinstance(response, dict):
                content = response.get("content", "")

            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(
                    lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
                )

            data = json.loads(content)
            return VerificationResult(
                passed=bool(data.get("passed", False)),
                score=float(data.get("score", 0.0)),
                issues=data.get("issues", []),
                suggestion=data.get("suggestion", ""),
                attempt=attempt,
                max_attempts=max_attempts,
                metadata={"raw_response": content[:500]},
            )
        except json.JSONDecodeError as e:
            logger.error("Verification LLM returned invalid JSON: %s", e)
            return None
        except Exception as e:
            logger.error("Verification LLM call failed: %s", e)
            return None

    async def adversarial_verify(
        self,
        claim: str,
        context: str = "",
        voter_count: int = 3,
        threshold: float = 0.6,
    ) -> dict:
        logger.info(
            "Adversarial verify: claim=%s voters=%d threshold=%.1f",
            claim[:60],
            voter_count,
            threshold,
        )
        prompts = []
        for i in range(voter_count):
            prompt = (
                f"You are skeptic #{i + 1}. Your job is to TRY TO REFUTE the following claim. "
                f"Be thorough and look for flaws, unsupported assertions, logical errors.\n\n"
                f"Claim: {claim}\nContext: {context}\n\n"
                f'Respond with JSON: {{"refuted": true/false, "reason": "why it is or is not refuted"}}'
            )
            prompts.append(prompt)

        tasks = [self._call_llm(p, i + 1, voter_count) for i, p in enumerate(prompts)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        votes = []
        refuted_count = 0
        survived_count = 0
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                votes.append({"voter": i, "verdict": "error", "reason": str(r)})
                refuted_count += 1
            elif r is None:
                votes.append(
                    {"voter": i, "verdict": "error", "reason": "LLM returned None"}
                )
                refuted_count += 1
            else:
                is_refuted = not r.passed or r.score < 0.5
                if is_refuted:
                    refuted_count += 1
                    votes.append(
                        {
                            "voter": i,
                            "verdict": "refuted",
                            "reason": r.suggestion or "Score too low",
                        }
                    )
                else:
                    survived_count += 1
                    votes.append(
                        {
                            "voter": i,
                            "verdict": "survived",
                            "reason": r.issues or "No issues found",
                        }
                    )

        passes = (survived_count / max(voter_count, 1)) >= threshold
        logger.info(
            "Adversarial verify result: survived=%d refuted=%d passes=%s",
            survived_count,
            refuted_count,
            passes,
        )
        return {
            "passes": passes,
            "survived": survived_count,
            "refuted": refuted_count,
            "threshold": threshold,
            "votes": votes,
        }
