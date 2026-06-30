from typing import Any

from neo4j import GraphDatabase

from context_engine.database.neo4j._common import _import_row as _import_row
from context_engine.database.neo4j.edges_calls import CallImportEdgesMixin
from context_engine.database.neo4j.edges_dynamic import DynamicEdgesMixin
from context_engine.database.neo4j.edges_structural import StructuralEdgesMixin
from context_engine.database.neo4j.impact import ImpactMixin
from context_engine.database.neo4j.workspace import WorkspaceMixin


class Neo4jClient(
    WorkspaceMixin,
    CallImportEdgesMixin,
    StructuralEdgesMixin,
    DynamicEdgesMixin,
    ImpactMixin,
):
    """Neo4j-backed graph client. Edge-type methods live in the mixins above;
    this facade owns the driver lifecycle and composes them into one surface."""

    def __init__(
        self,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
        *,
        driver: Any | None = None,
    ):
        if driver is not None:
            self.driver = driver
            self._owns_driver = False
        else:
            if uri is None or user is None or password is None:
                raise ValueError("uri, user, and password are required when driver is omitted")
            self.driver = GraphDatabase.driver(uri, auth=(user, password))
            self._owns_driver = True

    def close(self):
        if self._owns_driver:
            self.driver.close()
