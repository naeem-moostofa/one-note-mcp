from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
    # Defensive timeouts so a stuck/contended lock can never hang requests indefinitely:
    # - lock_timeout: a statement that waits more than this for a row lock errors out instead
    #   of blocking forever (e.g. behind another request's connection-row write).
    # - idle_in_transaction_session_timeout: a backend left "idle in transaction" — e.g. a
    #   process killed mid-request before commit — is reaped, releasing any locks it orphaned.
    # These are GUCs applied per asyncpg connection (values are milliseconds, as strings).
    connect_args={
        "server_settings": {
            "lock_timeout": "10000",                          # 10s
            "idle_in_transaction_session_timeout": "120000",  # 2 min
        }
    },
)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
