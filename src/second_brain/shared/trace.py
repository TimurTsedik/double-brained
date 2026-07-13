from dataclasses import dataclass
from secrets import token_hex


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    span_id: str

    def __post_init__(self) -> None:
        validate_identifier("trace_id", self.trace_id, 32)
        validate_identifier("span_id", self.span_id, 16)

    @classmethod
    def new_root(cls) -> "TraceContext":
        return cls(trace_id=new_identifier(16), span_id=new_identifier(8))

    def new_attempt(self) -> "TraceContext":
        span_id = new_identifier(8)
        while span_id == self.span_id:
            span_id = new_identifier(8)
        return TraceContext(trace_id=self.trace_id, span_id=span_id)


def new_identifier(byte_count: int) -> str:
    identifier = token_hex(byte_count)
    return identifier if identifier.strip("0") else "1" + identifier[1:]


def validate_identifier(name: str, identifier: str, length: int) -> None:
    if (
        len(identifier) != length
        or identifier == "0" * length
        or any(character not in "0123456789abcdef" for character in identifier)
    ):
        raise ValueError(f"{name} must be a nonzero lowercase hexadecimal identifier")
