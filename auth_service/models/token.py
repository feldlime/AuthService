from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class Token(BaseModel):
    token: str
    created_at: datetime
    expired_at: datetime


class RegistrationToken(Token):
    user_id: UUID


class AccessToken(Token):
    session_id: UUID


class RefreshToken(Token):
    session_id: UUID
