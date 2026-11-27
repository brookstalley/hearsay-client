"""``POST /lookup`` (contract 1.3, experimental): what a look-up asks, and what it is answered (`api-contract.md`).

A look-up starts from an entity or a typed identifier, never from words, and follows curated relations. It is answered
under no policy: the answers are what was said, with who said it, and nothing here is a verdict. ``followed`` says
which edges were followed (``said``); ``cuts`` says where a limit or a deadline shaped the answer, and ``complete`` is
true only when none did. The shape is experimental and may change between minors.

As everywhere in this client, an answer is read tolerantly: a field it does not know is ignored, and a value it does
not know is kept as the string it was sent.
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from hearsay_client.types import (
    Identifier,
    Misshapen,
    NamedTerm,
    Stamp,
    _bool,
    _day_or_none,
    _int,
    _list,
    _object,
    _str,
    _time,
)


@dataclass(frozen=True)
class LookupStep:
    """One step: a curated relation by name, followed ``out`` from its subject or ``in`` from its object, with at
    most one filter on a qualifier.

    ``holds`` is what the qualifier must hold: a :class:`NamedTerm`, such as the role ``producer``, which matches the
    role and every source token mapped to it, or an :class:`Identifier`, which matches that one token exactly.
    """

    relation: str
    direction: str = "out"
    qualifier: str | None = None
    holds: NamedTerm | Identifier | None = None

    def __post_init__(self) -> None:
        if (self.qualifier is None) != (self.holds is None):
            raise ValueError("a step's filter names a qualifier and what it holds, both or neither")

    def wire(self) -> dict[str, Any]:
        body: dict[str, Any] = {"relation": self.relation, "direction": self.direction}
        if self.qualifier is not None and self.holds is not None:
            holds = self.holds.wire() if isinstance(self.holds, NamedTerm) else {"identifier": self.holds.wire()}
            body["where"] = [{"qualifier": self.qualifier, "is": holds}]
        return body


@dataclass(frozen=True)
class LookupLimits:
    """The stated limits a look-up may lower or raise; None leaves the service's default."""

    per_node: int | None = None
    max_visited: int | None = None
    paths_per_answer: int | None = None
    statements_per_edge: int | None = None

    def wire(self) -> dict[str, Any]:
        return {name: value for name, value in vars(self).items() if value is not None}


@dataclass(frozen=True)
class Lookup:
    """A look-up. ``start`` is an entity's ``en_`` id or an :class:`Identifier`.

    :param stances: which stances make an edge followed; every stance is counted on each edge either way
    :param identity_schemes: with ``shared_identifier``, the schemes sameness may be followed in; None for every
        scheme curation flags as naming one thing
    :param store_state: read at exactly this store state, so the same look-up returns the same answer
    """

    start: str | Identifier
    steps: tuple[LookupStep, ...]
    stances: tuple[str, ...] | None = None
    shared_identifier: bool = False
    identity_schemes: tuple[str, ...] | None = None
    exclude: tuple[str, ...] = ()
    limits: LookupLimits = field(default_factory=LookupLimits)
    detail: str | None = None
    store_state: int | None = None

    def wire(self) -> dict[str, Any]:
        start = {"entity": self.start} if isinstance(self.start, str) else {"identifier": self.start.wire()}
        body: dict[str, Any] = {"start": start, "steps": [step.wire() for step in self.steps]}
        if self.stances is not None:
            body["stances"] = list(self.stances)
        if self.shared_identifier:
            body["identity"] = {"rule": "shared_identifier"}
            if self.identity_schemes is not None:
                body["identity"]["schemes"] = list(self.identity_schemes)
        elif self.identity_schemes is not None:
            raise ValueError("identity_schemes narrows shared_identifier, which this look-up does not follow")
        if self.exclude:
            body["exclude"] = list(self.exclude)
        if self.limits.wire():
            body["limits"] = self.limits.wire()
        for name in ("detail", "store_state"):
            if getattr(self, name) is not None:
                body[name] = getattr(self, name)
        return body


def _identifier(body: Any) -> Identifier:
    return Identifier(_str(body, "scheme"), _str(body, "value"))


def _identifier_or_none(body: Any, name: str) -> Identifier | None:
    if not isinstance(body, dict) or name not in body:
        return None
    return _identifier(_object(body, name))


