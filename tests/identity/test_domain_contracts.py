from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from second_brain.slices.identity.application.contracts import AccessContext
from second_brain.slices.identity.domain.entities import UserRole


def test_user_role_has_admin_and_member() -> None:
    assert [role.value for role in UserRole] == ["admin", "member"]


def test_access_context_holds_only_internal_ids() -> None:
    user_id = uuid4()
    user_space_id = uuid4()

    context = AccessContext(user_id=user_id, user_space_id=user_space_id)

    assert context.user_id == user_id
    assert context.user_space_id == user_space_id
    assert tuple(vars(context)) == ("user_id", "user_space_id")


def test_access_context_is_frozen() -> None:
    context = AccessContext(user_id=uuid4(), user_space_id=uuid4())

    with pytest.raises(FrozenInstanceError):
        context.user_id = uuid4()
