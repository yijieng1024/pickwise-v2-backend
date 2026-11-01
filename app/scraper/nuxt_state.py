"""
Read a Nuxt page's server-rendered state out of its raw HTML, with no browser
and no JavaScript runtime.

Nuxt 3 serialises its payload with `devalue` into
`<script id="__NUXT_DATA__">`: one flat JSON array whose element 0 is the
root, and in which every object value / array item is an *index* into that
same array. Negative indices are special values, not Python negative indexing
(reading -1 as "last element" was the survey script's bug). A list starting
with a string is a typed value (`["Reactive", i]`, `["Set", i, j]`, ...).

Nuxt 2 puts `window.__NUXT__=(function(a,b){...return {...}}(1,2))` in a script
tag. `nuxt2_state` reads that as literal syntax and never executes it, so it
needs no JavaScript runtime. Real pages fill shared objects before the `return`
(`a.b=c;`, `d[0]={...};`), and those statements are applied, not skipped: data
hangs off them. Only those two forms exist on the pages, so anything else raises.

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


_NUXT2_MARK = "window.__NUXT__="
_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_NUMBER_RE = re.compile(r"0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def nuxt2_state(html: str) -> dict:
    """The value of `window.__NUXT__` on a Nuxt 2 page. Raises NuxtStateError."""
    start = html.find(_NUXT2_MARK)
    if start < 0:
        raise NuxtStateError("no window.__NUXT__ in the page (not a Nuxt 2 page, or a block/challenge page)")
    end = html.find("</script>", start)
    if end < 0:
        raise NuxtStateError("window.__NUXT__ has no closing script tag")
    source = html[start + len(_NUXT2_MARK):end]
    try:
        root = _read_nuxt2(source)
    except (IndexError, ValueError, KeyError, TypeError) as exc:
        if isinstance(exc, NuxtStateError):
            raise
        raise NuxtStateError(f"window.__NUXT__ could not be read: {exc!r}") from exc
    if not isinstance(root, dict):
        raise NuxtStateError("window.__NUXT__ is not an object")
    return root


def _read_nuxt2(source: str) -> object:
    outer = _Literal(source, {})
    for token in ("(", "function", "("):
        outer.eat(token)
    params = []
    while outer.peek() != ")":
        params.append(outer.ident())
        outer.skip(",")
    outer.eat(")")
    outer.eat("{")

    # The arguments come after the body, but the body cannot be read without
    # them, so step over it first.
    body_start = outer.i
    body_end = _closing_brace(source, body_start)
    outer.i = body_end + 1
    outer.eat("(")
    args = []
    while outer.peek() != ")":
        args.append(outer.value())
        outer.skip(",")
    outer.eat(")")
    outer.eat(")")
    outer.skip(";")
    outer.at_end()

    body = _Literal(source[body_start:body_end], dict(zip(params, args + [None] * len(params))))
    while not body.at_return():
        body.assign()
    body.eat("return")
    root = body.value()
    body.skip(";")
    body.at_end()
    return root


def _closing_brace(text: str, start: int) -> int:
    """Index of the `}` closing the block that starts at *start*."""
    depth, i, quote = 0, start, None
    while i < len(text):
        char = text[i]
        if quote:
            if char == "\\":
                i += 1
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "{":
            depth += 1
        elif char == "}":
            if depth == 0:
                return i
            depth -= 1
        i += 1
    raise NuxtStateError("unbalanced braces in window.__NUXT__")


class _Literal:
    """A reader for the JavaScript literals Nuxt 2 emits, over one piece of
    source. Identifiers resolve in *env*; one it does not hold raises."""

    def __init__(self, text: str, env: dict):
        self.text = text
        self.env = env
        self.i = 0

    def _spaces(self) -> None:
        while self.i < len(self.text) and self.text[self.i] in " \t\r\n":
            self.i += 1

    def peek(self) -> str:
        self._spaces()
        return self.text[self.i:self.i + 1]

    def _error(self, what: str) -> NuxtStateError:
        return NuxtStateError(f"{what} at {self.i}: {self.text[self.i:self.i + 60]!r}")

    def eat(self, token: str) -> None:
        self._spaces()
        if not self.text.startswith(token, self.i):
            raise self._error(f"expected {token!r}")
        self.i += len(token)

    def skip(self, token: str) -> None:
        if self.peek() == token:
            self.i += 1

    def at_end(self) -> None:
        if self.peek():
            raise self._error("unexpected text after the value")

    def at_return(self) -> bool:
        self._spaces()
        match = _IDENT_RE.match(self.text, self.i)
        return bool(match) and match.group() == "return"

    def ident(self) -> str:
        self._spaces()
        match = _IDENT_RE.match(self.text, self.i)
        if not match:
            raise self._error("expected an identifier")
        self.i = match.end()
        return match.group()

    def lookup(self, name: str) -> object:
        if name not in self.env:
            raise self._error(f"unknown identifier {name!r}")
        return self.env[name]

    def assign(self) -> None:
        """One `name.key=value;` or `name[index]=value;` before the return."""
        target = self.lookup(self.ident())
        if self.peek() == ".":
            self.eat(".")
            key: object = self.ident()
        else:
            self.eat("[")
            key = self.number()
            self.eat("]")
        self.eat("=")
        value = self.value()
        self.skip(";")
        if isinstance(target, dict) and isinstance(key, str):
            target[key] = value
        elif isinstance(target, list) and isinstance(key, int) and 0 <= key < len(target):
            target[key] = value
        else:
            raise self._error(f"cannot assign {key!r} on a {type(target).__name__}")

    def value(self) -> object:
        char = self.peek()
        if char == "{":
            return self._object()
        if char == "[":
            return self._array()
        if char and char in "\"'":
            return self.string()
        if char == "-":
            self.eat("-")
            number = self.value()
            if not isinstance(number, (int, float)) or isinstance(number, bool):
                raise self._error("minus on something that is not a number")
            return -number
        if char == "." or char.isdigit():
            return self.number()
        word = self.ident()
        if word == "void":
            self.value()
            return None
        if word == "new":
            self.ident()
            self.eat("(")
            value = self.value()
            self.eat(")")
            return value
        if word == "Array":
            self.eat("(")
            size = self.value()
            self.eat(")")
            return [None] * size
        if word == "true":
            return True
        if word == "false":
            return False
        if word in ("null", "undefined"):
            return None
        if word == "NaN":
            return math.nan
        if word == "Infinity":
            return math.inf
        return self.lookup(word)

    def _object(self) -> dict:
        self.eat("{")
        result = {}
        while self.peek() != "}":
            char = self.peek()
            if char and char in "\"'":
                key: object = self.string()
            elif char.isdigit():
                key = self.number()
            else:
                key = self.ident()
            if self.peek() in (",", "}"):
                result[key] = self.lookup(key)  # shorthand
            else:
                self.eat(":")
                result[str(key)] = self.value()
            self.skip(",")
        self.eat("}")
        return result

    def _array(self) -> list:
        self.eat("[")
        result = []
        while self.peek() != "]":
            result.append(self.value())
            self.skip(",")
        self.eat("]")
        return result

    def number(self) -> int | float:
        self._spaces()
        match = _NUMBER_RE.match(self.text, self.i)
        if not match:
            raise self._error("expected a number")
        self.i = match.end()
        text = match.group()
        if text[:2].lower() == "0x":
            return int(text, 16)
        return float(text) if any(c in text for c in ".eE") else int(text)

    def string(self) -> str:
        self._spaces()
        quote = self.text[self.i]
        i = self.i + 1
        out = []
        while self.text[i] != quote:
            if self.text[i] != "\\":
                out.append(self.text[i])
                i += 1
                continue
            escape = self.text[i + 1]
            if escape == "u":
                out.append(chr(int(self.text[i + 2:i + 6], 16)))
                i += 6
            elif escape == "x":
                out.append(chr(int(self.text[i + 2:i + 4], 16)))
                i += 4
            else:
                out.append(_ESCAPES.get(escape, escape))
                i += 2
        self.i = i + 1
        # A character outside the BMP arrives as two \u escapes: join the pair
        return "".join(out).encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


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
