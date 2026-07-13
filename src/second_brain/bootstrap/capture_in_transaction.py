from second_brain.slices.capture.adapters.persistence.repository import (
    PostgresCaptureEventWriter,
)
from second_brain.slices.capture.application.capture_text import CaptureText
from second_brain.slices.capture.application.contracts import (
    CaptureTextCommand,
    CaptureTextPort,
)
from second_brain.slices.capture.domain.entities import CaptureEvent
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateTransaction,
)
from second_brain.slices.identity.application.contracts import UpdateTransaction


class CaptureInTransaction(CaptureTextPort):
    """Bootstrap composition that keeps CaptureEvent in the receipt transaction."""

    async def capture(
        self, command: CaptureTextCommand, transaction: UpdateTransaction
    ) -> CaptureEvent:
        if not isinstance(transaction, PostgresUpdateTransaction):
            raise TypeError("capture requires the PostgreSQL update transaction")
        return await CaptureText(
            PostgresCaptureEventWriter(transaction.active_session)
        ).execute(command)
