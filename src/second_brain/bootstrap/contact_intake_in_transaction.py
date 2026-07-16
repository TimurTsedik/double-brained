from second_brain.slices.contacts.adapters.persistence.repository import (
    PostgresContactWriter,
)
from second_brain.slices.contacts.application.contracts import (
    ContactIntakePort,
    SaveContactCommand,
)
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.contracts import UpdateTransaction


class ContactIntakeInTransaction(ContactIntakePort):
    """Bootstrap composition: upsert контакта внутри receipt-транзакции.

    Идемпотентность приёма — существующий receipt-механизм (bot_id+update_id):
    replay того же update вообще не доходит до save.
    """

    async def save(
        self, command: SaveContactCommand, transaction: UpdateTransaction
    ) -> None:
        if not isinstance(transaction, PostgresUpdateTransaction):
            raise TypeError("contact intake requires the PostgreSQL update transaction")
        await PostgresContactWriter(transaction.active_session).upsert_contact(command)
