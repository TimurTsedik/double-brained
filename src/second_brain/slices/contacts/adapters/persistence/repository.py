from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from second_brain.slices.contacts.adapters.persistence.models import ContactModel
from second_brain.slices.contacts.application.contracts import SaveContactCommand
from second_brain.slices.contacts.domain.entities import Contact
from second_brain.slices.identity.application.contracts import AccessContext


class PostgresContactWriter:
    """Contact reads/writes through a transaction owned by the caller."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert_contact(self, command: SaveContactCommand) -> None:
        # Один оператор под receipt-транзакцией: повторная карточка с тем же
        # именем (без учёта регистра) ОБНОВЛЯЕТ номер, а не плодит дубль —
        # конфликт ловит уникальный expression-индекс (user_space_id,
        # lower(display_name)).
        await _set_user_space_scope(self._session, command.access_context)
        statement = insert(ContactModel).values(
            id=uuid4(),
            user_space_id=command.access_context.user_space_id,
            display_name=command.display_name,
            phone_number=command.phone_number,
            created_at=command.saved_at,
            updated_at=command.saved_at,
            trace_id=command.trace_id,
        )
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    ContactModel.user_space_id,
                    func.lower(ContactModel.display_name),
                ],
                set_={
                    "phone_number": statement.excluded.phone_number,
                    "updated_at": statement.excluded.updated_at,
                    "trace_id": statement.excluded.trace_id,
                },
            )
        )
        await self._session.flush()

    async def list_contacts(self, access_context: AccessContext) -> tuple[Contact, ...]:
        # Один SELECT контактов пространства (их единицы): RLS-GUC уже выставлен
        # транзакцией вызывающего, плюс ЯВНЫЙ предикат по user_space_id.
        await _set_user_space_scope(self._session, access_context)
        models = await self._session.scalars(
            select(ContactModel)
            .where(ContactModel.user_space_id == access_context.user_space_id)
            .order_by(func.lower(ContactModel.display_name))
        )
        return tuple(_to_entity(model) for model in models)


async def _set_user_space_scope(
    session: AsyncSession, access_context: AccessContext
) -> None:
    await session.execute(
        text("SELECT set_config('second_brain.user_space_id', :user_space_id, true)"),
        {"user_space_id": str(access_context.user_space_id)},
    )


def _to_entity(model: ContactModel) -> Contact:
    return Contact(
        id=model.id,
        user_space_id=model.user_space_id,
        display_name=model.display_name,
        phone_number=model.phone_number,
        created_at=model.created_at,
        updated_at=model.updated_at,
        trace_id=model.trace_id,
    )