@dataclass(frozen=True)
class SaidStatement:
    """A statement behind an edge, as ``evidence`` shows it: who made it and read it, the excerpt it stands on, and
    where that was read from. ``source_url`` and ``retrieved_on`` are the kept fetch's; the world sees its day."""

    statement: str
    stance: str
    audience: str
    claimant: str
    claimant_document: str | None
    reader: str
    reader_kind: str
    excerpt_start: int | None
    excerpt_end: int | None
    excerpt_text: str | None
    source_event: str | None
    source_document: str | None
    source_url: str | None
    retrieved_on: date | None
    valid_from: datetime | None
    valid_to: datetime | None
    said_at: datetime | None

    @classmethod
    def read(cls, body: Any) -> "SaidStatement":
        claimant = _object(body, "claimant")
        reader = _object(body, "reader")
        excerpt = _object(body, "excerpt", nullable=True)
        source = _object(body, "source", nullable=True)
        valid = _object(body, "valid", nullable=True)
        return cls(
            statement=_str(body, "statement"),
            stance=_str(body, "stance"),
            audience=_str(body, "audience"),
            claimant=_str(claimant, "entity"),
            claimant_document=_str(claimant, "document", optional=True),
            reader=_str(reader, "id"),
            reader_kind=_str(reader, "kind"),
            excerpt_start=None if excerpt is None else _int(excerpt, "start"),
            excerpt_end=None if excerpt is None else _int(excerpt, "end"),
            excerpt_text=None if excerpt is None else _str(excerpt, "text"),
            source_event=None if source is None else _str(source, "event"),
            source_document=None if source is None else _str(source, "document", nullable=True),
            source_url=None if source is None else _str(source, "url", nullable=True),
            retrieved_on=None if source is None else _day_or_none(_str(source, "retrieved_on", nullable=True)),
            valid_from=None if valid is None else _time(_str(valid, "from", nullable=True)),
            valid_to=None if valid is None else _time(_str(valid, "to", nullable=True)),
            said_at=_time(_str(body, "said_at", nullable=True)),
        )


@dataclass(frozen=True)
class SaidOf:
    """How an edge is said: how many statements make it (``more`` when that is a floor), each stance's count, how
    many claimants, and at ``evidence`` the earliest statements."""

    count: int
    more: bool
    stances: dict[str, int]
    claimants: int
    statements: tuple[SaidStatement, ...] | None

    @classmethod
    def read(cls, body: Any) -> "SaidOf":
        stances = _object(body, "stances")
        for stance, count in stances.items():
            if not isinstance(count, int) or isinstance(count, bool):
                raise Misshapen(f"the count of {stance} is {type(count).__name__}, not int")
        shown = None
        if isinstance(body, dict) and "statements" in body:
            shown = tuple(SaidStatement.read(statement) for statement in _list(body, "statements"))
        return cls(_int(body, "count"), _bool(body, "more"), dict(stances), _int(body, "claimants"), shown)


@dataclass(frozen=True)
class HopQualifier:
    """A qualifier of a hop's proposition, as stated. For a source's token: its entity, its identifier, and the
    curated term it maps to at the store state. ``value`` is the whole value as sent, for the forms not named here."""

    relation: str | None
    entity: str | None
    identifier: Identifier | None
    maps_to: NamedTerm | None
    value: dict[str, Any]

    @classmethod
    def read(cls, body: Any) -> "HopQualifier":
        value = _object(body, "value")
        mapped = _object(value, "maps_to") if "maps_to" in value else None
        return cls(
            relation=_str(body, "relation", nullable=True),
            entity=_str(value, "entity", optional=True),
            identifier=_identifier_or_none(value, "identifier"),
            maps_to=None if mapped is None else NamedTerm(_str(mapped, "sort"), _str(mapped, "name")),
            value=dict(value),
        )


@dataclass(frozen=True)
class SameClaim:
    """One of the two claims a crossing rests on: the entity, and how its claim to the identifier is said."""

    entity: str
    proposition: str
    said: SaidOf

    @classmethod
    def read(cls, body: Any) -> "SameClaim":
        return cls(_str(body, "entity"), _str(body, "proposition"), SaidOf.read(_object(body, "said")))


@dataclass(frozen=True)
class Hop:
    """One hop of a path. ``kind`` is ``relation`` for an edge someone said, with its relation, direction,
    proposition, qualifiers and statements; or ``same`` for a crossing between two entities that claims give one
    identifier, with that identifier and both claims. Any other kind is kept as sent."""

    kind: str
    step: int
    source: str
    target: str
    relation: str | None = None
    direction: str | None = None
    proposition: str | None = None
    qualifiers: tuple[HopQualifier, ...] = ()
    said: SaidOf | None = None
    identifier: Identifier | None = None
    claims: tuple[SameClaim, ...] = ()

    @classmethod
    def read(cls, body: Any) -> "Hop":
        kind = _str(body, "kind", optional=True) or "relation"
        step, source, target = _int(body, "step"), _str(body, "from"), _str(body, "to")
        if kind == "relation":
            return cls(
                kind,
                step,
                source,
                target,
                relation=_str(body, "relation"),
                direction=_str(body, "direction"),
                proposition=_str(body, "proposition"),
                qualifiers=tuple(HopQualifier.read(qualifier) for qualifier in _list(body, "qualifiers")),
                said=SaidOf.read(_object(body, "said")),
            )
        if kind == "same":
            return cls(
                kind,
                step,
                source,
                target,
                identifier=_identifier(_object(body, "identifier")),
                claims=tuple(SameClaim.read(claim) for claim in _list(body, "claims")),
            )
        return cls(kind, step, source, target)


