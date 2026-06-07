# Library marker catalogue — structural evidence report

- total entries: 27
- by status: `{"absent": 22, "structurally_backed": 2, "unproven": 3}`

## By kind

- **error_dispatch**: `{"absent": 2}`
- **proxy_object**: `{"absent": 3}`
- **signal_register**: `{"absent": 5}`
- **task_register**: `{"absent": 4, "structurally_backed": 1}`
- **web_route_register**: `{"absent": 8, "structurally_backed": 1, "unproven": 3}`

## Entries

| canonical_qn | kind | status | workspace | method evidence |
|---|---|---|---|---|
| `aiohttp.web.Application` | web_route_register | **absent** | - | - |
| `aiohttp.web.UrlDispatcher` | web_route_register | **absent** | - | - |
| `arq.connections.ArqRedis` | task_register | **absent** | - | - |
| `blinker.Namespace` | signal_register | **absent** | - | - |
| `blinker.Signal` | signal_register | **absent** | - | - |
| `celery.app.base.Celery` | task_register | **structurally_backed** | qa_repo/celery@axis-v4+axis_python_v1 | send_task(metadata_key_roundtrip) |
| `celery.utils.dispatch.Signal` | signal_register | **absent** | - | - |
| `django.dispatch.Signal` | signal_register | **absent** | - | - |
| `django.dispatch.dispatcher.Signal` | signal_register | **absent** | - | - |
| `dramatiq.Broker` | task_register | **absent** | - | - |
| `fastapi.applications.FastAPI` | web_route_register | **unproven** | qa_repo/fastapi@axis-v4+axis_python_v1 | - |
| `fastapi.routing.APIRouter` | web_route_register | **unproven** | qa_repo/fastapi@axis-v4+axis_python_v1 | - |
| `flask.app.Flask` | web_route_register | **structurally_backed** | qa_repo/flask@axis-v4+axis_python_v1 | add_url_rule(metadata_key_roundtrip) |
| `flask.blueprints.Blueprint` | web_route_register | **unproven** | qa_repo/flask@axis-v4+axis_python_v1 | - |
| `huey.Huey` | task_register | **absent** | - | - |
| `rq.Queue` | task_register | **absent** | - | - |
| `sanic.Sanic` | web_route_register | **absent** | - | - |
| `sanic.blueprints.Blueprint` | web_route_register | **absent** | - | - |
| `starlette.applications.Starlette` | web_route_register | **absent** | - | - |
| `starlette.exceptions.ExceptionMiddleware` | error_dispatch | **absent** | - | - |
| `starlette.middleware.exceptions.ExceptionMiddleware` | error_dispatch | **absent** | - | - |
| `starlette.routing.Mount` | web_route_register | **absent** | - | - |
| `starlette.routing.Route` | web_route_register | **absent** | - | - |
| `starlette.routing.Router` | web_route_register | **absent** | - | - |
| `werkzeug.local.Local` | proxy_object | **absent** | - | - |
| `werkzeug.local.LocalProxy` | proxy_object | **absent** | - | - |
| `werkzeug.local.LocalStack` | proxy_object | **absent** | - | - |
