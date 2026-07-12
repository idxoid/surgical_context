"""Dynamic-boundary edges: proxy, decorator, hook, metadata, http, injection."""

from collections import defaultdict
from typing import Any

from context_engine.database.neo4j._common import (
    HOOK_AMBIGUITY_MAX,
    METADATA_BRIDGE_FANOUT_MAX,
    _bump_workspace_graph_version,
)
from context_engine.workspace import DEFAULT_WORKSPACE_ID


class DynamicEdgesMixin:
    driver: Any

    def link_proxy_bindings(
        self,
        proxy_bindings: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create ProxyBinding nodes + PROXY_OF edges for lazy-proxy module vars.

        A ProxyBinding is a transit anchor (``kind='proxy_binding'``), not a retrieval
        target; the resolution phase forwards calls THROUGH it to the real type. The
        ``PROXY_OF`` edge points at the annotated target type (matched by trailing
        qualified-name segment, robust to source-root prefix differences).
        """
        if not proxy_bindings:
            return
        with self.driver.session() as session:
            session.execute_write(self._create_proxy_relations, proxy_bindings, workspace_id)
            _bump_workspace_graph_version(session, workspace_id)

    @staticmethod
    def _create_proxy_relations(tx, proxy_bindings, workspace_id):
        if not proxy_bindings:
            return
        tx.run(
            """
            UNWIND $bindings AS b
            MATCH (f:File {path: b.file_path, workspace_id: $workspace_id})
            MERGE (p:Symbol {uid: b.proxy_uid})
            SET p.workspace_id = $workspace_id,
                p.name = b.proxy_name,
                p.kind = 'proxy_binding',
                p.qualified_name = b.proxy_qualified_name,
                p.context_var = coalesce(b.context_var, ''),
                p.context_type = coalesce(b.context_type, ''),
                p.context_attr = coalesce(b.context_attr, ''),
                p.binding_source = coalesce(b.binding_source, '')
            MERGE (f)-[:CONTAINS {workspace_id: $workspace_id}]->(p)
            WITH p, b
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(target:Symbol)
            WHERE target.kind IN ['class', 'function', 'method']
              AND (target.qualified_name = b.target_type
                   OR target.qualified_name ENDS WITH ('.' + split(b.target_type, '.')[-1]))
            WITH p, b, target
            ORDER BY size(target.qualified_name) ASC
            WITH p, b, collect(target)[0] AS target
            WHERE target IS NOT NULL
            MERGE (p)-[r:PROXY_OF {workspace_id: $workspace_id}]->(target)
            SET r.resolver = CASE b.target_source
                               WHEN 'wrapped_callable' THEN 'proxysurface-callable-v1'
                               ELSE 'proxysurface-v1' END,
                r.target_source = coalesce(b.target_source, 'annotation'),
                r.wrapped_callable = b.wrapped_callable,
                r.confidence = coalesce(b.confidence, 1.0)
            WITH p, b
            OPTIONAL MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(accessor:Symbol)
            WHERE coalesce(b.context_type, '') <> ''
              AND coalesce(b.context_attr, '') <> ''
              AND (
                accessor.qualified_name = b.context_type + '.' + b.context_attr
                OR accessor.qualified_name ENDS WITH (
                  '.' + split(b.context_type, '.')[-1] + '.' + b.context_attr
                )
              )
            WITH p, b, accessor
            ORDER BY size(accessor.qualified_name) ASC
            WITH p, b, collect(accessor)[0] AS accessor
            FOREACH (_ IN CASE WHEN accessor IS NULL THEN [] ELSE [1] END |
              MERGE (p)-[ra:RESOLVES_ATTR {workspace_id: $workspace_id}]->(accessor)
              SET ra.resolver = 'proxysurface-context-v1',
                  ra.context_var = b.context_var,
                  ra.context_type = b.context_type,
                  ra.context_attr = b.context_attr,
                  ra.confidence = coalesce(b.confidence, 1.0)
            )
            """,
            bindings=proxy_bindings,
            workspace_id=workspace_id,
        )

    def resolve_proxy_calls(
        self,
        proxy_calls: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> int:
        """Forward calls on a proxy var to the real type's method via PROXY_OF.

        ``proxy_calls`` are parsed call facts whose ``callee_qualified_name`` is
        ``<proxy_var_qn>.<method>``. We split off the trailing method, match the
        prefix to a ProxyBinding (by trailing var name, prefix-agnostic), follow
        ``PROXY_OF`` to the target type, and wire ``caller -> target.method`` (the
        method found directly on the target or via INHERITED_API). The ``via_proxy``
        edge property marks the hop as transparent for the ranker.
        """
        if not proxy_calls:
            return 0
        rows = []
        for c in proxy_calls:
            qn = c.get("callee_qualified_name") or ""
            if "." not in qn:
                continue
            prefix, _, method = qn.rpartition(".")
            proxy_var = prefix.rpartition(".")[2]
            if not proxy_var or not method or not c.get("caller_uid"):
                continue
            rows.append(
                {
                    "caller_uid": c["caller_uid"],
                    "proxy_var": proxy_var,
                    "method": method,
                    "call_site_line": c.get("call_site_line") or 0,
                }
            )
        if not rows:
            return 0
        query = """
        UNWIND $rows AS row
        MATCH (caller:Symbol {uid: row.caller_uid})
        MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(p:Symbol {kind: 'proxy_binding', name: row.proxy_var})
        MATCH (p)-[:PROXY_OF {workspace_id: $workspace_id}]->(t:Symbol)
        OPTIONAL MATCH (t)-[:HAS_API|INHERITED_API]->(direct:Symbol {name: row.method})
        OPTIONAL MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(own:Symbol {name: row.method})
        WHERE own.qualified_name STARTS WITH t.qualified_name + '.'
        WITH caller, p, row, coalesce(direct, own) AS callee
        WHERE callee IS NOT NULL AND caller <> callee
        MERGE (caller)-[r:CALLS_DYNAMIC {workspace_id: $workspace_id,
                                        call_site_line: row.call_site_line}]->(callee)
        SET r.confidence = 0.75,
            r.tier = 'proxy',
            r.resolver = 'proxysurface-v1',
            r.via_proxy = row.proxy_var
        RETURN count(r) AS created
        """
        try:
            with self.driver.session() as session:
                rec = session.run(query, rows=rows, workspace_id=workspace_id).single()
                created = int(rec["created"]) if rec else 0
                if created:
                    _bump_workspace_graph_version(session, workspace_id)
                return created
        except Exception:
            return 0

    def resolve_proxy_return_calls(
        self,
        candidates: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> int:
        """Wire ``L.method()`` → ``C.method`` where ``L = self.M()`` and ``M``
        returns a lazy-proxy global whose ``PROXY_OF`` target is class ``C``.

        Sibling of :meth:`resolve_proxy_calls`: that one forwards a *direct*
        proxy-var call (``current_app.x()``); this one closes the case where the
        proxy is reached through a method return (``app = self._get_app();
        app.x()``). ``candidates`` carry ``returns_global_qn`` (the global the
        method returns) and ``callee_name`` (the method invoked on the local).
        We match the proxy binding by the global's trailing var name (prefix-
        agnostic, like the sibling), follow ``PROXY_OF`` to ``C``, resolve
        ``C.callee_name`` (direct or via ``INHERITED_API``), and wire a
        ``CALLS_DYNAMIC`` edge (in the AFFECTS rel set, so the impact closure
        picks it up). The ``via_proxy_return`` property marks the hop.
        """
        if not candidates:
            return 0
        rows = []
        for c in candidates:
            qn = c.get("returns_global_qn") or ""
            proxy_var = qn.rpartition(".")[2]
            method = c.get("callee_name")
            if not proxy_var or not method or not c.get("caller_uid"):
                continue
            rows.append(
                {
                    "caller_uid": c["caller_uid"],
                    "proxy_var": proxy_var,
                    "method": method,
                    "call_site_line": c.get("call_site_line") or 0,
                }
            )
        if not rows:
            return 0
        query = """
        UNWIND $rows AS row
        MATCH (caller:Symbol {uid: row.caller_uid})
        MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(p:Symbol {kind: 'proxy_binding', name: row.proxy_var})
        MATCH (p)-[:PROXY_OF {workspace_id: $workspace_id}]->(t:Symbol)
        OPTIONAL MATCH (t)-[:HAS_API|INHERITED_API]->(direct:Symbol {name: row.method})
        OPTIONAL MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(own:Symbol {name: row.method})
        WHERE own.qualified_name STARTS WITH t.qualified_name + '.'
        WITH caller, row, coalesce(direct, own) AS callee
        WHERE callee IS NOT NULL AND caller <> callee
        MERGE (caller)-[r:CALLS_DYNAMIC {workspace_id: $workspace_id,
                                        call_site_line: row.call_site_line}]->(callee)
        SET r.confidence = 0.7,
            r.tier = 'proxy_return',
            r.resolver = 'proxyreturn-v1',
            r.via_proxy_return = row.proxy_var
        RETURN count(r) AS created
        """
        try:
            with self.driver.session() as session:
                rec = session.run(query, rows=rows, workspace_id=workspace_id).single()
                created = int(rec["created"]) if rec else 0
                if created:
                    _bump_workspace_graph_version(session, workspace_id)
                return created
        except Exception:
            return 0

    def delete_proxy_bindings_for_file(
        self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ):
        """Remove ProxyBinding nodes (and their edges) for a file before relinking."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(p:Symbol {kind: 'proxy_binding'})
                DETACH DELETE p
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def link_decorators(
        self,
        decorators: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create DECORATED_BY and HANDLES edges from decoration facts.

        DECORATED_BY: decorated_symbol -> decorator (handler → registry hook).
        HANDLES: decorator -> decorated_symbol (dispatcher → registered handler).
        Both are derived from the same ``@deco`` AST fact; HANDLES is the inverse
        edge ranker BFS needs to walk from ``@app.route`` / ``@app.task`` outward.
        """
        if not decorators:
            return
        with self.driver.session() as session:
            session.execute_write(self._create_decorator_relations, decorators, workspace_id)
            _bump_workspace_graph_version(session, workspace_id)

    @staticmethod
    def _create_decorator_relations(tx, decorators, workspace_id):
        if not decorators:
            return
        tx.run(
            """
            UNWIND $decorators AS d
            MATCH (decorated:Symbol {uid: d.decorated_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(deco:Symbol)
            WHERE deco.qualified_name = d.decorator_qualified_name
               OR deco.name = d.decorator_name
            WITH decorated, d, deco
            ORDER BY
              CASE WHEN deco.qualified_name = d.decorator_qualified_name THEN 0 ELSE 1 END,
              size(deco.qualified_name) ASC
            WITH decorated, d, collect(deco)[0] AS deco
            WHERE deco IS NOT NULL AND decorated <> deco
            MERGE (decorated)-[r:DECORATED_BY {workspace_id: $workspace_id}]->(deco)
            SET r.resolver = 'decorator-v1',
                r.decorator_name = d.decorator_name
            MERGE (deco)-[h:HANDLES {workspace_id: $workspace_id}]->(decorated)
            SET h.resolver = 'decorator-v1',
                h.decorator_name = d.decorator_name
            """,
            decorators=decorators,
            workspace_id=workspace_id,
        )
        tx.run(
            """
            UNWIND $decorators AS d
            WITH d
            WHERE coalesce(d.decorator_owner_qualified_name, '') <> ''
            MATCH (decorated:Symbol {uid: d.decorated_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(owner:Symbol)
            WHERE (owner.qualified_name = d.decorator_owner_qualified_name
               OR owner.name = d.decorator_owner_name)
              AND coalesce(owner.kind, '') IN ['class', 'interface']
            WITH decorated, d, owner
            ORDER BY
              CASE WHEN owner.qualified_name = d.decorator_owner_qualified_name THEN 0 ELSE 1 END,
              size(owner.qualified_name) ASC
            WITH decorated, d, collect(owner)[0] AS owner
            WHERE owner IS NOT NULL AND decorated <> owner
            MERGE (owner)-[h:HANDLES {workspace_id: $workspace_id}]->(decorated)
            SET h.resolver = 'decorator-owner-v1',
                h.decorator_name = d.decorator_name,
                h.decorator_owner_name = d.decorator_owner_name
            """,
            decorators=decorators,
            workspace_id=workspace_id,
        )
        # Variable-owner branch: ``@app.get(...)`` where ``app`` is a
        # module-level Variable Symbol holding an external instance (e.g.
        # ``app = FastAPI()`` / ``app = Flask(__name__)``). The variable is
        # admitted as an owner only when it carries at least one outgoing
        # ``INSTANTIATES_EXTERNAL`` edge — that is the structural proof
        # that the variable is *an instance of something external*, which
        # is the kind of object that legitimately acts as a registry hook
        # in a decorator. Plain unrelated module-level variables don't get
        # promoted to decorator owners and stay out of the HANDLES graph.
        tx.run(
            """
            UNWIND $decorators AS d
            WITH d
            WHERE coalesce(d.decorator_owner_qualified_name, '') <> ''
            MATCH (decorated:Symbol {uid: d.decorated_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(owner:Symbol)
            WHERE (owner.qualified_name = d.decorator_owner_qualified_name
               OR owner.name = d.decorator_owner_name)
              AND coalesce(owner.kind, '') = 'variable'
            MATCH (owner)-[ext:INSTANTIATES_EXTERNAL]->(:ExternalSymbol)
            WHERE coalesce(ext.workspace_id, $workspace_id) = $workspace_id
            WITH decorated, d, owner
            ORDER BY
              CASE WHEN owner.qualified_name = d.decorator_owner_qualified_name THEN 0 ELSE 1 END,
              size(owner.qualified_name) ASC
            WITH decorated, d, collect(owner)[0] AS owner
            WHERE owner IS NOT NULL AND decorated <> owner
            MERGE (owner)-[h:HANDLES {workspace_id: $workspace_id}]->(decorated)
            SET h.resolver = 'decorator-owner-v1-variable',
                h.decorator_name = d.decorator_name,
                h.decorator_owner_name = d.decorator_owner_name
            """,
            decorators=decorators,
            workspace_id=workspace_id,
        )

    def link_hooks(
        self,
        hooks: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create the EVENT channel + HOOK wrapper edges from hook facts.

        Two ORTHOGONAL, nested-but-separate layers come out of the same facts
        (see ``extract_hooks``):

        * **EVENT (channel)** — ``EVENT_SUB`` / ``EVENT_PUB`` wire a site to the
          TOPIC it subscribes / publishes to (the event method declaration, or a
          module-level Signal object). This is the pub/sub channel: subscriber
          and publisher meet only at the topic. ``config`` == subscribe,
          ``exec`` == publish.
        * **HOOK (wrapper)** — ``HOOK_CONFIG`` / ``HOOK_EXEC`` wire the same site
          to the registration / dispatch API it goes THROUGH (the opaque
          wrapper: ``listens_for``/``receiver``/``connect`` for config,
          ``dispatch``/``send`` for exec). A hook *creates* a subscription, so a
          subscribe site carries BOTH an EVENT_SUB (to the topic) and a
          HOOK_CONFIG (to the wrapper); a pure publish has only EVENT_PUB unless
          its dispatch api resolves.

        Resolution is name → declaration in both layers. Per precision over
        recall the linker ABSTAINS when the name is ambiguous (more than
        ``HOOK_AMBIGUITY_MAX`` carriers) — an ambiguous name is an honest gap,
        not a fan of guessed edges.
        """
        if not hooks:
            return
        with self.driver.session() as session:
            session.execute_write(self._create_hook_relations, hooks, workspace_id)
            _bump_workspace_graph_version(session, workspace_id)

    @staticmethod
    def _create_hook_relations(tx, hooks, workspace_id):
        if not hooks:
            return
        # EVENT channel, declared-method topic: a string-literal hook name -> the
        # event-method declaration that IS the topic (sqlalchemy
        # ``listens_for``/``.dispatch.X``). config == subscribe -> EVENT_SUB,
        # exec == publish -> EVENT_PUB; the wrapper they go through is the
        # separate HOOK layer below. The class-method gate is already precise, so
        # no co-occurrence prune applies to this subtype.
        tx.run(
            """
            UNWIND $hooks AS h
            WITH h WHERE coalesce(h.target_kind, 'method') = 'method'
            MATCH (site:Symbol {uid: h.site_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(decl:Symbol)
            WHERE decl.name = h.hook_name AND coalesce(decl.kind, '') = 'function'
            MATCH (cls:Symbol)-[:HAS_API]->(decl)
            WHERE coalesce(cls.kind, '') IN ['class', 'interface']
            WITH site, h, collect(DISTINCT decl) AS decls
            WHERE size(decls) >= 1 AND size(decls) <= $ambig_max
            UNWIND decls AS decl
            WITH site, h, decl
            WHERE site <> decl
            FOREACH (_ IN CASE WHEN h.kind = 'config' THEN [1] ELSE [] END |
                MERGE (site)-[r:EVENT_SUB {workspace_id: $workspace_id, hook_name: h.hook_name}]->(decl)
                SET r.resolver = 'event-method-v1', r.via = coalesce(h.via, ''))
            FOREACH (_ IN CASE WHEN h.kind = 'exec' THEN [1] ELSE [] END |
                MERGE (site)-[r:EVENT_PUB {workspace_id: $workspace_id, hook_name: h.hook_name}]->(decl)
                SET r.resolver = 'event-method-v1', r.via = coalesce(h.via, ''))
            """,
            hooks=hooks,
            workspace_id=workspace_id,
            ambig_max=HOOK_AMBIGUITY_MAX,
        )
        # event/pub-sub kind: the hook is an OBJECT reference (a Signal), not a
        # literal — a DISTINCT class from declared hooks. Subscribe sites
        # (``@receiver``/``.connect``) emit EVENT_SUB, publish sites (``.send``)
        # emit EVENT_PUB. Resolve the name to a module-level variable gated on an
        # outgoing INSTANTIATES / INSTANTIATES_EXTERNAL edge (an instantiated
        # module-level object). ``via`` records the idiom for the co-occurrence
        # prune below.
        tx.run(
            """
            UNWIND $hooks AS h
            WITH h WHERE coalesce(h.target_kind, 'method') = 'object'
            MATCH (site:Symbol {uid: h.site_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(decl:Symbol)
            WHERE decl.name = h.hook_name AND coalesce(decl.kind, '') = 'variable'
            MATCH (decl)-[:INSTANTIATES|INSTANTIATES_EXTERNAL]->()
            WITH site, h, collect(DISTINCT decl) AS decls
            WHERE size(decls) >= 1 AND size(decls) <= $ambig_max
            UNWIND decls AS decl
            WITH site, h, decl
            WHERE site <> decl
            FOREACH (_ IN CASE WHEN h.kind = 'config' THEN [1] ELSE [] END |
                MERGE (site)-[r:EVENT_SUB {workspace_id: $workspace_id, hook_name: h.hook_name}]->(decl)
                SET r.resolver = 'event-signal-v1', r.via = coalesce(h.via, ''))
            FOREACH (_ IN CASE WHEN h.kind = 'exec' THEN [1] ELSE [] END |
                MERGE (site)-[r:EVENT_PUB {workspace_id: $workspace_id, hook_name: h.hook_name}]->(decl)
                SET r.resolver = 'event-signal-v1', r.via = coalesce(h.via, ''))
            """,
            hooks=hooks,
            workspace_id=workspace_id,
            ambig_max=HOOK_AMBIGUITY_MAX,
        )
        # Handler registration (Express ``app.use(mw)``, interceptors, RxJS
        # ``.subscribe(handler)``): resolve the registered handler by bare name
        # without requiring a class HAS_API surface.
        tx.run(
            """
            UNWIND $hooks AS h
            WITH h WHERE coalesce(h.target_kind, '') = 'handler'
            MATCH (site:Symbol {uid: h.site_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(decl:Symbol)
            WHERE decl.name = h.hook_name
              AND coalesce(decl.kind, '') IN ['function', 'method']
            WITH site, h, collect(DISTINCT decl) AS decls
            WHERE size(decls) >= 1 AND size(decls) <= $ambig_max
            UNWIND decls AS decl
            WITH site, h, decl
            WHERE site <> decl
            FOREACH (_ IN CASE WHEN h.kind = 'config' THEN [1] ELSE [] END |
                MERGE (site)-[r:EVENT_SUB {workspace_id: $workspace_id, hook_name: h.hook_name}]->(decl)
                SET r.resolver = 'event-handler-v1', r.via = coalesce(h.via, ''))
            FOREACH (_ IN CASE WHEN h.kind = 'exec' THEN [1] ELSE [] END |
                MERGE (site)-[r:EVENT_PUB {workspace_id: $workspace_id, hook_name: h.hook_name}]->(decl)
                SET r.resolver = 'event-handler-v1', r.via = coalesce(h.via, ''))
            """,
            hooks=hooks,
            workspace_id=workspace_id,
            ambig_max=HOOK_AMBIGUITY_MAX,
        )
        # pub/sub co-occurrence prune: ``.connect``/``.send`` are generic verbs, so
        # an event target is kept only when it has the signal shape — subscribed
        # via ``@receiver`` (idiom is signal-specific), OR both connect-ed AND
        # sent-from (subscribed AND published). A target only ``.connect``-ed and
        # never ``.send``-from (a DB connection proxy, a websocket
        # ConnectionManager) is dropped. Runs BEFORE the supersede so a pruned pair
        # keeps its original READS_ATTR/CALLS_DYNAMIC.
        tx.run(
            """
            MATCH (s)-[h:EVENT_SUB|EVENT_PUB]->(decl:Symbol)
            WHERE h.resolver = 'event-signal-v1'
              AND coalesce(h.workspace_id, $workspace_id) = $workspace_id
            WITH decl, collect(DISTINCT (type(h) + ':' + coalesce(h.via, ''))) AS sigs
            WITH decl,
                 ('EVENT_SUB:receiver' IN sigs) AS has_receiver,
                 ('EVENT_SUB:connect' IN sigs) AS has_connect,
                 ('EVENT_PUB:send' IN sigs) AS has_send
            WHERE NOT (has_receiver OR (has_connect AND has_send))
            MATCH (s2)-[bad:EVENT_SUB|EVENT_PUB]->(decl)
            WHERE bad.resolver = 'event-signal-v1'
              AND coalesce(bad.workspace_id, $workspace_id) = $workspace_id
            DELETE bad
            """,
            workspace_id=workspace_id,
        )
        # HOOK wrapper layer (DISTINCT from the EVENT channel above). EVENT_* wire
        # a site to the TOPIC; a HOOK edge wires the SAME site to the
        # registration / dispatch API it goes THROUGH — the opaque wrapper
        # (``listens_for``/``receiver``/``connect``/``listen`` for config,
        # ``dispatch``/``send`` for exec). Hook and event are nested but separate:
        # a subscribe site gets BOTH EVENT_SUB (topic) and HOOK_CONFIG (wrapper).
        # ``via`` is the api token; resolution is name -> declaration, ABSTAINING
        # past ambig_max — so common verbs (``send``/``connect``/``dispatch``)
        # stay an honest gap while rare names (``listens_for``) resolve. The edge
        # targets the api, so it has no parallel READS_ATTR/CALLS_DYNAMIC to
        # supersede below.
        tx.run(
            """
            UNWIND $hooks AS h
            WITH h WHERE coalesce(h.via, '') <> ''
            MATCH (site:Symbol {uid: h.site_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(api:Symbol)
            WHERE api.name = h.via AND coalesce(api.kind, '') = 'function'
            WITH site, h, collect(DISTINCT api) AS apis
            WHERE size(apis) >= 1 AND size(apis) <= $ambig_max
            UNWIND apis AS api
            WITH site, h, api
            WHERE site <> api
            FOREACH (_ IN CASE WHEN h.kind = 'config' THEN [1] ELSE [] END |
                MERGE (site)-[r:HOOK_CONFIG {workspace_id: $workspace_id}]->(api)
                SET r.resolver = 'hook-api-v1', r.via = h.via)
            FOREACH (_ IN CASE WHEN h.kind = 'exec' THEN [1] ELSE [] END |
                MERGE (site)-[r:HOOK_EXEC {workspace_id: $workspace_id}]->(api)
                SET r.resolver = 'hook-api-v1', r.via = h.via)
            """,
            hooks=hooks,
            workspace_id=workspace_id,
            ambig_max=HOOK_AMBIGUITY_MAX,
        )
        # Replace the broad edge with the precise one: where an EVENT edge now
        # captures a site->topic pair, the parallel READS_ATTR / CALLS_DYNAMIC
        # that the attr-access / call-resolution phases emitted for the same
        # `.dispatch.<name>` access is a coarser duplicate — drop it so the
        # relationship is carried only by the precise EVENT edge. READS_ATTR
        # is out of materialized degree; CALLS_DYNAMIC is in it, but the degree
        # recompute (stage 4.7) runs after this phase, so the count stays
        # consistent. Walk coverage is unchanged (EVENT_* sit in the same
        # BINDING/PROXIMITY profiles).
        tx.run(
            """
            MATCH (site:Symbol)-[hk:EVENT_SUB|EVENT_PUB]->(decl:Symbol)
            WHERE coalesce(hk.workspace_id, $workspace_id) = $workspace_id
            MATCH (site)-[r:READS_ATTR|CALLS_DYNAMIC]->(decl)
            WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
            DELETE r
            """,
            workspace_id=workspace_id,
        )

    def delete_hooks_for_file(self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID):
        """Clear EVENT / HOOK edges whose site symbol lives in ``file_path``."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(site:Symbol)
                OPTIONAL MATCH (site)-[r:EVENT_SUB|EVENT_PUB|HOOK_CONFIG|HOOK_EXEC]->()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def link_metadata_bridges(
        self,
        facts: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create ``METADATA_BRIDGE`` edges from reflect-metadata producer/consumer facts.

        Producers (``Reflect.defineMetadata`` / ``SetMetadata``) and consumers
        (``Reflect.getMetadata`` / ``Reflector.get*``) are paired by the shared
        metadata-key qualified name (see ``extract_metadata_bridges``). A read
        whose key has no producer in the workspace yields no edge — the
        precision gate that lets generic ``reflector.get`` reads abstain. The
        edge points producer→consumer; the walk traverses it undirected.
        """
        if not facts:
            return
        with self.driver.session() as session:
            session.execute_write(self._create_metadata_bridges, facts, workspace_id)
            _bump_workspace_graph_version(session, workspace_id)

    @staticmethod
    def _metadata_bridge_indexes(
        facts: list[dict],
    ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        defines: dict[str, set[str]] = defaultdict(set)
        reads: dict[str, set[str]] = defaultdict(set)
        for fact in facts:
            site = fact.get("site_uid")
            key = fact.get("key_qn")
            role = fact.get("role")
            if not site or not key:
                continue
            if role == "define":
                defines[key].add(site)
            elif role == "read":
                reads[key].add(site)
        return defines, reads

    @staticmethod
    def _metadata_bridge_pairs(
        defines: dict[str, set[str]],
        reads: dict[str, set[str]],
    ) -> list[dict]:
        pairs: list[dict] = []
        for key, producers in defines.items():
            consumers = reads.get(key)
            if not consumers:
                continue
            if (
                len(producers) > METADATA_BRIDGE_FANOUT_MAX
                or len(consumers) > METADATA_BRIDGE_FANOUT_MAX
            ):
                continue
            for producer in producers:
                for consumer in consumers:
                    if producer == consumer:
                        continue
                    pairs.append({"producer": producer, "consumer": consumer, "key": key})
        return pairs

    @staticmethod
    def _create_metadata_bridges(tx, facts, workspace_id):
        if not facts:
            return
        defines, reads = DynamicEdgesMixin._metadata_bridge_indexes(facts)
        pairs = DynamicEdgesMixin._metadata_bridge_pairs(defines, reads)
        if not pairs:
            return
        tx.run(
            """
            UNWIND $pairs AS p
            MATCH (d:Symbol {uid: p.producer})
            MATCH (r:Symbol {uid: p.consumer})
            MERGE (d)-[e:METADATA_BRIDGE {workspace_id: $workspace_id, key: p.key}]->(r)
            SET e.resolver = 'ts-metadata-bridge-v1'
            """,
            pairs=pairs,
            workspace_id=workspace_id,
        )

    def delete_metadata_bridges_for_file(
        self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ):
        """Clear METADATA_BRIDGE edges incident to a symbol in ``file_path`` (either end)."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(site:Symbol)
                OPTIONAL MATCH (site)-[r:METADATA_BRIDGE]-()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def link_http_endpoints(
        self,
        facts: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Materialize ``ApiEndpoint`` nodes and client/server bridge edges.

        ``implement`` facts become ``IMPLEMENTS_ENDPOINT``; ``call`` facts become
        ``CALLS_ENDPOINT``. Endpoints are keyed by ``method:path`` fingerprint
        within the workspace so TS clients and Python handlers can meet on the
        same node in a monorepo.
        """
        if not facts:
            return
        with self.driver.session() as session:
            session.execute_write(self._create_http_endpoints, facts, workspace_id)
            _bump_workspace_graph_version(session, workspace_id)

    @staticmethod
    def _create_http_endpoints(tx, facts, workspace_id):
        from context_engine.indexer.http_endpoint import endpoint_fingerprint

        implement_rows: list[dict] = []
        call_rows: list[dict] = []
        for fact in facts:
            site_uid = fact.get("site_uid")
            method = fact.get("method")
            path = fact.get("path")
            role = fact.get("role")
            via = fact.get("via") or ""
            if not site_uid or not method or not path or role not in {"implement", "call"}:
                continue
            row = {
                "site_uid": site_uid,
                "method": method,
                "path": path,
                "fingerprint": endpoint_fingerprint(method, path),
                "via": via,
            }
            if role == "implement":
                implement_rows.append(row)
            else:
                call_rows.append(row)

        if implement_rows:
            tx.run(
                """
                UNWIND $rows AS row
                MERGE (e:ApiEndpoint {workspace_id: $workspace_id, fingerprint: row.fingerprint})
                SET e.method = row.method, e.path = row.path
                WITH e, row
                MATCH (s:Symbol {uid: row.site_uid})
                MERGE (s)-[r:IMPLEMENTS_ENDPOINT {workspace_id: $workspace_id}]->(e)
                SET r.via = row.via, r.resolver = 'http-endpoint-v1'
                """,
                rows=implement_rows,
                workspace_id=workspace_id,
            )
        if call_rows:
            tx.run(
                """
                UNWIND $rows AS row
                MERGE (e:ApiEndpoint {workspace_id: $workspace_id, fingerprint: row.fingerprint})
                SET e.method = row.method, e.path = row.path
                WITH e, row
                MATCH (s:Symbol {uid: row.site_uid})
                MERGE (s)-[r:CALLS_ENDPOINT {workspace_id: $workspace_id}]->(e)
                SET r.via = row.via, r.resolver = 'http-endpoint-v1'
                """,
                rows=call_rows,
                workspace_id=workspace_id,
            )

    def delete_http_endpoints_for_file(
        self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ):
        """Clear HTTP endpoint bridge edges for symbols defined in ``file_path``."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(site:Symbol)
                OPTIONAL MATCH (site)-[r:IMPLEMENTS_ENDPOINT|CALLS_ENDPOINT]->(:ApiEndpoint)
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def link_attr_accesses(
        self,
        accesses: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create READS_ATTR / WRITES_ATTR edges from the accessor function
        to an attribute symbol resolved by qualified-name with name-uniqueness
        fallback.

        Attribute access is the structural backbone of binding-surface
        signals — a function reading ``self.config`` or writing
        ``self.fields[k] = v`` carries data-shape evidence that pure call
        edges miss. The edge ``kind`` carries the specific access form:
        ``read``, ``write``, ``write_subscript`` (mapping/sequence write
        into the attribute), or ``write_subscript_local`` (write into a
        typed local).
        """
        if not accesses:
            return
        with self.driver.session() as session:
            session.execute_write(self._create_attr_access_relations, accesses, workspace_id)
            _bump_workspace_graph_version(session, workspace_id)

    @staticmethod
    def _create_attr_access_relations(tx, accesses, workspace_id):
        if not accesses:
            return
        # Resolve attribute Symbols workspace-wide by qualified_name first
        # (the strong match), then by unique name. Mirrors the call
        # resolver's safety: name fallback fires only when exactly one
        # Symbol carries that name. Reads and writes are split into two
        # MERGEs by edge type.
        reads = [a for a in accesses if a.get("kind") == "read"]
        writes = [
            a
            for a in accesses
            if a.get("kind") in ("write", "write_subscript", "write_subscript_local")
        ]
        for rel_type, rows in (("READS_ATTR", reads), ("WRITES_ATTR", writes)):
            if not rows:
                continue
            # Resolution tiers, in order of strength:
            #   1. qualified-name exact match (``ClassName.attr``) — strong.
            #   2. workspace-unique name match — fires ONLY when exactly one
            #      Symbol carries the name. A non-unique bare name (e.g.
            #      ``send_task`` defined on several classes) is genuinely
            #      ambiguous without receiver type, so it binds to NOTHING
            #      rather than guessing the shortest-qn candidate (which would
            #      be an arbitrary, often wrong, target). Precision over recall.
            tx.run(
                f"""
                UNWIND $rows AS a
                MATCH (accessor:Symbol {{uid: a.accessor_uid}})
                MATCH (:File {{workspace_id: $workspace_id}})-[:CONTAINS]->(attr:Symbol)
                WHERE attr.qualified_name = a.attr_qualified_name
                   OR attr.name = a.attr_name
                WITH accessor, a,
                     [x IN collect(attr)
                        WHERE x.qualified_name = a.attr_qualified_name] AS qn_exact,
                     [x IN collect(attr)
                        WHERE x.qualified_name <> a.attr_qualified_name] AS name_only
                WITH accessor, a,
                     CASE
                       WHEN size(qn_exact) >= 1 THEN qn_exact[0]
                       WHEN size(name_only) = 1 THEN name_only[0]
                       ELSE null
                     END AS attr
                WHERE attr IS NOT NULL AND accessor <> attr
                MERGE (accessor)-[r:{rel_type} {{workspace_id: $workspace_id}}]->(attr)
                SET r.resolver = 'attr-access-v1',
                    r.kind = a.kind
                """,
                rows=rows,
                workspace_id=workspace_id,
            )

    def delete_attr_accesses_for_file(
        self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ):
        """Clear READS_ATTR / WRITES_ATTR edges originating in ``file_path``."""
        with self.driver.session() as session:
            for rel_type in ("READS_ATTR", "WRITES_ATTR"):
                session.run(
                    f"""
                    MATCH (f:File {{path: $path, workspace_id: $workspace_id}})
                          -[:CONTAINS]->(s:Symbol)-[r:{rel_type}]->()
                    WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                    DELETE r
                    """,
                    path=file_path,
                    workspace_id=workspace_id,
                )

    def link_decorator_compositions(
        self,
        compositions: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create COMPOSES edges from decorated class → each composed symbol.

        Subtype 2 of composition_surface: a class decorated with
        ``@Module({ imports, providers, controllers })`` names the components
        it composes inline. Each name is an AST-visible identifier in an
        array under the decorator's object-literal argument. Resolution to a
        Symbol uses the import-resolved qualified name, falling back to a
        bare-name match to keep external symbols traceable.
        """
        if not compositions:
            return
        with self.driver.session() as session:
            session.execute_write(
                self._create_decorator_composition_relations, compositions, workspace_id
            )
            _bump_workspace_graph_version(session, workspace_id)

    @staticmethod
    def _create_decorator_composition_relations(tx, compositions, workspace_id):
        if not compositions:
            return
        tx.run(
            """
            UNWIND $compositions AS c
            MATCH (decorated:Symbol {uid: c.decorated_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(ref:Symbol)
            WHERE ref.qualified_name = c.referenced_qualified_name
               OR ref.name = c.referenced_name
            WITH decorated, c, ref
            ORDER BY
              CASE WHEN ref.qualified_name = c.referenced_qualified_name THEN 0 ELSE 1 END,
              size(ref.qualified_name) ASC
            WITH decorated, c, collect(ref)[0] AS ref
            WHERE ref IS NOT NULL AND decorated <> ref
            MERGE (decorated)-[r:COMPOSES {workspace_id: $workspace_id}]->(ref)
            SET r.resolver = 'decorator-compose-v1',
                r.decorator_name = c.decorator_name,
                r.decorator_key = c.decorator_key
            """,
            compositions=compositions,
            workspace_id=workspace_id,
        )

    def delete_decorator_compositions_for_file(
        self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ):
        """Clear COMPOSES edges originating from symbols in ``file_path``."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)-[r:COMPOSES]->()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def delete_decorators_for_file(self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID):
        """Clear DECORATED_BY / HANDLES edges for symbols defined in a file."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)-[r:DECORATED_BY]->()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)<-[r:HANDLES]-()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def link_instantiations(
        self,
        instantiations: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create INSTANTIATES edges: caller symbol -> the project class it constructs.

        A construction (literal ``X(...)`` or ``v(...)`` for a ``type[X]``-typed
        local) is a static AST fact, so this is a derived edge — a refinement of a
        call where the callee is a class. The class is matched in-graph by qualified
        name (exact, else trailing-name segment, shortest-qn wins) and **must be a
        class** (kind filter); names resolving to a function or to no in-graph symbol
        produce no edge. Gives ``factory_surface`` an explicit construction signal
        distinct from a plain caller / the ``type_fan_out(return)`` heuristic.
        """
        if not instantiations:
            return
        with self.driver.session() as session:
            session.execute_write(
                self._create_instantiation_relations, instantiations, workspace_id
            )
            _bump_workspace_graph_version(session, workspace_id)

    @staticmethod
    def _create_instantiation_relations(tx, instantiations, workspace_id):
        if not instantiations:
            return
        # Split parser rows into internal (in-workspace target) and external
        # (upstream library target) groups. The parser has already proven
        # externality via the file's imports table; the linker just routes.
        internal_rows = [d for d in instantiations if not d.get("is_external")]
        external_rows = [d for d in instantiations if d.get("is_external")]

        if internal_rows:
            tx.run(
                """
                UNWIND $instantiations AS d
                MATCH (caller:Symbol {uid: d.caller_uid})
                MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(cls:Symbol)
                WHERE (cls.qualified_name = d.type_qualified_name OR cls.name = d.type_name)
                  AND cls.kind IN ['class', 'interface', 'struct', 'enum']
                WITH caller, d, cls
                ORDER BY
                  CASE WHEN cls.qualified_name = d.type_qualified_name THEN 0 ELSE 1 END,
                  size(cls.qualified_name) ASC,
                  cls.uid ASC
                WITH caller, d, collect(cls)[0] AS cls
                WHERE cls IS NOT NULL AND caller <> cls
                MERGE (caller)-[r:INSTANTIATES {workspace_id: $workspace_id}]->(cls)
                SET r.resolver = 'instantiate-v1',
                    r.type_name = d.type_name
                """,
                instantiations=internal_rows,
                workspace_id=workspace_id,
            )

        if external_rows:
            from context_engine.indexer.external_boundary import external_symbol_uid

            external_payload: list[dict] = []
            for row in external_rows:
                qn = str(row.get("type_qualified_name") or "")
                caller_uid = str(row.get("caller_uid") or "")
                if not qn or not caller_uid:
                    continue
                module, _, name = qn.rpartition(".")
                external_payload.append(
                    {
                        "caller_uid": caller_uid,
                        "type_name": str(row.get("type_name") or ""),
                        "type_qualified_name": qn,
                        "type_module": module,
                        "type_short_name": name or qn,
                        "type_external_uid": external_symbol_uid(workspace_id, qn),
                    }
                )

            if external_payload:
                tx.run(
                    """
                    UNWIND $rows AS d
                    MATCH (caller:Symbol {uid: d.caller_uid})
                    MERGE (e:ExternalSymbol {
                        uid: d.type_external_uid,
                        workspace_id: $workspace_id
                    })
                    ON CREATE SET e.qualified_name = d.type_qualified_name,
                        e.module = d.type_module,
                        e.name = d.type_short_name,
                        e.is_external = true,
                        e.resolver = 'instantiate-external-v1-derived'
                    MERGE (caller)-[r:INSTANTIATES_EXTERNAL {workspace_id: $workspace_id}]->(e)
                    SET r.resolver = 'instantiate-external-v1',
                        r.type_name = d.type_name
                    """,
                    rows=external_payload,
                    workspace_id=workspace_id,
                )

    def delete_instantiations_for_file(
        self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID
    ):
        """Clear INSTANTIATES and INSTANTIATES_EXTERNAL edges from a file's symbols."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)
                    -[r:INSTANTIATES|INSTANTIATES_EXTERNAL]->()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )

    def link_injections(
        self,
        injections: list[dict],
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ):
        """Create INJECTS edges: owner symbol -> the provider wired into its parameters.

        ``def f(x = Marker(provider))`` is a static binding (like an import), so this is
        a derived edge. The provider is matched to an in-graph symbol by qualified name
        (exact, else trailing-name segment, shortest-qn wins). Providers resolving to no
        in-graph symbol (locals/literals/external) produce no edge — project providers
        only, precision over recall.
        """
        if not injections:
            return
        with self.driver.session() as session:
            session.execute_write(self._create_injection_relations, injections, workspace_id)
            _bump_workspace_graph_version(session, workspace_id)

    @staticmethod
    def _create_injection_relations(tx, injections, workspace_id):
        if not injections:
            return
        tx.run(
            """
            UNWIND $injections AS d
            MATCH (owner:Symbol {uid: d.owner_uid})
            MATCH (:File {workspace_id: $workspace_id})-[:CONTAINS]->(prov:Symbol)
            WHERE (prov.qualified_name = d.provider_qualified_name
                   OR prov.name = d.provider_name)
              AND prov.kind IN ['function', 'method', 'class']
            WITH owner, d, prov
            ORDER BY
              CASE WHEN prov.qualified_name = d.provider_qualified_name THEN 0 ELSE 1 END,
              size(prov.qualified_name) ASC
            WITH owner, d, collect(prov)[0] AS prov
            WHERE prov IS NOT NULL AND owner <> prov
            MERGE (owner)-[r:INJECTS {workspace_id: $workspace_id}]->(prov)
            SET r.resolver = 'inject-v1',
                r.provider_name = d.provider_name,
                r.confidence = 0.85
            """,
            injections=injections,
            workspace_id=workspace_id,
        )

    def delete_injections_for_file(self, file_path: str, workspace_id: str = DEFAULT_WORKSPACE_ID):
        """Clear INJECTS edges from a file's symbols before relinking."""
        with self.driver.session() as session:
            session.run(
                """
                MATCH (f:File {path: $path, workspace_id: $workspace_id})-[:CONTAINS]->(s:Symbol)-[r:INJECTS]->()
                WHERE coalesce(r.workspace_id, $workspace_id) = $workspace_id
                DELETE r
                """,
                path=file_path,
                workspace_id=workspace_id,
            )
