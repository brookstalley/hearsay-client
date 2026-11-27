"""The Hearsay client: the startup handshake, typed tells and look-ups, and capped retries (`api-contract.md`).

Open it with :meth:`HearsayClient.connect`, or as an async context manager, which calls it. The handshake fails at
startup, before any data call, when the service does not serve this client's contract major, serves it at an older
minor, or belongs to another environment than the one the client was configured for (handoff #5, #4).

A failure is raised, never turned into an empty answer: a timeout, an unreachable service or a 5xx means there is
no stored result, not that the store holds nothing. Every failure is a :class:`HearsayError`, an answer whose body
does not match the contract included (``unexpected_response``), so a caller catches that one type and nothing that
could hide its own bugs. A service that does not answer fails the call at once, so a
caller looking up before a fetch carries on without reuse rather than waiting out retries; a tell that fails is the
caller's to queue.
"""

import asyncio
import logging
import random
import re
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self, TypeVar

import httpx

from hearsay_client.lookup import LookedUp, Lookup
from hearsay_client.types import (
    AboutTold,
    CurationLoaded,
    CurationTerm,
    CurationToken,
    Fetch,
    FetchTold,
    Handshake,
    KeptDocument,
    Misshapen,
    MyFetch,
    Reader,
    ReaderRelay,
    ReaderStatement,
    RelationDeclared,
    Relations,
    Search,
    SearchesSeen,
    SearchTold,
    SeenSearch,
    ServedContract,
    Stamp,
    StatementsTold,
    UrlSeen,
    Utterance,
    UtteranceTold,
    Witness,
    Witnessed,
    WitnessedTold,
    _bool,
    _int,
    _list,
    _object,
    _str,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

#: The contract version this client is built against. It is sent on every call but the handshake.
CONTRACT_VERSION = "1.5"

CONTRACT_HEADER = "Hearsay-Contract"
IDEMPOTENCY_HEADER = "Idempotency-Key"

_VERSION = re.compile(r"(\d+)\.(\d+)")


class HearsayError(Exception):
    """A call failed. ``code`` is the contract's error code, or ``unreachable`` when no answer came back.

    :ivar status: the HTTP status, or None when no answer came back
    :ivar retryable: whether the contract says the call may be tried again
    :ivar request_id: the id the service gave the call, for its operator's logs
    :ivar errors: each failing field's path and code
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        error_class: str | None = None,
        retryable: bool = False,
        request_id: str | None = None,
        errors: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.status = status
        self.error_class = error_class
        self.retryable = retryable
        self.request_id = request_id
        self.errors = errors or []


class ContractMismatchError(HearsayError):
    """The service cannot serve this client: another environment, or no contract version this client can use."""


@dataclass(frozen=True)
class RetryPolicy:
    """How a failed idempotent call is tried again: one wait before each retry, so ``waits`` also caps their
    number. Each wait is stretched by up to ``jitter`` of itself, and is never shorter than a ``Retry-After``."""

    waits: tuple[float, ...] = (0.2, 1.0, 5.0)
    jitter: float = 0.25


def _version(text: str) -> tuple[int, int]:
    matched = _VERSION.fullmatch(text)
    if matched is None:
        # Misshapen, a ValueError: a served version that does not parse is an answer that does not match the contract.
        raise Misshapen(f"{text!r} is not a MAJOR.MINOR contract version")
    return int(matched[1]), int(matched[2])


def check_handshake(handshake: Handshake, *, contract: str, environment: str) -> ServedContract:
    """The served contract this client will use, or why it cannot use any.

    :raises ContractMismatchError: the service is not Hearsay, is in another environment, does not serve the
        client's major, or serves it at a minor older than the client's
    """
    if handshake.service != "hearsay":
        raise ContractMismatchError("contract_unsupported", f"the service is {handshake.service!r}, not hearsay")
    if handshake.environment != environment:
        raise ContractMismatchError(
            "contract_unsupported",
            f"the service is in environment {handshake.environment!r}, and this client was configured for "
            f"{environment!r}",
        )
    major, minor = _version(contract)
    same_major = [served for served in handshake.contracts if _version(served.version)[0] == major]
    offered = ", ".join(served.version for served in handshake.contracts) or "nothing"
    if not same_major:
        raise ContractMismatchError(
            "contract_unsupported", f"the service serves {offered}, and this client is built for {contract}"
        )
    newest = max(same_major, key=lambda served: _version(served.version)[1])
    if _version(newest.version)[1] < minor:
        raise ContractMismatchError(
            "contract_unsupported", f"the service serves {newest.version}, older than this client's {contract}"
        )
    if newest.status == "deprecated":
        log.warning("Hearsay contract %s is deprecated, sunset %s", newest.version, newest.sunset)
    return newest


def _problem(response: httpx.Response) -> HearsayError:
    """The error a failed response carries, read as the contract's problem object where it is one. A problem object
    whose fields are of the wrong type is ``unexpected_response`` too, retryable only as a gateway failure is: a
    ``retryable`` that is not a boolean is never read as one."""
    try:
        body = response.json()
    except ValueError:
        body = None
    gateway = response.status_code in (502, 503, 504)
    if not isinstance(body, dict) or "code" not in body:
        # Not the service's answer, such as a proxy's page. A gateway failure may pass; anything else is not ours.
        return HearsayError(
            "unexpected_response",
            f"HTTP {response.status_code} without a problem object",
            status=response.status_code,
            retryable=gateway,
            request_id=response.headers.get("request-id"),
        )
    try:
        return HearsayError(
            _str(body, "code"),
            _str(body, "title", nullable=True, optional=True) or "",
            status=response.status_code,
            error_class=_str(body, "class", nullable=True, optional=True),
            retryable=_bool(body, "retryable", optional=True, default=False),
            request_id=_str(body, "request_id", nullable=True, optional=True),
            errors=[(_str(error, "path"), _str(error, "code")) for error in _list(body, "errors", optional=True)],
        )
    except Misshapen as error:
        return HearsayError(
            "unexpected_response",
            f"HTTP {response.status_code} with a problem object that does not match the contract ({error})",
            status=response.status_code,
            retryable=gateway,
            request_id=response.headers.get("request-id"),
        )


@dataclass(frozen=True)
class _Answer:
    """A successful call's parsed body, with what an error about it must carry for the service's operator."""

    body: dict[str, Any]
    status: int
    request_id: str | None


def _shaped(what: str, answer: _Answer, read: Callable[[dict[str, Any]], T]) -> T:
    """``read(body)``, which builds a call's answer from its body, with a body of the wrong shape raised as
    ``unexpected_response``: a missing field, a field of the wrong type or a value that does not parse. The error
    carries the call's status and request id, so the service's log line for it can be found. Only the readers'
    :class:`Misshapen` is caught, so a bug in the client's own code surfaces as itself, never as the service's.

    :raises HearsayError: ``unexpected_response``
    """
    try:
        return read(answer.body)
    except Misshapen as error:
        raise HearsayError(
            "unexpected_response",
            f"the answer to {what} does not match the contract ({error})",
            status=answer.status,
            request_id=answer.request_id,
        ) from error


def _told_id(body: dict[str, Any], name: str) -> str | None:
    """A tell's id, which a replay whose first answer was cleared after 30 days does not carry. Any other answer
    without it does not match the contract, and fails as ``unexpected_response``."""
    if isinstance(body, dict) and name not in body and body.get("replayed") is True:
        return None
    value: str = _str(body, name)
    return value


def _retry_after(response: httpx.Response) -> float:
    value = response.headers.get("retry-after")
    try:
        return max(0.0, float(value)) if value is not None else 0.0
    except ValueError:
        return 0.0


class HearsayClient:
    """Typed calls to one Hearsay service, as one product (the key names it).

    :param base_url: the service's address
    :param key: the product's bearer key
    :param environment: the environment this client is configured for; a service in another one is refused
    :param http: an HTTP client to call through; one is made, and closed with this client, when none is given
    :param retry: how failed idempotent calls are tried again
    :param timeout: seconds before a call with no answer fails, sent with every call, whichever HTTP client is used
    :param sleep: how the client waits between retries
    """

    def __init__(
        self,
        base_url: str,
        key: str,
        *,
        environment: str,
        http: httpx.AsyncClient | None = None,
        retry: RetryPolicy | None = None,
        timeout: float = 10.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._owns_http = http is None
        self._http = http if http is not None else httpx.AsyncClient()
        self._timeout = timeout
        self._base_url = base_url.rstrip("/")
        self._authorization = {"Authorization": f"Bearer {key}"}
        self._environment = environment
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self.handshake: Handshake | None = None

    async def __aenter__(self) -> Self:
        try:
            await self.connect()
        except BaseException:
            await self.close()
            raise
        return self

    async def __aexit__(
        self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.close()

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def connect(self) -> Handshake:
        """Call the handshake and check it. Every data call needs it first.

        :raises ContractMismatchError: the service cannot serve this client
        :raises HearsayError: the handshake itself failed
        """
        answer = await self._request("GET", "/contract", contract=False)
        handshake = _shaped("GET /contract", answer, Handshake.read)
        _shaped(
            "GET /contract",
            answer,
            lambda body: check_handshake(handshake, contract=CONTRACT_VERSION, environment=self._environment),
        )
        self.handshake = handshake
        return handshake

    # --- Readers ---------------------------------------------------------------------------------------------------

    async def register_reader(self, kind: str, name: str, version: str) -> Reader:
        """Register a reader at a version, or get the one already registered."""
        answer = await self._request("POST", "/readers", json={"kind": kind, "name": name, "version": version})
        return _shaped(
            "POST /readers",
            answer,
            lambda body: Reader(_str(body, "reader"), _bool(body, "created"), Stamp.read(_object(body, "stamp"))),
        )

    # --- Relations -------------------------------------------------------------------------------------------------

    async def declare_relation(
        self, name: str, means: str, *, subject: str, object: str, object_schemes: Sequence[str] = ()
    ) -> RelationDeclared:
        """Declare a relation this product uses, once: its name, a one-line meaning, the kind of its subject
        (``utterance`` or ``entity``) and of its object (``entity`` or ``value``), and the identifier schemes an entity
        object may be given by. Declaring the same body again returns it; a changed body under a used name is
        ``relation_conflict``, since a changed meaning is a new name."""
        body = {
            "name": name,
            "means": means,
            "subject": subject,
            "object": object,
            "object_schemes": list(object_schemes),
        }
        answer = await self._request("POST", "/relations", json=body)
        return _shaped(
            "POST /relations",
            answer,
            lambda body: RelationDeclared(
                _str(body, "relation"), _bool(body, "created"), Stamp.read(_object(body, "stamp"))
            ),
        )

    async def relations(self, *, min_store_state: int | None = None, on_behind: str | None = None) -> Relations:
        """The curated relations and this product's own declared ones."""
        given = (("min_store_state", min_store_state), ("on_behind", on_behind))
        params = {name: value for name, value in given if value is not None}
        answer = await self._request("GET", "/relations", params=params)
        return _shaped("GET /relations", answer, Relations.read)

    # --- Curation --------------------------------------------------------------------------------------------------

    async def load_curation(
        self, digest: str, terms: Sequence[CurationTerm], tokens: Sequence[CurationToken] = ()
    ) -> CurationLoaded:
        """Load the curation files' vocabulary (operator key only): every term and token the files declare, each meaning
        line as its SHA-256, and ``digest``, the files' own (``sha256:`` and 64 hex digits), which the loader's reader
        takes as its version. What the files no longer hold is withdrawn, so a load is the whole of the curation. A
        load changing nothing writes nothing and answers ``unchanged``; the call is idempotent, so a retry is safe."""
        body = {
            "digest": digest,
            "terms": [term.wire() for term in terms],
            "tokens": [token.wire() for token in tokens],
        }
        answer = await self._request("POST", "/curation/load", json=body)
        return _shaped("POST /curation/load", answer, CurationLoaded.read)

    # --- Utterances ------------------------------------------------------------------------------------------------

    async def tell_utterance(self, utterance: Utterance, *, idempotency_key: str | None = None) -> UtteranceTold:
        """Keep one aired segment with this product's own statements about it. Pass the segment's own stable id as
        ``idempotency_key``, so a backfill or a retry stores nothing twice."""
        answer = await self._tell("/tell/utterance", utterance.wire(), idempotency_key)
        return _shaped(
            "POST /tell/utterance",
            answer,
            lambda body: UtteranceTold(
                utterance=_told_id(body, "utterance"),
                document=_str(body, "document", nullable=True, optional=True),
                about=tuple(AboutTold.read(item) for item in _list(body, "about", optional=True)),
                replayed=_bool(body, "replayed", optional=True, default=False),
                stamp=Stamp.read(_object(body, "stamp")),
            ),
        )

    # --- Tells -----------------------------------------------------------------------------------------------------

    async def tell_fetch(self, fetch: Fetch, *, idempotency_key: str | None = None) -> FetchTold:
        """Keep a fetch. A retry, by this client or a caller passing the same key, stores nothing again."""
        answer = await self._tell("/tell/fetch", fetch.wire(), idempotency_key)
        return _shaped(
            "POST /tell/fetch",
            answer,
            lambda body: FetchTold(
                request=_told_id(body, "request"),
                document=_str(body, "document", nullable=True, optional=True),
                document_new=_bool(body, "document_new", nullable=True, optional=True),
                replayed=_bool(body, "replayed", optional=True, default=False),
                stamp=Stamp.read(_object(body, "stamp")),
            ),
        )

    async def tell_search(self, search: Search, *, idempotency_key: str | None = None) -> SearchTold:
        """Keep a search with its results, including none."""
        answer = await self._tell("/tell/search", search.wire(), idempotency_key)
        return _shaped(
            "POST /tell/search",
            answer,
            lambda body: SearchTold(
                _told_id(body, "event"),
                _bool(body, "replayed", optional=True, default=False),
                Stamp.read(_object(body, "stamp")),
            ),
        )

    async def tell_witnessed(
        self, statements: Sequence[Witnessed], *, reader: str, idempotency_key: str | None = None
    ) -> WitnessedTold:
        """Keep this product's own witnessed statements, 1 to 200, each using one of its declared relations: all of
        them, or none when one is refused. A retry with the same key stores nothing again."""
        body = {"reader": reader, "statements": [statement.wire() for statement in statements]}
        answer = await self._tell("/tell/witnessed", body, idempotency_key)
        return _shaped("POST /tell/witnessed", answer, WitnessedTold.read)

    async def tell_statements(
        self,
        statements: Sequence[ReaderStatement | ReaderRelay],
        *,
        reader: str,
        source_event: str,
        document: str,
        idempotency_key: str | None = None,
    ) -> StatementsTold:
        """Keep statements one of this product's readers read from one kept document, 1 to 200: all of them, or none
        when one is refused. ``source_event`` is the document's fetch witness (``st_…``) and ``document`` its
        ``doc_…`` id. An importer reader needs an operator's key. A retry with the same key stores nothing again.
        A relay names the statement it reports by its index in ``statements`` (contract 1.4). An aired line is read
        with ``source_event`` its world utterance (``st_…``), in the speaker's voice (:class:`SpeakerClaimant`,
        contract 1.5)."""
        return await self.tell_statement_bodies(
            [statement.wire() for statement in statements],
            reader=reader,
            source_event=source_event,
            document=document,
            idempotency_key=idempotency_key,
        )

    async def tell_statement_bodies(
        self,
        statements: Sequence[Mapping[str, Any]],
        *,
        reader: str,
        source_event: str,
        document: str,
        idempotency_key: str | None = None,
    ) -> StatementsTold:
        """:meth:`tell_statements` for statements already in the contract's wire form, each the body
        ``ReaderStatement.wire()`` gives, sent as given: for a reader that builds the wire form itself, such as a
        mapping's interpreter. Nothing is checked here that the service does not check again."""
        body = {
            "reader": reader,
            "source_event": source_event,
            "document": document,
            "statements": [dict(statement) for statement in statements],
        }
        answer = await self._tell("/tell/statements", body, idempotency_key)
        return _shaped("POST /tell/statements", answer, StatementsTold.read)

    async def _tell(self, path: str, body: dict[str, Any], idempotency_key: str | None) -> _Answer:
        # One key for every attempt, so a retry after a lost answer is replayed rather than written twice.
        key = idempotency_key or str(uuid.uuid4())
        return await self._request("POST", path, json=body, headers={IDEMPOTENCY_HEADER: key})

    # --- Look-ups --------------------------------------------------------------------------------------------------

    async def lookup_url(
        self, url: str, *, limit: int | None = None, min_store_state: int | None = None, on_behind: str | None = None
    ) -> UrlSeen:
        """Q1: the world's witnesses of ``url`` and this product's own fetches of it, newest first."""
        answer = await self._request(
            "POST", "/lookup/url", json=_read({"url": url}, limit, min_store_state, on_behind)
        )
        return _shaped(
            "POST /lookup/url",
            answer,
            lambda body: UrlSeen(
                witnesses=[Witness.read(witness) for witness in _list(body, "witnesses")],
                mine=[MyFetch.read(mine) for mine in _list(body, "mine")],
                stamp=Stamp.read(_object(body, "stamp")),
            ),
        )

    async def lookup(
        self,
        lookup: Lookup,
        *,
        limit: int | None = None,
        cursor: str | None = None,
        min_store_state: int | None = None,
        on_behind: str | None = None,
    ) -> LookedUp:
        """A structured query, answered under no policy (contract 1.3, experimental): from an entity or a typed
        identifier, along curated relations, the entities reached with the statements behind each. What comes back is
        what was said, never a verdict: read ``complete`` and ``cuts`` before treating the answers as all there are.

        A further page is the same look-up with the ``next_cursor`` the page before returned; the cursor pins the
        store state, and is refused (``cursor_invalid``) with another look-up or after ten minutes."""
        body = _read(lookup.wire(), limit, min_store_state, on_behind)
        if cursor is not None:
            body["cursor"] = cursor
        answer = await self._request("POST", "/lookup", json=body)
        return _shaped("POST /lookup", answer, LookedUp.read)

    async def lookup_search(
        self,
        provider: str,
        query: str,
        scope: Mapping[str, Any],
        *,
        limit: int | None = None,
        min_store_state: int | None = None,
        on_behind: str | None = None,
    ) -> SearchesSeen:
        """Q2: this product's searches with exactly this provider, query and scope, newest first."""
        answer = await self._request(
            "POST",
            "/lookup/search",
            json=_read(
                {"provider": provider, "query": query, "scope": dict(scope)}, limit, min_store_state, on_behind
            ),
        )
        return _shaped(
            "POST /lookup/search",
            answer,
            lambda body: SearchesSeen(
                [SeenSearch.read(seen) for seen in _list(body, "searches")], Stamp.read(_object(body, "stamp"))
            ),
        )

    async def document(
        self,
        document: str,
        *,
        start: int | None = None,
        end: int | None = None,
        min_store_state: int | None = None,
        on_behind: str | None = None,
    ) -> KeptDocument:
        """A kept page's text, or characters ``[start, end)`` of it."""
        given = (("start", start), ("end", end), ("min_store_state", min_store_state), ("on_behind", on_behind))
        params = {name: value for name, value in given if value is not None}
        answer = await self._request("GET", f"/documents/{document}", params=params)
        return _shaped(
            "GET /documents/{id}",
            answer,
            lambda body: KeptDocument(
                document=_str(body, "document"),
                length=_int(body, "length"),
                start=_int(body, "start"),
                end=_int(body, "end"),
                text=_str(body, "text"),
                mime_type=_str(body, "mime_type", nullable=True),
                stamp=Stamp.read(_object(body, "stamp")),
            ),
        )

    # --- Transport -------------------------------------------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        contract: bool = True,
        idempotent: bool = True,
    ) -> _Answer:
        """One call, tried again on a retryable answer while the policy allows, when the call is idempotent. No
        answer at all is raised at once as ``unreachable``.

        A tell is idempotent through its key, and a read changes nothing, so every call the contract has today
        is; a call that is not must pass ``idempotent=False`` and is then tried once. An ``internal`` error is
        tried again once, as the contract allows for idempotent calls.
        """
        if contract and self.handshake is None:
            raise RuntimeError("call connect() first: the handshake checks the service before any data call")
        sent = dict(self._authorization)
        if contract:
            sent[CONTRACT_HEADER] = CONTRACT_VERSION
        sent.update(headers or {})
        url = f"{self._base_url}{path}"
        retried_internal = False
        attempt = 0
        while True:
            try:
                response = await self._http.request(
                    method, url, json=json, params=params, headers=sent, timeout=self._timeout
                )
            except httpx.TransportError as error:
                # Not retried: a service that does not answer is failed at once, so the caller can carry on
                # without reuse (architecture.md, Service down) instead of waiting out every retry's timeout.
                raise HearsayError("unreachable", f"no answer from Hearsay ({type(error).__name__})") from error
            if response.is_success:
                try:
                    answer: dict[str, Any] = response.json()
                except ValueError as error:
                    raise HearsayError(
                        "unexpected_response",
                        f"HTTP {response.status_code} with a body that is not JSON",
                        status=response.status_code,
                        request_id=response.headers.get("request-id"),
                    ) from error
                return _Answer(answer, response.status_code, response.headers.get("request-id"))
            failure = _problem(response)
            again = failure.retryable or (failure.code == "internal" and not retried_internal)
            if not (idempotent and again and attempt < len(self._retry.waits)):
                raise failure
            retried_internal = retried_internal or failure.code == "internal"
            wait = self._retry.waits[attempt] * (1 + random.uniform(0, self._retry.jitter))
            wait = max(wait, _retry_after(response))
            log.warning(
                "Hearsay %s %s failed with %s (request %s); trying again in %.2f s",
                method,
                path,
                failure.code,
                failure.request_id,
                wait,
            )
            attempt += 1
            await self._sleep(wait)


def _read(
    body: dict[str, Any], limit: int | None, min_store_state: int | None, on_behind: str | None
) -> dict[str, Any]:
    for name, value in (("limit", limit), ("min_store_state", min_store_state), ("on_behind", on_behind)):
        if value is not None:
            body[name] = value
    return body
