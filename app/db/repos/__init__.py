"""Repository module exports."""

from app.db.repos.base import BaseRepo
from app.db.repos.draft_repo import DraftRepo
from app.db.repos.email_repo import EmailRepo
from app.db.repos.rule_repo import RuleRepo
from app.db.repos.telegram_repo import TelegramRepo
from app.db.repos.user_repo import UserRepo

__all__ = [
    "BaseRepo",
    "UserRepo",
    "EmailRepo",
    "DraftRepo",
    "TelegramRepo",
    "RuleRepo",
]
