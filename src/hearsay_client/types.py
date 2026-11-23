"""The wire contract's request and response shapes, as the client sends and reads them (`api-contract.md`).

The client is a tolerant reader: a response field it does not know is ignored, and an enum value it does not know is
kept as the string it was sent, so a service that adds to its contract within a major never breaks it.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

# --- Times --------------------------------------------------------------------------------------------------------


def wire_time(value: datetime) -> str:
    """A time as the contract writes it: RFC 3339 in UTC, ending in ``Z``.

    :raises ValueError: the time has no time zone, so which instant it names is unknown
    """
    if value.tzinfo is None:
        raise ValueError("a time sent to Hearsay must carry its time zone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)


def _day(value: str) -> date:
    return date.fromisoformat(value)


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
            policy=body["policy"],
            policy_version=body["policy_version"],
            store_state=body["store_state"],
            latest_store_state=body["latest_store_state"],
            behind=body["behind"],
            contract=body["contract"],
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
            service=body["service"],
            environment=body["environment"],
            product=body["product"],
            roles=list(body["roles"]),
            contracts=[
                ServedContract(
                    version=served["version"],
                    status=served["status"],
                    sunset=None if served.get("sunset") is None else _day(served["sunset"]),
                )
                for served in body["contracts"]
            ],
            default_policy=body["default_policy"],
            default_policy_version=body["default_policy_version"],
            store_state=body["store_state"],
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
        return cls(body["rank"], body["url"], body["title"], body.get("snippet"), body.get("score"))


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
            event=body["event"],
            audience=body["audience"],
            url_requested=body["url_requested"],
            url_final=body.get("url_final"),
            retrieved_on=_day(body["retrieved_on"]),
            document=body["document"],
            chars_read=body.get("chars_read"),
            chars_total=body.get("chars_total"),
            etag=body.get("etag"),
            last_modified=_time(body.get("last_modified")),
            published_at=_time(body.get("published_at")),
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
            request=body["request"],
            url_requested=body["url_requested"],
            url_final=body.get("url_final"),
            retrieved_at=datetime.fromisoformat(body["retrieved_at"]),
            outcome=body["outcome"],
            document=body.get("document"),
            witness=body.get("witness"),
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
    degradation: Any
    results: list[SearchResult]
    erased: list[str] = field(default_factory=list)

    @classmethod
    def read(cls, body: dict[str, Any]) -> "SeenSearch":
        return cls(
            event=body["event"],
            reader=body["reader"],
            provider=body["provider"],
            query=body["query"],
            scope=body["scope"],
            retrieved_at=datetime.fromisoformat(body["retrieved_at"]),
            duration_ms=body.get("duration_ms"),
            result_count=body["result_count"],
            error=body.get("error"),
            degradation=body.get("degradation"),
            results=[SearchResult.read(result) for result in body["results"]],
            erased=list(body.get("erased", [])),
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
