"""
Read a Nuxt page's server-rendered state out of its raw HTML, with no browser
and no JavaScript runtime.

Nuxt 3 serialises its payload with `devalue` into
`<script id="__NUXT_DATA__">`: one flat JSON array whose element 0 is the
root, and in which every object value / array item is an *index* into that
same array. Negative indices are special values, not Python negative indexing
(reading -1 as "last element" was the survey script's bug). A list starting
with a string is a typed value (`["Reactive", i]`, `["Set", i, j]`, ...).

Strict on purpose: an unknown type tag, a cycle or a bad index raises instead
of handing back a half-revived structure, because a page whose shape changed
must fail loudly rather than scrape as "no variants".
"""
import json
import math
import re

_NUXT_DATA_RE = re.compile(r'<script[^>]*\bid="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S)

# devalue's special values
_SPECIAL = {
    -1: None,           # undefined
    -2: None,           # array hole
    -3: math.nan,
    -4: math.inf,
    -5: -math.inf,
    -6: -0.0,
}

# Nuxt's own reducers wrap one value; the wrapper carries nothing we need
_WRAPPERS = {"Reactive", "ShallowReactive", "Ref", "ShallowRef"}


class NuxtStateError(ValueError):
    pass


def nuxt3_state(html: str) -> object:
    """The revived Nuxt 3 payload (root object) of a page. Raises NuxtStateError."""
    match = _NUXT_DATA_RE.search(html)
    if not match:
        raise NuxtStateError("no __NUXT_DATA__ script in the page (not a Nuxt 3 page, or a block/challenge page)")
    try:
        flat = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise NuxtStateError(f"__NUXT_DATA__ is not valid JSON: {exc}") from exc
    if not isinstance(flat, list) or not flat:
        raise NuxtStateError("__NUXT_DATA__ is not a non-empty array")
    return _Reviver(flat).revive(0)


class _Reviver:
    def __init__(self, flat: list):
        self.flat = flat
        self.done: dict[int, object] = {}
        self.in_progress: set[int] = set()

    def revive(self, index: object) -> object:
        if not isinstance(index, int) or isinstance(index, bool):
            raise NuxtStateError(f"expected an index, got {index!r}")
        if index < 0:
            if index not in _SPECIAL:
                raise NuxtStateError(f"unknown special index {index}")
            return _SPECIAL[index]
        if index >= len(self.flat):
            raise NuxtStateError(f"index {index} out of range ({len(self.flat)} entries)")
        if index in self.done:
            return self.done[index]  # shared reference: the same object again
        if index in self.in_progress:
            raise NuxtStateError(f"cycle through index {index}")
        self.in_progress.add(index)
        value = self._revive_value(self.flat[index])
        self.in_progress.discard(index)
        self.done[index] = value
        return value

    def _revive_value(self, value: object) -> object:
        if isinstance(value, dict):
            return {key: self.revive(i) for key, i in value.items()}
        if not isinstance(value, list):
            return value  # str / number / bool / null
        if not value or not isinstance(value[0], str):
            return [self.revive(i) for i in value]

        tag, args = value[0], value[1:]
        # Nuxt's revivers take one revived argument
        if tag in _WRAPPERS or tag == "NuxtError":
            return self.revive(args[0])
        if tag in ("EmptyRef", "EmptyShallowRef"):
            data = self.revive(args[0])
            return None if data == "_" else data
        # devalue's built-ins carry literals, not indices (kept in string form)
        if tag in ("Date", "RegExp", "BigInt", "Object"):
            return args[0]
        if tag == "Set":
            return [self.revive(i) for i in args]
        if tag == "Map":
            return {self.revive(k): self.revive(v) for k, v in zip(args[::2], args[1::2])}
        if tag == "null":  # null-prototype object: literal key, value index pairs
            return {k: self.revive(v) for k, v in zip(args[::2], args[1::2])}
        raise NuxtStateError(f"unknown devalue type tag {tag!r}")
