"""Build-time copy of Ark2's source-table DSL parser.

Derived from tools/build_character_archive.py (decimal_json, DSLParseError,
DSLParser), with support for repeated top-level property separators. Runtime
code reads compiled JSON and never imports this module.
"""
from __future__ import annotations
import re
from decimal import Decimal, InvalidOperation
from typing import Any

def decimal_json(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


class DSLParseError(ValueError):
    def __init__(self, message: str, position: int):
        super().__init__(f"{message} at position {position}")
        self.message = message
        self.position = position


class DSLParser:
    """Tolerant parser for the source tables' object/array/hash DSL."""

    def __init__(self, value: str):
        self.value = value
        self.position = 0

    def parse(self) -> Any:
        self._skip_space()
        items = [self._parse_value()]
        separators: list[str] = []
        self._skip_space()
        while self.position < len(self.value):
            if self.value[self.position] not in "~|":
                raise DSLParseError("unexpected trailing input", self.position)
            separator = self.value[self.position]
            self.position += 1
            while self.position < len(self.value) and self.value[self.position] == separator[0]:
                separator += self.value[self.position]
                self.position += 1
            separators.append(separator)
            self._skip_space()
            items.append(self._parse_value())
            self._skip_space()
        if len(items) == 1:
            return items[0]
        return {"__type__": "sequence", "separators": separators, "items": items}

    def _skip_space(self) -> None:
        while self.position < len(self.value) and self.value[self.position].isspace():
            self.position += 1

    def _peek(self) -> str:
        self._skip_space()
        return self.value[self.position] if self.position < len(self.value) else ""

    def _parse_value(self) -> Any:
        self._skip_space()
        if self.position >= len(self.value):
            raise DSLParseError("expected value", self.position)
        char = self.value[self.position]
        if char == "{":
            return self._parse_object()
        if char == "[":
            return self._parse_array()
        if char in {'"', "'"}:
            return self._parse_string()
        if char.isdigit() or char in "+-." and self._looks_numeric():
            return self._parse_number()
        identifier = self._parse_identifier()
        self._skip_space()
        if self.position < len(self.value) and self.value[self.position] == "#":
            self.position += 1
            return {
                "__type__": "hash",
                "name": identifier,
                "value": self._parse_value(),
            }
        upper = identifier.upper()
        if upper == "TRUE":
            return True
        if upper == "FALSE":
            return False
        if upper in {"NULL", "NONE"}:
            return None
        return identifier

    def _looks_numeric(self) -> bool:
        tail = self.value[self.position :]
        return bool(re.match(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", tail))

    def _parse_number(self) -> int | float:
        match = re.match(
            r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",
            self.value[self.position :],
        )
        if not match:
            raise DSLParseError("invalid number", self.position)
        token = match.group(0)
        self.position += len(token)
        try:
            number = Decimal(token)
        except InvalidOperation as exc:
            raise DSLParseError("invalid number", self.position - len(token)) from exc
        return decimal_json(number)

    def _parse_identifier(self) -> str:
        self._skip_space()
        start = self.position
        while self.position < len(self.value):
            char = self.value[self.position]
            if char.isspace() or char in "{}[]:,#~|":
                break
            self.position += 1
        if self.position == start:
            raise DSLParseError("expected identifier", self.position)
        return self.value[start : self.position]

    def _parse_string(self) -> str:
        quote = self.value[self.position]
        self.position += 1
        result: list[str] = []
        while self.position < len(self.value):
            char = self.value[self.position]
            self.position += 1
            if char == quote:
                return "".join(result)
            if char == "\\":
                if self.position >= len(self.value):
                    raise DSLParseError("unterminated escape", self.position)
                escaped = self.value[self.position]
                self.position += 1
                result.append(
                    {
                        "n": "\n",
                        "r": "\r",
                        "t": "\t",
                        "\\": "\\",
                        '"': '"',
                        "'": "'",
                    }.get(escaped, escaped)
                )
            else:
                result.append(char)
        raise DSLParseError("unterminated string", self.position)

    def _parse_object(self) -> dict[str, Any]:
        self.position += 1
        result: dict[str, Any] = {}
        duplicates: list[str] = []
        while True:
            self._skip_space()
            if self._peek() == "}":
                self.position += 1
                if duplicates:
                    result["__duplicate_keys__"] = duplicates
                return result
            if self._peek() in {'"', "'"}:
                key = self._parse_string()
            else:
                key = self._parse_identifier()
            self._skip_space()
            if self._peek() != ":":
                raise DSLParseError("expected ':'", self.position)
            self.position += 1
            parsed = self._parse_value()
            if key in result:
                duplicates.append(key)
            result[key] = parsed
            self._skip_space()
            char = self._peek()
            if char == ",":
                self.position += 1
                continue
            if char == "}":
                continue
            raise DSLParseError("expected ',' or '}'", self.position)

    def _parse_array(self) -> list[Any]:
        self.position += 1
        result: list[Any] = []
        while True:
            self._skip_space()
            if self._peek() == "]":
                self.position += 1
                return result
            result.append(self._parse_value())
            self._skip_space()
            char = self._peek()
            if char == ",":
                self.position += 1
                continue
            if char == "]":
                continue
            raise DSLParseError("expected ',' or ']'", self.position)
