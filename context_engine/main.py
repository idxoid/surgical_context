"""FastAPI context_engine entrypoint."""

import context_engine.bootstrap as _bootstrap

_bootstrap.apply(caller_file=__file__)

import logging

from context_engine.api.app import create_app
from context_engine.api.config import load_context_engine_config
from context_engine.api.route_services import RouteServices
from context_engine.api.schemas import (
    AskAxisRequest,  # noqa: F401 — re-exported for endpoint tests
    AskRequest,  # noqa: F401 — re-exported for endpoint tests
    FeedbackRequest,  # noqa: F401 — re-exported for endpoint tests
    HistoryAskRecordRequest,  # noqa: F401 — re-exported for endpoint tests
    IndexFileRequest,  # noqa: F401 — re-exported for endpoint tests
    IndexFilesRequest,  # noqa: F401 — re-exported for endpoint tests
    IndexRequest,  # noqa: F401 — re-exported for endpoint tests
    OverlayRequest,  # noqa: F401 — re-exported for endpoint tests
    UnifiedSearchRequest,  # noqa: F401 — re-exported for endpoint tests
)
from context_engine.api.state import SidecarState, build_context_engine_state
from context_engine.indexer.queue import (
    EnqueueResult,  # noqa: F401 — re-exported for endpoint tests
    IndexWorkItem,  # noqa: F401 — re-exported for endpoint tests
)
from context_engine.workspace import (
    DEFAULT_WORKSPACE_ID,  # noqa: F401 — re-exported for endpoint tests
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

state: SidecarState = build_context_engine_state(load_context_engine_config())
route_services = RouteServices(state)

# Service handles re-exported for endpoint tests (shared objects: patching an
# attribute here mutates the same instance the routes reach via route_services).
ai_engine = state.ai_engine
ask_context_builder = state.ask_context_builder
feedback_store = state.feedback_store
indexing_service = state.indexing_service
user_auth = state.user_auth

app = create_app(state, route_services)

from context_engine.api.routes import ask as _ask_routes
from context_engine.api.routes import auth as _auth_routes
from context_engine.api.routes import feedback as _feedback_routes
from context_engine.api.routes import health as _health_routes
from context_engine.api.routes import history as _history_routes
from context_engine.api.routes import impact as _impact_routes
from context_engine.api.routes import indexing as _indexing_routes
from context_engine.api.routes import overlay as _overlay_routes
from context_engine.api.routes import search as _search_routes

ask = _ask_routes.ask  # noqa: F401 — re-exported for endpoint tests
ask_axis = _ask_routes.ask_axis  # noqa: F401
ask_stream = _ask_routes.ask_stream  # noqa: F401
intent = _ask_routes.intent  # noqa: F401
audit_actions = _auth_routes.audit_actions  # noqa: F401
auth_token = _auth_routes.auth_token  # noqa: F401
clear_overlay = _overlay_routes.clear_overlay  # noqa: F401
cloud_status = _auth_routes.cloud_status  # noqa: F401
health = _health_routes.health  # noqa: F401
history_conversation = _history_routes.history_conversation  # noqa: F401
history_conversations = _history_routes.history_conversations  # noqa: F401
history_request_bundle = _history_routes.history_request_bundle  # noqa: F401
impact = _impact_routes.impact  # noqa: F401
index = _indexing_routes.index  # noqa: F401
index_docs_endpoint = _indexing_routes.index_docs_endpoint  # noqa: F401
index_file_endpoint = _indexing_routes.index_file_endpoint  # noqa: F401
index_files_endpoint = _indexing_routes.index_files_endpoint  # noqa: F401
index_git_delta_endpoint = _indexing_routes.index_git_delta_endpoint  # noqa: F401
index_git_delta_status = _indexing_routes.index_git_delta_status  # noqa: F401
index_manifest_endpoint = _indexing_routes.index_manifest_endpoint  # noqa: F401
index_queue_status = _indexing_routes.index_queue_status  # noqa: F401
index_stats = _indexing_routes.index_stats  # noqa: F401
list_users = _auth_routes.list_users  # noqa: F401
metrics = _health_routes.metrics  # noqa: F401
record_feedback = _feedback_routes.record_feedback  # noqa: F401
record_history_ask = _history_routes.record_history_ask  # noqa: F401
search = _search_routes.search  # noqa: F401
unified_search = _search_routes.unified_search  # noqa: F401
update_overlay = _overlay_routes.update_overlay  # noqa: F401


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
