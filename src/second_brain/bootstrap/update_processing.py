"""Сборка LocalUpdateProcessor — один конвейер обработки апдейтов.

Композиция всех портов процессора вынесена из поллера и переиспользуется
двумя входами с одинаковым поведением: long-polling (local_polling) и
inbox-шагом воркера (webhook-путь, telegram_inbox_step). Любое изменение
набора портов делается здесь ОДИН раз — пути не расходятся.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from second_brain.bootstrap.contact_intake_in_transaction import (
    ContactIntakeInTransaction,
)
from second_brain.bootstrap.digest_in_transaction import DigestInTransaction
from second_brain.bootstrap.exact_search_in_transaction import (
    ExactSearchInTransaction,
)
from second_brain.bootstrap.image_capture_in_transaction import (
    ImageCaptureInTransaction,
)
from second_brain.bootstrap.memory_ask_in_transaction import MemoryAskInTransaction
from second_brain.bootstrap.project_context_in_transaction import (
    ProjectContextInTransaction,
)
from second_brain.bootstrap.record_edit_in_transaction import RecordEditInTransaction
from second_brain.bootstrap.record_view_in_transaction import RecordViewInTransaction
from second_brain.bootstrap.settings import Settings
from second_brain.bootstrap.task_capture_in_transaction import TaskCaptureInTransaction
from second_brain.bootstrap.voice_capture_in_transaction import (
    VoiceCaptureInTransaction,
)
from second_brain.shared.clock import SystemClock
from second_brain.slices.identity.adapters.persistence.repositories import (
    PostgresUpdateRepository,
)
from second_brain.slices.identity.application.local_updates import LocalUpdateProcessor


def build_update_processor(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    bot_username: str | None,
) -> LocalUpdateProcessor:
    """Собирает процессор апдейтов со ВСЕМИ портами (как делал поллер)."""
    task_capture = TaskCaptureInTransaction()
    exact_search = ExactSearchInTransaction()
    project_context = ProjectContextInTransaction()
    # Один объект на оба порта показа: запись целиком + её sidecar-ссылки.
    record_view = RecordViewInTransaction(
        image_storage_root=settings.image_storage_root
    )
    return LocalUpdateProcessor(
        store=PostgresUpdateRepository(session_factory),
        clock=SystemClock(),
        pepper=settings.invite_token_pepper,
        pepper_key_id=settings.invite_token_pepper_key_id,
        capture_text_port=task_capture,
        task_mode_port=task_capture,
        task_panel_port=task_capture,
        exact_search_port=exact_search,
        capture_voice_port=VoiceCaptureInTransaction(),
        capture_image_port=ImageCaptureInTransaction(),
        project_panel_port=project_context,
        memory_ask_port=MemoryAskInTransaction(),
        bot_username=bot_username,
        reminder_ack_port=task_capture,
        contact_port=ContactIntakeInTransaction(),
        record_view_port=record_view,
        digest_port=DigestInTransaction(),
        record_links_port=record_view,
        record_edit_port=RecordEditInTransaction(),
        api_token_pepper=settings.api_token_pepper,
        api_token_pepper_key_id=settings.api_token_pepper_key_id,
    )
