__all__ = ["Session"]

import time
from typing import Any, AsyncGenerator

from fastapi import Request

from ayon_server.api.clientinfo import ClientInfo, get_client_info, get_real_ip
from ayon_server.config import ayonconfig
from ayon_server.entities import UserEntity
from ayon_server.events import EventStream
from ayon_server.lib.redis import Redis
from ayon_server.types import OPModel
from ayon_server.utils import create_hash, json_dumps, json_loads


class SessionModel(OPModel):
    user: UserEntity.model.main_model  # type: ignore
    token: str
    created: float = 0
    last_used: float = 0
    is_service: bool = False
    client_info: ClientInfo | None = None

    @property
    def user_entity(self) -> UserEntity:
        return UserEntity(
            payload=self.user.dict(),
            exists=True,
        )


def is_local_ip(ip: str) -> bool:
    return (
        ip.startswith("127.")
        or ip.startswith("10.")
        or ip.startswith("192.168.")
        or ip.startswith("172.")
    )


class Session:
    ns = "session"

    @classmethod
    def is_expired(cls, session: SessionModel) -> bool:
        ttl = 600 if session.is_service else ayonconfig.session_ttl
        return time.time() - session.last_used > ttl

    @classmethod
    async def check(cls, token: str, request: Request | None) -> SessionModel | None:
        """Return a session corresponding to a given access token.

        Return None if the token is invalid.
        If the session is expired, it will be removed from the database.
        If it's not expired, update the last_used field and extend
        its lifetime.
        """
        data = await Redis.get(cls.ns, token)
        if not data:
            return None

        session = SessionModel(**json_loads(data))

        if cls.is_expired(session):
            await cls.delete(token, "Session expired")
            return None

        if request:
            if (
                not session.client_info
                or session.client_info.site_id != request.headers.get("x-ayon-site-id")
            ):
                session.client_info = get_client_info(request)
                session.last_used = time.time()
                await Redis.set(cls.ns, token, session.json())
            elif not ayonconfig.disable_check_session_ip:
                real_ip = get_real_ip(request)
                if not is_local_ip(real_ip):
                    if session.client_info.ip != real_ip:
                        r = f"Stored: {session.client_info.ip}, current: {real_ip}"
                        await cls.delete(token, f"Client IP mismatch: {r}")
                        return None

        # extend normal tokens validity, but not service tokens.
        # they should be validated against db forcefully every 10 minutes or so

        if not session.is_service:
            remaining_ttl = ayonconfig.session_ttl - (time.time() - session.last_used)
            if remaining_ttl < ayonconfig.session_ttl - 120:
                session.last_used = time.time()
                await Redis.set(cls.ns, token, json_dumps(session.dict()))

        return session

    @classmethod
    async def create(
        cls,
        user: UserEntity,
        request: Request | None = None,
        token: str | None = None,
        message: str = "User logged in",
        event_payload: dict[str, Any] | None = None,
    ) -> SessionModel:
        """Create a new session for a given user."""
        is_service = bool(token)
        if token is None:
            token = create_hash()
        client_info = get_client_info(request) if request else None
        session = SessionModel(
            user=user.dict(),
            token=token,
            created=time.time(),
            last_used=time.time(),
            is_service=is_service,
            client_info=client_info,
        )
        event_summary = client_info.dict() if client_info else {}
        await Redis.set(cls.ns, token, session.json())
        if not user.is_service:
            await EventStream.dispatch(
                "auth.login",
                description=message,
                user=user.name,
                summary=event_summary,
                payload=event_payload,
            )
        return session

    @classmethod
    async def update(
        cls,
        token: str,
        user: UserEntity,
        client_info: ClientInfo | None = None,
    ) -> None:
        """Update a session with new user data."""
        data = await Redis.get(cls.ns, token)
        if not data:
            # TODO: shouldn't be silent!
            return None

        session = SessionModel(**json_loads(data))
        session.user = user.dict()
        if client_info is not None:
            session.client_info = client_info
        session.last_used = time.time()
        await Redis.set(cls.ns, token, session.json())

    @classmethod
    async def delete(cls, token: str, message: str = "User logged out") -> None:
        data = await Redis.get(cls.ns, token)
        if data:
            session = SessionModel(**json_loads(data))
            if not session.user.data.get("isService"):
                await EventStream.dispatch(
                    "auth.logout",
                    description=message,
                    user=session.user.name,
                )
        await Redis.delete(cls.ns, token)

    @classmethod
    async def list(
        cls, user_name: str | None = None
    ) -> AsyncGenerator[SessionModel, None]:
        """List active sessions for all or given user

        Additionally, this function also removes expired sessions
        from the database.
        """

        async for _, data in Redis.iterate("session"):
            if data is None:
                continue  # this should never happen, but keeps mypy happy

            session = SessionModel(**json_loads(data))
            if cls.is_expired(session):
                await cls.delete(session.token, message="Session expired")
                continue

            if user_name is None or session.user.name == user_name:
                yield session
