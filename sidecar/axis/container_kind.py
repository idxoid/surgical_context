"""Container kind classifier (L2).

Reads axis bits + payload + optional graph-context probes for one symbol and
emits the set of container kinds whose fingerprint that symbol matches. Owns
the per-kind predicate. Benchmark role/contract analysis lives outside the
runtime package (see ``QA.axis_analysis``) so this module cannot become a
role mapping table by accident.

Terminology (see ``docs/axis_terminology.md``):

  fact      = physical AST/graph observation
  axis bit  = normalized fact on CFG/DFG/STRUCT
  contract  = provable combination of axis bits
  role      = user/benchmark requirement
  bucket    = optimisation grouping (not used here)

Layer boundary rules — copy these into any new predicate before adding it:

  - The classifier reads ``AxisProfile`` (axis bits + payload) and a
    ``GraphContextProbe`` (optional; small, well-typed). Nothing else.
  - The classifier returns ``ContainerKindMatch`` records. It NEVER decides
    a role, a contract, or a retrieval action.
  - A predicate body MAY check axis bits and payload contents structurally.
    A predicate body MAY call the graph-context probe with structural
    questions ("how many outgoing kind-classified edges does this symbol
    have", "is this symbol re-exported"). A predicate body MAY NOT match
    symbol names, file stems, or framework keywords.
  - Library markers (``starlette.routing.Router`` ⇒ ``web_route_register``)
    arrive via the graph-context probe carrying *its* own kind, not via a
    name list inside this module.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Protocol

from sidecar.axis.schema import AxisFact, AxisProfile

# ---------------------------------------------------------------------------
# Graph context probe — the (optional) bridge from the axis layer to the
# graph topology layer. The classifier holds a *protocol*, not a concrete
# Neo4j client. The implementation can be backed by Neo4j, by an in-memory
# graph stub, or by a no-op for axis-only smoke tests.
# ---------------------------------------------------------------------------


class GraphContextProbe(Protocol):
    """Structural questions a kind predicate may ask about a symbol."""

    def outgoing_kind_edges(
        self,
        symbol_uid: str,
        kinds: Iterable[str],
    ) -> int:
        """Count outgoing edges from ``symbol_uid`` to nodes whose container
        kind is in ``kinds``. Used to test 'low edge density to anchors',
        registry routing, etc. ``0`` when no graph context is available."""

    def library_marker_kinds(self, symbol_uid: str) -> set[str]:
        """Return container kinds inherited from external library markers
        (e.g. ``{web_route_register}`` for a class inheriting
        ``starlette.routing.Router``). Empty set when no marker matches."""

    def caller_package_dispersion(self, symbol_uid: str) -> float:
        """Heuristic in [0, 1]: how spread across packages this symbol's
        callers are. ``1.0`` = every package has at least one caller;
        ``0.0`` = a single package owns all callers."""

    def is_cfg_driver(self, symbol_uid: str) -> bool:
        """True when the symbol is the top of a dispatch loop, the body of a
        registered handler invocation, or otherwise drives control flow.
        Used by ``dispersed_runtime_position`` to rule out CFG drivers."""

    def outgoing_handles_count(self, symbol_uid: str) -> int:
        """Count outgoing ``HANDLES`` edges from ``symbol_uid``. A registry
        Symbol (e.g. ``app = Flask(...)``) with at least one HANDLES edge
        has actually been used to register a handler via a decorator — the
        cross-axis proof that the marker-only kind (web_route_register,
        task_register, error_dispatch) is real, not just instantiated."""

    def outgoing_injects_count(self, symbol_uid: str) -> int:
        """Count outgoing ``INJECTS`` edges from ``symbol_uid``. A function
        with at least one INJECTS edge has had at least one of its parameter
        defaults resolved to a provider symbol — the cross-symbol DFG proof
        that the ``Depends(provider)`` / ``Inject(provider)`` pattern is
        actually wired, not just a local ``Call``-shaped default."""

    def peer_container_kinds_for(self, qualified_name_prefix: str) -> set[str]:
        """Union of container kinds across peer profiles whose qualified_name
        starts with ``qualified_name_prefix``. The pipeline uses a per-file
        wrapping probe so a class predicate can ask
        ``probe.peer_container_kinds_for(class_qn + '.')`` and see what kinds
        its same-file methods carry — the structural floor under
        ``registry_class``. The default probe knows no peers (empty set)."""

    def is_event_signal(self, symbol_uid: str) -> bool:
        """True when ``symbol_uid`` is the target of an EVENT_SUB / EVENT_PUB
        edge — a pub/sub signal channel proven by structural co-occurrence
        (subscribed via ``@receiver``, or both connected AND sent-from). This is
        the graph-context proof that lets ``signal_register`` drop its
        library-marker dependency."""

    def is_error_model_type_name(self, key_name: str, symbol_uid: str) -> bool:
        """True when ``key_name`` names an exception type — a builtin from the
        standard hierarchy or an in-workspace class whose
        ``inherits_builtin_exception`` marker was set at link time."""

    def inherits_error_dispatch(self, symbol_uid: str) -> bool:
        """True when ``symbol_uid`` structurally inherits ``error_dispatch``
        from a ``DEPENDS_ON`` ancestor that already carries the kind."""

    def has_proxy_object_topology(self, symbol_uid: str) -> bool:
        """True when graph topology proves lazy proxy resolution on ``symbol_uid``.

        Covers ``proxy_binding`` symbols and any symbol with outgoing
        ``PROXY_OF`` / ``RESOLVES_ATTR`` edges."""

    def inherits_proxy_object(self, symbol_uid: str) -> bool:
        """True when ``symbol_uid`` inherits ``proxy_object`` from a
        ``DEPENDS_ON`` ancestor that already carries the kind."""


class NullGraphProbe:
    """Default probe: no graph context available, every probe returns 'no'."""

    def outgoing_kind_edges(self, symbol_uid: str, kinds: Iterable[str]) -> int:
        return 0

    def library_marker_kinds(self, symbol_uid: str) -> set[str]:
        return set()

    def caller_package_dispersion(self, symbol_uid: str) -> float:
        return 0.0

    def is_cfg_driver(self, symbol_uid: str) -> bool:
        return False

    def outgoing_handles_count(self, symbol_uid: str) -> int:
        return 0

    def outgoing_injects_count(self, symbol_uid: str) -> int:
        return 0

    def is_event_signal(self, symbol_uid: str) -> bool:
        return False

    def peer_container_kinds_for(self, qualified_name_prefix: str) -> set[str]:
        return set()

    def is_error_model_type_name(self, key_name: str, symbol_uid: str) -> bool:
        return False

    def inherits_error_dispatch(self, symbol_uid: str) -> bool:
        return False

    def has_proxy_object_topology(self, symbol_uid: str) -> bool:
        return False

    def inherits_proxy_object(self, symbol_uid: str) -> bool:
        return False


def _probe_has_proxy_object_topology(probe: GraphContextProbe, symbol_uid: str) -> bool:
    fn = getattr(probe, "has_proxy_object_topology", None)
    if not callable(fn):
        return False
    return bool(fn(symbol_uid))


def _probe_inherits_proxy_object(probe: GraphContextProbe, symbol_uid: str) -> bool:
    fn = getattr(probe, "inherits_proxy_object", None)
    if not callable(fn):
        return False
    return bool(fn(symbol_uid))


def _probe_is_error_model_type_name(
    probe: GraphContextProbe,
    key_name: str,
    symbol_uid: str,
) -> bool:
    fn = getattr(probe, "is_error_model_type_name", None)
    if not callable(fn):
        return False
    return bool(fn(key_name, symbol_uid))


def _probe_inherits_error_dispatch(probe: GraphContextProbe, symbol_uid: str) -> bool:
    fn = getattr(probe, "inherits_error_dispatch", None)
    if not callable(fn):
        return False
    return bool(fn(symbol_uid))


# ---------------------------------------------------------------------------
# Classifier output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainerKindMatch:
    """One kind that matched on a symbol. ``evidence`` lists the axis bits
    and probe answers that supported the match; the role/contract layer
    surfaces this when explaining 'why this kind'."""

    kind: str
    symbol_uid: str
    qualified_name: str
    evidence_bits: tuple[tuple[str, str], ...]  # list of (axis, bit)
    evidence_probes: tuple[str, ...]  # human-readable probe outcomes
    payload: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "symbol_uid": self.symbol_uid,
            "qualified_name": self.qualified_name,
            "evidence_bits": [list(pair) for pair in self.evidence_bits],
            "evidence_probes": list(self.evidence_probes),
            "payload": dict(self.payload),
        }


# ---------------------------------------------------------------------------
# Per-kind predicate registry. A predicate returns a ``ContainerKindMatch``
# or ``None``. Order does not matter — a symbol may match multiple kinds;
# the classifier returns all matches.
# ---------------------------------------------------------------------------


KindPredicate = Callable[[AxisProfile, GraphContextProbe], ContainerKindMatch | None]


_PREDICATES: dict[str, KindPredicate] = {}


def register_kind(kind: str) -> Callable[[KindPredicate], KindPredicate]:
    """Decorator used inside this module to register a kind predicate."""

    def deco(fn: KindPredicate) -> KindPredicate:
        if kind in _PREDICATES:
            raise ValueError(f"Container kind already registered: {kind}")
        _PREDICATES[kind] = fn
        return fn

    return deco


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _facts_for_bit(profile: AxisProfile, axis: str, bit: str) -> list[AxisFact]:
    return [f for f in profile.facts if f.axis == axis and f.bit == bit]


def _struct(profile: AxisProfile, bit: str) -> list[AxisFact]:
    return _facts_for_bit(profile, "struct", bit)


def _dfg(profile: AxisProfile, bit: str) -> list[AxisFact]:
    return _facts_for_bit(profile, "dfg", bit)


def _cfg(profile: AxisProfile, bit: str) -> list[AxisFact]:
    return _facts_for_bit(profile, "cfg", bit)


# ---------------------------------------------------------------------------
# Kind predicates — start with kinds provable from axis bits alone (no graph
# context required). Kinds that depend on graph topology (e.g. inheritance
# from a library marker) ALSO get a predicate but it short-circuits unless
# the probe answers.
# ---------------------------------------------------------------------------


@register_kind("registry_class")
def _classify_registry_class(
    profile: AxisProfile,
    probe: GraphContextProbe,
) -> ContainerKindMatch | None:
    """Structural floor under marker-only registry kinds.

    Two channels lead here, both axis-only and independent of the catalogue:

      1. **Class channel** — a class definition whose same-file peer methods
         already prove ``metadata_key_roundtrip`` (keyed registry write +
         read of the same key) or ``callable_container_dispatch`` (callable
         write + iteration + invocation on a shared container). The
         workspace-level inheritance phase
         (:mod:`sidecar.indexer.fast.registry_class_inheritance`)
         propagates this verdict down ``DEPENDS_ON`` ancestry and through
         per-file import alias resolution.

      2. **Consumer-derived Variable channel** — a module-level Variable
         Symbol that holds an instance ( ``app = Something()``) and has at
         least one outgoing ``HANDLES`` edge (a decorator like
         ``@app.route``). The decorator-binding pattern IS the structural
         proof: whatever ``Something`` is, it is being used here as a
         registry receiving callable bindings. Catalogue still names the
         subtype (web / task / signal) on top; this channel guarantees the
         *generic* registry classification even for libraries no catalogue
         entry lists.
    """
    if profile.symbol_kind == "class":
        prefix = f"{profile.qualified_name}."
        peer_kinds = probe.peer_container_kinds_for(prefix)
        registry_method_kinds = {"metadata_carrier", "middleware_chain"}
        matched = peer_kinds & registry_method_kinds
        if not matched:
            return None
        return ContainerKindMatch(
            kind="registry_class",
            symbol_uid=profile.symbol_uid,
            qualified_name=profile.qualified_name,
            evidence_bits=(("struct", "class_def"),),
            evidence_probes=(f"peer_method_kinds:{','.join(sorted(matched))}",),
            payload={"registry_method_kinds": sorted(matched)},
        )

    if profile.symbol_kind == "variable":
        # ``dfg.registered_callable`` is emitted on the stub profile by the
        # pipeline when the Variable has ≥1 outgoing HANDLES edge. Its
        # presence is the consumer-derived proof that this variable acts
        # as a registry — no catalogue lookup needed.
        registered_facts = [
            f for f in profile.facts if f.axis == "dfg" and f.bit == "registered_callable"
        ]
        if not registered_facts:
            return None
        registered_count = sum(
            int(v) if isinstance(v, (int, float)) else 0
            for f in registered_facts
            for v in [(f.payload or {}).get("count")]
        )
        return ContainerKindMatch(
            kind="registry_class",
            symbol_uid=profile.symbol_uid,
            qualified_name=profile.qualified_name,
            evidence_bits=(("dfg", "registered_callable"),),
            evidence_probes=(f"consumer_derived:registered_handler_count={registered_count}",),
            payload={"registered_callable_count": registered_count},
        )

    return None


@register_kind("data_model")
def _classify_data_model(
    profile: AxisProfile,
    probe: GraphContextProbe,
) -> ContainerKindMatch | None:
    """A class whose body declares multiple typed/descriptor-shaped attributes.

    Pure axis-level signal — no probe required. It intentionally uses only
    facts currently emitted on the class profile itself: ``class_def``,
    ``class_attribute``, ``annotation``, and ``generic_shape``. Method-level
    constructed outputs may strengthen this kind later through graph context,
    but they are not required here because the extractor stores them on method
    profiles, not on the class profile.
    """
    if profile.symbol_kind != "class":
        return None
    if not _struct(profile, "class_def"):
        return None
    class_attrs = _struct(profile, "class_attribute")
    annotations = _struct(profile, "annotation")
    generic_shapes = _struct(profile, "generic_shape")
    if len(class_attrs) + len(annotations) < 2:
        return None
    if not (annotations or generic_shapes):
        return None
    evidence_bits = (
        ("struct", "class_def"),
        ("struct", "class_attribute") if class_attrs else ("struct", "annotation"),
        ("struct", "generic_shape") if generic_shapes else ("struct", "annotation"),
    )
    payload: dict[str, object] = {
        "class_attribute_count": len(class_attrs),
        "annotation_count": len(annotations),
        "generic_shape_count": len(generic_shapes),
    }
    return ContainerKindMatch(
        kind="data_model",
        symbol_uid=profile.symbol_uid,
        qualified_name=profile.qualified_name,
        evidence_bits=evidence_bits,
        evidence_probes=(),
        payload=payload,
    )


@register_kind("metadata_carrier")
def _classify_metadata_carrier(
    profile: AxisProfile,
    probe: GraphContextProbe,
) -> ContainerKindMatch | None:
    """Object with key/value writes that share literal-key identity with later
    reads on the same scope. Pure axis-level signal.

    Both writes and reads must carry payload literals (``literal_key`` or a
    payload field with a constant key); the predicate ONLY checks that the
    shape exists, not what the keys *mean*.
    """
    writes = _dfg(profile, "keyed_write")
    reads = _dfg(profile, "keyed_read")
    if not writes or not reads:
        return None
    write_keys = {str(f.payload.get("key", "")) for f in writes if f.payload.get("key")}
    read_keys = {str(f.payload.get("key", "")) for f in reads if f.payload.get("key")}
    shared = write_keys & read_keys
    if not shared:
        return None
    return ContainerKindMatch(
        kind="metadata_carrier",
        symbol_uid=profile.symbol_uid,
        qualified_name=profile.qualified_name,
        evidence_bits=(
            ("dfg", "keyed_write"),
            ("dfg", "keyed_read"),
            ("struct", "literal_key"),
        ),
        evidence_probes=(),
        payload={"shared_keys": sorted(shared)[:5], "shared_key_count": len(shared)},
    )


@register_kind("middleware_chain")
def _classify_middleware_chain(
    profile: AxisProfile,
    probe: GraphContextProbe,
) -> ContainerKindMatch | None:
    """Container that is appended into AND later iterated AND whose iteration
    invokes the stored callable values.

    Three concurrent axis-level conditions; no probe required. Filters out
    accidental list-appends that are never iterated.
    """
    callable_values = _dfg(profile, "callable_value")
    appends = _dfg(profile, "container_write_value")
    iterations = _dfg(profile, "iteration_source")
    if not (callable_values and appends and iterations):
        return None
    # Require at least one value_call to confirm the iterated value is invoked.
    if not _cfg(profile, "value_call"):
        return None
    return ContainerKindMatch(
        kind="middleware_chain",
        symbol_uid=profile.symbol_uid,
        qualified_name=profile.qualified_name,
        evidence_bits=(
            ("dfg", "callable_value"),
            ("dfg", "container_write_value"),
            ("dfg", "iteration_source"),
            ("cfg", "value_call"),
        ),
        evidence_probes=(),
        payload={
            "append_sites": len(appends),
            "iteration_sites": len(iterations),
        },
    )


@register_kind("keyed_register_callable")
def _classify_keyed_register_callable(
    profile: AxisProfile,
    probe: GraphContextProbe,
) -> ContainerKindMatch | None:
    """Method that registers a callable into a keyed container.

    Mirror of ``keyed_dispatch_callable``: the dispatcher *reads* one
    callable out of a registry by key and invokes it; this kind
    *writes* one callable into a registry by key and exits. Celery's
    ``TaskRegistry.register`` (``self[task.name] = task``), FastAPI's
    ``APIRouter.add_api_route`` (``self.routes.append(APIRoute(...))``
    after keyed lookup), and Flask's ``Flask.add_url_rule`` all match
    this shape.

    Required structural fingerprint:

      - dfg.subscript_write + dfg.keyed_write + dfg.container_write_value
        (write one entry into a keyed container)
      - dfg.callable_value (the value being stored is a callable; pure
        data registration would not show this bit)

    Discriminator vs ``middleware_chain``: ``middleware_chain`` stores
    callables *and* iterates them; pure registration never iterates the
    container in the same body. So ``iteration_source`` must be
    absent.
    """
    if not _dfg(profile, "subscript_write"):
        return None
    if not _dfg(profile, "keyed_write"):
        return None
    if not _dfg(profile, "container_write_value"):
        return None
    if not _dfg(profile, "callable_value"):
        return None
    # Discriminator: middleware_chain iterates; registration does not.
    if _dfg(profile, "iteration_source"):
        return None
    # Discriminator: a real registration is a *setter* — it stores its
    # parameter and returns (typically ``None``). Methods that also
    # produce a return value with a structured shape
    # (``FastAPI.__call__`` writing to ``scope`` then awaiting
    # ``super().__call__``; ``APIRoute.matches`` writing ``self`` into
    # ``child_scope`` then returning ``(match, child_scope)``) are
    # dispatch / inspection methods that happen to mutate a container,
    # not registration calls. The ``return_output`` bit fires whenever
    # a method propagates a value out; combined with the storage
    # signature it is a precise structural separator. Real
    # registration methods set state and exit.
    if _dfg(profile, "return_output"):
        return None
    return ContainerKindMatch(
        kind="keyed_register_callable",
        symbol_uid=profile.symbol_uid,
        qualified_name=profile.qualified_name,
        evidence_bits=(
            ("dfg", "subscript_write"),
            ("dfg", "keyed_write"),
            ("dfg", "container_write_value"),
            ("dfg", "callable_value"),
        ),
        evidence_probes=(),
        payload={},
    )


@register_kind("keyed_dispatch_callable")
def _classify_keyed_dispatch_callable(
    profile: AxisProfile,
    probe: GraphContextProbe,
) -> ContainerKindMatch | None:
    """Method that resolves a callable through a keyed lookup on a
    container attribute and then invokes it.

    Concrete shape::

        def dispatch(self, ..., key):
            ...
            handler = self.registry[key]   # subscript_read + keyed_read + container_read_key
            return handler(...)            # callable_value + value_call

    This is Flask's ``Flask.dispatch_request`` /
    ``Flask.full_dispatch_request`` / ``Flask.wsgi_app`` pattern,
    Django's URL resolver dispatch, FastAPI's APIRoute closure that
    grabs a handler out of a route table, and any registry-keyed
    dispatcher.

    Distinct from ``middleware_chain`` (which *iterates* a list-shaped
    container and invokes each entry) and ``signal_register`` (which
    fans out to receivers). The discriminator is the **absence of
    iteration**: keyed dispatch picks one callable by key, middleware
    walks them all.
    """
    if not _dfg(profile, "subscript_read"):
        return None
    if not _dfg(profile, "keyed_read"):
        return None
    if not _dfg(profile, "container_read_key"):
        return None
    if not _dfg(profile, "callable_value"):
        return None
    if not _cfg(profile, "value_call"):
        return None
    # Discriminator: middleware chains iterate the container. A
    # registry-keyed dispatcher reads ONE entry by key, never the whole
    # list, so iteration_source must NOT be present in the same body.
    if _dfg(profile, "iteration_source"):
        return None
    return ContainerKindMatch(
        kind="keyed_dispatch_callable",
        symbol_uid=profile.symbol_uid,
        qualified_name=profile.qualified_name,
        evidence_bits=(
            ("dfg", "subscript_read"),
            ("dfg", "keyed_read"),
            ("dfg", "container_read_key"),
            ("dfg", "callable_value"),
            ("cfg", "value_call"),
        ),
        evidence_probes=(),
        payload={},
    )


@register_kind("signal_register")
def _classify_signal_register(
    profile: AxisProfile,
    probe: GraphContextProbe,
) -> ContainerKindMatch | None:
    """Bidirectional callable storage: receivers attached and later invoked.

    Axis-only shape overlaps exactly with generic middleware/callback chains, so
    this kind needs graph context to prove a signal-specific topology — formerly
    a library-marker dependency. The structural proof now is the EVENT_SUB /
    EVENT_PUB pub/sub edges, which ``link_hooks`` keeps only for a
    co-occurrence-validated channel (subscribed via ``@receiver``, or both
    connected AND sent-from). A symbol that is the target of an EVENT edge IS the
    signal channel (the transmit node); without that proof, no match.
    """
    if not probe.is_event_signal(profile.symbol_uid):
        return None
    return ContainerKindMatch(
        kind="signal_register",
        symbol_uid=profile.symbol_uid,
        qualified_name=profile.qualified_name,
        evidence_bits=(
            ("dfg", "container_write_value"),
            ("dfg", "iteration_source"),
            ("cfg", "value_call"),
        ),
        evidence_probes=("graph_context:event_pubsub",),
        payload={"via": "event_topology"},
    )


@register_kind("config_carrier")
def _classify_config_carrier(
    profile: AxisProfile,
    probe: GraphContextProbe,
) -> ContainerKindMatch | None:
    """A class body with annotated default-value class attributes; the values
    are observed to influence branches elsewhere.

    The 'observed to influence branches' part is structural negative-space —
    we look here only at the local pattern (class with annotated literal
    defaults) and let the contract compiler check the cross-symbol branch
    influence at L3.
    """
    if profile.symbol_kind != "class":
        return None
    annotated_attrs = [
        f for f in _struct(profile, "class_attribute") if f.payload.get("annotation")
    ]
    if len(annotated_attrs) < 2:
        return None
    defaulted_attrs = [f for f in annotated_attrs if str(f.payload.get("value") or "").strip()]
    if not defaulted_attrs:
        return None
    return ContainerKindMatch(
        kind="config_carrier",
        symbol_uid=profile.symbol_uid,
        qualified_name=profile.qualified_name,
        evidence_bits=(
            ("struct", "class_def"),
            ("struct", "class_attribute"),
            ("struct", "annotation"),
        ),
        evidence_probes=(),
        payload={"annotated_default_count": len(defaulted_attrs)},
    )


@register_kind("proxy_object")
def _classify_proxy_object(
    profile: AxisProfile,
    probe: GraphContextProbe,
) -> ContainerKindMatch | None:
    """Object whose attribute reads/writes resolve to a scoped target.

    Proof is graph topology only — ``proxy_binding`` markers,
    ``PROXY_OF`` / ``RESOLVES_ATTR`` edges, or inheritance from a carrier
    already tagged by the workspace propagation pass. No werkzeug name
    catalogue.
    """
    if _probe_has_proxy_object_topology(probe, profile.symbol_uid):
        return ContainerKindMatch(
            kind="proxy_object",
            symbol_uid=profile.symbol_uid,
            qualified_name=profile.qualified_name,
            evidence_bits=(),
            evidence_probes=("graph_context:proxy_topology",),
            payload={"via": "proxy_topology"},
        )

    if _probe_inherits_proxy_object(probe, profile.symbol_uid):
        return ContainerKindMatch(
            kind="proxy_object",
            symbol_uid=profile.symbol_uid,
            qualified_name=profile.qualified_name,
            evidence_bits=(("struct", "class_def"),) if profile.symbol_kind == "class" else (),
            evidence_probes=("graph_context:inherited_proxy_object",),
            payload={"via": "inheritance"},
        )

    return None


@register_kind("error_dispatch")
def _classify_error_dispatch(
    profile: AxisProfile,
    probe: GraphContextProbe,
) -> ContainerKindMatch | None:
    """Container mapping exception types to handler callables.

    The discriminator is whether ``keyed_write`` keys resolve to *exception
    types* — builtin roots from the standard hierarchy or in-workspace classes
    marked ``inherits_builtin_exception`` at link time. That is graph context,
    not a library-name catalogue entry.

    A second channel is structural inheritance: a class whose ``DEPENDS_ON``
    ancestry reaches an ``error_dispatch`` carrier (workspace propagation
    pass tags these after embed).
    """
    exception_keys: list[str] = []
    for fact in _dfg(profile, "keyed_write"):
        if str(fact.payload.get("key_kind") or "") != "Name":
            continue
        key = str(fact.payload.get("key") or "").strip()
        if not key:
            continue
        if _probe_is_error_model_type_name(probe, key, profile.symbol_uid):
            exception_keys.append(key)

    if exception_keys:
        matched = sorted(set(exception_keys))
        return ContainerKindMatch(
            kind="error_dispatch",
            symbol_uid=profile.symbol_uid,
            qualified_name=profile.qualified_name,
            evidence_bits=(("dfg", "keyed_write"),),
            evidence_probes=(f"exception_keyed_registry:{','.join(matched[:5])}",),
            payload={"exception_keys": matched[:8]},
        )

    if _probe_inherits_error_dispatch(probe, profile.symbol_uid):
        return ContainerKindMatch(
            kind="error_dispatch",
            symbol_uid=profile.symbol_uid,
            qualified_name=profile.qualified_name,
            evidence_bits=(("struct", "class_def"),) if profile.symbol_kind == "class" else (),
            evidence_probes=("graph_context:inherited_error_dispatch",),
            payload={"via": "inheritance"},
        )

    return None


@register_kind("di_container")
def _classify_di_container(
    profile: AxisProfile,
    probe: GraphContextProbe,
) -> ContainerKindMatch | None:
    """A function whose parameter defaults are CALL expressions producing
    callables — the FastAPI ``Depends(provider)`` / NestJS ``@Inject(provider)``
    pattern. A pure axis-level signal.

    The kind is on the FUNCTION symbol (the consumer of providers), not on a
    surrounding class — this mirrors how a contract compiler would later prove
    dependency_binding by following parameter_default_value → call site →
    provider callable.

    Discriminators that keep this narrow:

    - ``parameter_default`` must exist with ``default_kind == 'Call'`` —
      excludes plain literal defaults, type annotations, and name defaults.
    - ``callable_value`` must be present on the same scope — the default
      expression's argument (or the call itself) must produce a callable.
    - Pytest fixture style (``def test(db):``) has NO parameter default and
      so does not match here; it is a name-resolution pattern that lives
      outside axis bits.
    - Click ``@pass_context`` is a decorator-injection pattern, not a
      default-injection pattern — also excluded by design.
    """
    if profile.symbol_kind not in {"function", "method"}:
        return None
    call_defaults = [
        f
        for f in _struct(profile, "parameter_default")
        if str(f.payload.get("default_kind", "")) == "Call"
    ]
    if not call_defaults:
        return None
    if not _dfg(profile, "callable_value"):
        return None
    return ContainerKindMatch(
        kind="di_container",
        symbol_uid=profile.symbol_uid,
        qualified_name=profile.qualified_name,
        evidence_bits=(
            ("struct", "function_def"),
            ("struct", "parameter_default"),
            ("dfg", "parameter_default_value"),
            ("dfg", "callable_value"),
        ),
        evidence_probes=(),
        payload={
            "call_default_parameters": [str(f.payload.get("name") or "") for f in call_defaults][
                :8
            ],
            "call_default_count": len(call_defaults),
        },
    )


@register_kind("task_register")
def _classify_task_register(
    profile: AxisProfile,
    probe: GraphContextProbe,
) -> ContainerKindMatch | None:
    """Container that registers callables as deferred / queued tasks.

    Discriminator from ``web_route_register`` and ``signal_register`` is which
    external packages the container's enclosing file imports — that is graph
    context, not axis content. Local-only axis bits cannot pull these apart
    cleanly, so this kind is **marker-primary**:

    - If a library marker says ``task_register``, we accept (Celery, Dramatiq,
      RQ, Huey — the catalogue carries the external symbol → kind mapping).
    - There is no axis-only fallback. A standalone "class with decorator
      method that wraps callables" fingerprint matches every registry kind;
      claiming task_register from it would be a name match in disguise (we
      could only justify it by looking at messaging-related identifiers).

    Honest result: when graph context is unavailable, this predicate returns
    ``None`` and the question lands in the contract compiler with task_register
    unproven. That diagnostic is the correct one — the discriminator genuinely
    lives outside axis bits.
    """
    library_kinds = probe.library_marker_kinds(profile.symbol_uid)
    if "task_register" not in library_kinds:
        return None
    return ContainerKindMatch(
        kind="task_register",
        symbol_uid=profile.symbol_uid,
        qualified_name=profile.qualified_name,
        evidence_bits=(),
        evidence_probes=("library_marker:task_register",),
        payload={"via": "library_marker"},
    )


@register_kind("web_route_register")
def _classify_web_route_register(
    profile: AxisProfile,
    probe: GraphContextProbe,
) -> ContainerKindMatch | None:
    """Container that maps URL/HTTP-method literals to handler callables.

    Local axis bits alone are too broad here: "two keyed writes of callables"
    also describes generic callback tables. Until graph context can prove
    route topology, this kind is marker/probe-only.
    """
    library_kinds = probe.library_marker_kinds(profile.symbol_uid)
    if "web_route_register" not in library_kinds:
        return None
    return ContainerKindMatch(
        kind="web_route_register",
        symbol_uid=profile.symbol_uid,
        qualified_name=profile.qualified_name,
        evidence_bits=(),
        evidence_probes=("library_marker:web_route_register",),
        payload={"via": "library_marker"},
    )


# ---------------------------------------------------------------------------
# Top-level classifier
# ---------------------------------------------------------------------------


@dataclass
class ContainerKindClassifier:
    """Run every registered kind predicate against a profile."""

    probe: GraphContextProbe = field(default_factory=NullGraphProbe)

    def classify(self, profile: AxisProfile) -> list[ContainerKindMatch]:
        matches: list[ContainerKindMatch] = []
        for kind, predicate in _PREDICATES.items():
            result = predicate(profile, self.probe)
            if result is not None and result.kind == kind:
                matches.append(result)
        return matches

    def classify_many(
        self,
        profiles: Iterable[AxisProfile],
    ) -> dict[str, list[ContainerKindMatch]]:
        """Return ``{symbol_uid: [matches]}`` over a batch of profiles."""
        out: dict[str, list[ContainerKindMatch]] = {}
        for profile in profiles:
            matches = self.classify(profile)
            if matches:
                out[profile.symbol_uid] = matches
        return out

    def registered_kinds(self) -> list[str]:
        return sorted(_PREDICATES)


__all__ = [
    "ContainerKindClassifier",
    "ContainerKindMatch",
    "GraphContextProbe",
    "NullGraphProbe",
    "register_kind",
]
