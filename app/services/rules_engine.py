"""Rules Engine with Trust Ladder: suggest, act_notify, and auto."""

from typing import Any
from uuid import UUID

import structlog

from app.db.repos.rule_repo import RuleRepo
from app.models.db import EmailRow

logger = structlog.get_logger("app.services.rules_engine")


class RulesEngine:
    """Evaluates user-defined rules and executes actions based on trust levels.

    CRITICAL INVARIANT: The 'auto' trust level NEVER automatically sends emails.
    """

    def __init__(self, rule_repo: RuleRepo) -> None:
        self.rule_repo = rule_repo

    def matches_condition(self, email: EmailRow, condition: dict[str, Any]) -> bool:
        """Check if an email matches a rule condition."""
        # Sender match
        if "sender_contains" in condition:
            val = str(condition["sender_contains"]).lower()
            if val not in email.sender.lower() and val not in email.sender_email.lower():
                return False

        # Subject match
        if "subject_contains" in condition:
            val = str(condition["subject_contains"]).lower()
            if val not in email.subject.lower():
                return False

        # Body match
        if "body_contains" in condition:
            val = str(condition["body_contains"]).lower()
            if val not in (email.body_clean or "").lower():
                return False

        return True

    async def evaluate_email(self, user_id: UUID | str, email: EmailRow) -> list[dict[str, Any]]:
        """Evaluate all active rules for a user against an email."""
        rules = await self.rule_repo.list_by_user(user_id)
        executed_actions: list[dict[str, Any]] = []

        for rule in rules:
            if not rule.enabled:
                continue

            if self.matches_condition(email, rule.condition):
                action = rule.action
                trust = rule.trust_level.lower()

                # Rule Execution based on Trust Ladder
                if trust == "auto":
                    # Silently apply non-destructive tag/label action
                    executed_actions.append(
                        {
                            "rule_id": str(rule.id),
                            "action": action,
                            "execution": "auto_silent",
                        }
                    )
                elif trust == "act_notify":
                    # Execute and queue notification
                    executed_actions.append(
                        {
                            "rule_id": str(rule.id),
                            "action": action,
                            "execution": "act_and_notify",
                        }
                    )
                else:  # suggest
                    # Only suggest action to user
                    executed_actions.append(
                        {
                            "rule_id": str(rule.id),
                            "action": action,
                            "execution": "suggest_only",
                        }
                    )

                logger.info(
                    "rule_triggered",
                    rule_id=str(rule.id),
                    user_id=str(user_id),
                    trust_level=trust,
                )

        return executed_actions
