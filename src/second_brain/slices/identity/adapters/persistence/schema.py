from sqlalchemy.ext.asyncio import AsyncEngine

from second_brain.slices.identity.adapters.persistence.models import Base


async def initialize_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def reset_prototype_schema(engine: AsyncEngine, confirm: bool) -> None:
    if not confirm:
        raise ValueError("prototype schema reset requires confirmation")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
