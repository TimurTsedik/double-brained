from typing import Protocol

from second_brain.slices.classification.application.contracts import (
    ClassificationDraft,
    ClassificationRequest,
)


class ClassificationModel(Protocol):
    async def classify(self, request: ClassificationRequest) -> ClassificationDraft: ...
