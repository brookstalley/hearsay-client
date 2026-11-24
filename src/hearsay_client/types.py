"""The wire contract's request and response shapes, as the client sends and reads them (`api-contract.md`).

The client is a tolerant reader: a response field it does not know is ignored, and an enum value it does not know is
kept as the string it was sent, so a service that adds to its contract within a major never breaks it.
"""

import math
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any


class Misshapen(ValueError):
    """An answer that does not match the contract: a field missing, of the wrong JSON type, or a value that does not
    parse. The client raises it as ``HearsayError("unexpected_response")``; nothing else is, so a bug in the client's
    own code is never reported as the service's."""

# --- Times --------------------------------------------------------------------------------------------------------


def wire_time(value: datetime) -> str:
    """A time as the contract writes it: RFC 3339 in UTC, ending in ``Z``.

    :raises ValueError: the time has no time zone, so which instant it names is unknown
    """
    if value.tzinfo is None:
        raise ValueError("a time sent to Hearsay must carry its time zone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _instant(value: str) -> datetime:
    """A time as the contract sends it, RFC 3339 with its offset. One without an offset names no instant.

    :raises Misshapen: the value does not parse, or carries no offset
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise Misshapen(f"{value!r} is not an RFC 3339 time") from error
    if parsed.tzinfo is None:
        raise Misshapen(f"{value!r} carries no time zone")
    return parsed


def _time(value: str | None) -> datetime | None:
    return None if value is None else _instant(value)


def _day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise Misshapen(f"{value!r} is not a calendar day") from error


def _day_or_none(value: str | None) -> date | None:
    return None if value is None else _day(value)


# --- Reading a field ----------------------------------------------------------------------------------------------

# Each reader below takes one field of an answer and checks its JSON type, so a wrong-typed field is refused where it
# is read rather than handed to the caller. ``nullable`` lets the field be null; ``optional`` lets it be absent, read
# as ``default``, and is given only where the contract leaves a field out (a tell's answer omits what does not apply):
# absent and null mean different things. A refusal is a Misshapen, which the client raises as ``unexpected_response``.
# A JSON boolean is never read as a number, though Python counts ``bool`` as an ``int``, and a number is finite.


def _field(body: Any, name: str, kinds: tuple[type, ...], *, nullable: bool, optional: bool, default: Any) -> Any:
    if not isinstance(body, dict):
        raise Misshapen(f"an object was expected to hold {name}, not {type(body).__name__}")
    if name not in body:
        if optional:
            return default
        raise Misshapen(f"{name} is missing")
    value = body[name]
    if value is None and nullable:
        return None
    if not isinstance(value, kinds) or (isinstance(value, bool) and bool not in kinds):
        raise Misshapen(f"{name} is {type(value).__name__}, not {' or '.join(kind.__name__ for kind in kinds)}")
    if isinstance(value, float) and not math.isfinite(value):
        raise Misshapen(f"{name} is {value}, not a finite number")
    return value


def _str(body: Any, name: str, *, nullable: bool = False, optional: bool = False) -> Any:
    return _field(body, name, (str,), nullable=nullable, optional=optional, default=None)


def _int(body: Any, name: str, *, nullable: bool = False, optional: bool = False) -> Any:
    return _field(body, name, (int,), nullable=nullable, optional=optional, default=None)


def _number(body: Any, name: str, *, nullable: bool = False, optional: bool = False) -> Any:
    return _field(body, name, (int, float), nullable=nullable, optional=optional, default=None)


def _bool(body: Any, name: str, *, nullable: bool = False, optional: bool = False, default: Any = None) -> Any:
    return _field(body, name, (bool,), nullable=nullable, optional=optional, default=default)


def _object(body: Any, name: str, *, nullable: bool = False) -> Any:
    return _field(body, name, (dict,), nullable=nullable, optional=False, default=None)


def _list(body: Any, name: str, *, optional: bool = False) -> list[Any]:
    value: list[Any] = _field(body, name, (list,), nullable=False, optional=optional, default=[])
    return value


def _strings(body: Any, name: str, *, optional: bool = False) -> list[str]:
    """A list of strings, each checked."""
    values = _list(body, name, optional=optional)
    for value in values:
        if not isinstance(value, str):
            raise Misshapen(f"{name} holds {type(value).__name__}, not str")
    return list(values)


# --- Common -------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Stamp:
    """What every success response carries: the policy it was read under and the store state it saw or wrote."""

    policy: str | None
    policy_version: str | None
    store_state: int
    latest_store_state: int
    behind: bool
    contract: str

    @classmethod
    def read(cls, body: dict[str, Any]) -> "Stamp":
        return cls(
            policy=_str(body, "policy", nullable=True),
            policy_version=_str(body, "policy_version", nullable=True),
            store_state=_int(body, "store_state"),
            latest_store_state=_int(body, "latest_store_state"),
            behind=_bool(body, "behind"),
            contract=_str(body, "contract"),
        )


@dataclass(frozen=True)
class ServedContract:
    version: str
    status: str
    sunset: date | None


@dataclass(frozen=True)
class Handshake:
    """``GET /contract``: which contract versions the service serves, to which product, in which environment."""

    service: str
    environment: str
    product: str
    roles: list[str]
    contracts: list[ServedContract]
    default_policy: str | None
    default_policy_version: str | None
    store_state: int

    @classmethod
    def read(cls, body: dict[str, Any]) -> "Handshake":
        return cls(
            service=_str(body, "service"),
            environment=_str(body, "environment"),
            product=_str(body, "product"),
            roles=_strings(body, "roles"),
            contracts=[
                ServedContract(
                    version=_str(served, "version"),
                    status=_str(served, "status"),
                    sunset=_day_or_none(_str(served, "sunset", nullable=True, optional=True)),
                )
                for served in _list(body, "contracts")
            ],
            default_policy=_str(body, "default_policy", nullable=True),
            default_policy_version=_str(body, "default_policy_version", nullable=True),
            store_state=_int(body, "store_state"),
        )


# --- Readers ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Reader:
    reader: str
    created: bool
    stamp: Stamp


# --- Tells --------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Document:
    """A fetched page's extracted text, or its ``sha256`` alone when this product can already read it."""

    text: str | None = None
    sha256: str | None = None
    mime_type: str | None = None

    def wire(self) -> dict[str, Any]:
        return {"text": self.text, "sha256": self.sha256, "mime_type": self.mime_type}


@dataclass(frozen=True)
class Fetch:
    """One fetch, as ``POST /tell/fetch`` takes it. ``document`` is given exactly when ``outcome`` is ``read``."""

    reader: str
    url_requested: str
    retrieved_at: datetime
    outcome: str
    url_final: str | None = None
    witness_audience: str = "world"
    reason: str | None = None
    chars_read: int | None = None
    chars_total: int | None = None
    search: str | None = None
    published_at: datetime | None = None
    etag: str | None = None
    last_modified: datetime | None = None
    document: Document | None = None

    def wire(self) -> dict[str, Any]:
        return {
            "reader": self.reader,
            "witness_audience": self.witness_audience,
            "url_requested": self.url_requested,
            "url_final": self.url_final,
            "retrieved_at": wire_time(self.retrieved_at),
            "outcome": self.outcome,
            "reason": self.reason,
            "chars_read": self.chars_read,
            "chars_total": self.chars_total,
            "search": self.search,
            "published_at": None if self.published_at is None else wire_time(self.published_at),
            "etag": self.etag,
            "last_modified": None if self.last_modified is None else wire_time(self.last_modified),
            "document": None if self.document is None else self.document.wire(),
        }


@dataclass(frozen=True)
class FetchTold:
    """A kept fetch. ``request`` is None only on a replay whose first answer was cleared, 30 days after it was
    given: the fetch is kept, and ``stamp`` names the store state it was written at, but its ids are not returned."""

    request: str | None
    document: str | None
    document_new: bool | None
    replayed: bool
    stamp: Stamp


@dataclass(frozen=True)
class SearchResult:
    rank: int
    url: str
    title: str | None
    snippet: str | None = None
    score: float | None = None

    def wire(self) -> dict[str, Any]:
        """The result as a tell sends it. A title may read as None only once erased, so a tell must give one.

        :raises ValueError: the result has no title
        """
        if self.title is None:
            raise ValueError("a search result told to Hearsay needs its title")
        return {"rank": self.rank, "url": self.url, "title": self.title, "snippet": self.snippet, "score": self.score}

    @classmethod
    def read(cls, body: dict[str, Any]) -> "SearchResult":
        return cls(
            rank=_int(body, "rank"),
            url=_str(body, "url"),
            title=_str(body, "title", nullable=True),
            snippet=_str(body, "snippet", nullable=True),
            score=_number(body, "score", nullable=True),
        )


@dataclass(frozen=True)
class Search:
    """One search, as ``POST /tell/search`` takes it, including one that found nothing. A search is always the
    product's own, so its audience is not a choice."""

    reader: str
    provider: str
    query: str
    scope: dict[str, Any]
    retrieved_at: datetime
    results: list[SearchResult]
    duration_ms: int | None = None
    error: str | None = None
    degradation: dict[str, Any] | None = None

    def wire(self) -> dict[str, Any]:
        return {
            "reader": self.reader,
            "audience": "product",
            "provider": self.provider,
            "query": self.query,
            "scope": self.scope,
            "retrieved_at": wire_time(self.retrieved_at),
            "duration_ms": self.duration_ms,
            "result_count": len(self.results),
            "error": self.error,
            "degradation": self.degradation,
            "results": [result.wire() for result in self.results],
        }


@dataclass(frozen=True)
class SearchTold:
    """A kept search. ``event`` is None only on a replay whose first answer was cleared, as for :class:`FetchTold`."""

    event: str | None
    replayed: bool
    stamp: Stamp


# --- Look-ups -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Witness:
    """The world's record that a URL returned a document on a day, by some product it does not name."""

    event: str
    audience: str
    url_requested: str
    url_final: str | None
    retrieved_on: date
    document: str
    chars_read: int | None
    chars_total: int | None
    etag: str | None
    last_modified: datetime | None
    published_at: datetime | None

    @classmethod
    def read(cls, body: dict[str, Any]) -> "Witness":
        return cls(
            event=_str(body, "event"),
            audience=_str(body, "audience"),
            url_requested=_str(body, "url_requested"),
            url_final=_str(body, "url_final", nullable=True),
            retrieved_on=_day(_str(body, "retrieved_on")),
            document=_str(body, "document"),
            chars_read=_int(body, "chars_read", nullable=True),
            chars_total=_int(body, "chars_total", nullable=True),
            etag=_str(body, "etag", nullable=True),
            last_modified=_time(_str(body, "last_modified", nullable=True)),
            published_at=_time(_str(body, "published_at", nullable=True)),
        )


@dataclass(frozen=True)
class MyFetch:
    """One of this product's own fetch requests, with its exact time, and its witness once the day's batch ran."""

    request: str
    url_requested: str
    url_final: str | None
    retrieved_at: datetime
    outcome: str
    document: str | None
    witness: str | None

    @classmethod
    def read(cls, body: dict[str, Any]) -> "MyFetch":
        return cls(
            request=_str(body, "request"),
            url_requested=_str(body, "url_requested"),
            url_final=_str(body, "url_final", nullable=True),
            retrieved_at=_instant(_str(body, "retrieved_at")),
            outcome=_str(body, "outcome"),
            document=_str(body, "document", nullable=True),
            witness=_str(body, "witness", nullable=True),
        )


@dataclass(frozen=True)
class UrlSeen:
    """Q1. Both lists empty means the URL was never seen: a reading, not a failure."""

    witnesses: list[Witness]
    mine: list[MyFetch]
    stamp: Stamp


@dataclass(frozen=True)
class SeenSearch:
    """One of this product's searches, as it was told. A value whose text was erased is None and named in
    ``erased``, so an erased error never reads as a search that did not fail."""

    event: str
    reader: str
    provider: str | None
    query: str | None
    scope: dict[str, Any] | None
    retrieved_at: datetime
    duration_ms: int | None
    result_count: int
    error: str | None
    degradation: dict[str, Any] | None
    results: list[SearchResult]
    erased: list[str] = field(default_factory=list)

    @classmethod
    def read(cls, body: dict[str, Any]) -> "SeenSearch":
        return cls(
            event=_str(body, "event"),
            reader=_str(body, "reader"),
            provider=_str(body, "provider", nullable=True),
            query=_str(body, "query", nullable=True),
            scope=_object(body, "scope", nullable=True),
            retrieved_at=_instant(_str(body, "retrieved_at")),
            duration_ms=_int(body, "duration_ms", nullable=True),
            result_count=_int(body, "result_count"),
            error=_str(body, "error", nullable=True),
            degradation=_object(body, "degradation", nullable=True),
            results=[SearchResult.read(result) for result in _list(body, "results")],
            erased=_strings(body, "erased"),
        )


@dataclass(frozen=True)
class SearchesSeen:
    """Q2. An empty list means this product never ran this search."""

    searches: list[SeenSearch]
    stamp: Stamp


@dataclass(frozen=True)
class KeptDocument:
    """A kept page's text, or the range of it asked for, in characters."""

    document: str
    length: int
    start: int
    end: int
    text: str
    mime_type: str | None
    stamp: Stamp