@dataclass(frozen=True)
class LookupAnswer:
    """An entity the look-up reached. ``label`` is a quote: the words the statement ``label_from`` used for it.
    ``paths`` is None at ``plain``; ``paths_count`` is a floor when ``paths_more``."""

    entity: str
    label: str | None
    label_from: str | None
    identifiers: tuple[Identifier, ...]
    paths_count: int
    paths_more: bool
    paths: tuple[tuple[Hop, ...], ...] | None

    @classmethod
    def read(cls, body: Any) -> "LookupAnswer":
        end = _object(body, "end")
        paths = None
        if isinstance(body, dict) and "paths" in body:
            paths = []
            for path in _list(body, "paths"):
                if not isinstance(path, list):
                    raise Misshapen(f"a path is {type(path).__name__}, not list")
                paths.append(tuple(Hop.read(hop) for hop in path))
        return cls(
            entity=_str(end, "entity"),
            label=_str(end, "label", nullable=True),
            label_from=_str(end, "label_from", nullable=True),
            identifiers=tuple(_identifier(held) for held in _list(end, "identifiers")),
            paths_count=_int(body, "paths_count"),
            paths_more=_bool(body, "paths_more"),
            paths=None if paths is None else tuple(paths),
        )


@dataclass(frozen=True)
class LookupCut:
    """A limit or a work stop that shaped the answer: ``node_limit`` (``followed`` edges of ``at`` were followed and
    at least one more was not), ``visited_limit`` (``left`` entities were not expanded), ``identity_limit`` or
    ``deadline``. Only a cut that is not ``reproducible`` may differ on the next read of the same store state."""

    kind: str
    step: int
    at: str | None
    limit: int | None
    followed: int | None
    left: int | None
    reproducible: bool

    @classmethod
    def read(cls, body: Any) -> "LookupCut":
        return cls(
            kind=_str(body, "kind"),
            step=_int(body, "step"),
            at=_str(body, "at", optional=True),
            limit=_int(body, "limit", optional=True),
            followed=_int(body, "followed", optional=True),
            left=_int(body, "left", optional=True),
            reproducible=_bool(body, "reproducible"),
        )


@dataclass(frozen=True)
class LookupGap:
    """Where a step found nothing said from an entity: ``never_checked``. ``at`` is None when the look-up started
    from an identifier no claim holds."""

    step: int
    at: str | None
    kind: str

    @classmethod
    def read(cls, body: Any) -> "LookupGap":
        return cls(_int(body, "step"), _str(body, "at", nullable=True), _str(body, "kind"))


@dataclass(frozen=True)
class StartResolved:
    """An entity the look-up started from. From an identifier: the identifier, and the claims that give it."""

    entity: str
    by: Identifier | None
    claims: tuple[str, ...]

    @classmethod
    def read(cls, body: Any) -> "StartResolved":
        claims = _list(body, "claims", optional=True)
        for claim in claims:
            if not isinstance(claim, str):
                raise Misshapen(f"a claim is {type(claim).__name__}, not str")
        return cls(_str(body, "entity"), _identifier_or_none(body, "by"), tuple(claims))


@dataclass(frozen=True)
class LookedUp:
    """A look-up's answer: the answers in entity order, one page of them.

    :ivar followed: which edges were followed: ``said``, under no policy
    :ivar complete: true only when no cut shaped the answer; a further page is not a cut
    :ivar gaps: where a step found nothing, at ``standing`` and ``evidence``; None at ``plain``, which says
        ``not_known`` instead
    :ivar next_cursor: pass it to read the next page at the same store state; None when there is none
    """

    followed: str
    complete: bool
    start: tuple[StartResolved, ...]
    answers: tuple[LookupAnswer, ...]
    cuts: tuple[LookupCut, ...]
    cuts_more: bool
    gaps: tuple[LookupGap, ...] | None
    gaps_more: bool
    not_known: bool
    next_cursor: str | None
    stamp: Stamp

    @classmethod
    def read(cls, body: Any) -> "LookedUp":
        gaps = None
        if isinstance(body, dict) and "gaps" in body:
            gaps = tuple(LookupGap.read(gap) for gap in _list(body, "gaps"))
        return cls(
            followed=_str(body, "followed"),
            complete=_bool(body, "complete"),
            start=tuple(StartResolved.read(found) for found in _list(_object(body, "start"), "resolved")),
            answers=tuple(LookupAnswer.read(answer) for answer in _list(body, "answers")),
            cuts=tuple(LookupCut.read(cut) for cut in _list(body, "cuts")),
            cuts_more=_bool(body, "cuts_more", optional=True, default=False),
            gaps=gaps,
            gaps_more=_bool(body, "gaps_more", optional=True, default=False),
            not_known=_bool(body, "not_known", optional=True, default=False),
            next_cursor=_str(body, "next_cursor", nullable=True),
            stamp=Stamp.read(_object(body, "stamp")),
        )
