import asyncio
import sys

from fastapi.testclient import TestClient
import httpx
import pytest

import ddtrace
from ddtrace import config
from ddtrace.constants import ERROR_MSG
from ddtrace.contrib.fastapi import patch as fastapi_patch
from ddtrace.contrib.fastapi import unpatch as fastapi_unpatch
from ddtrace.contrib.starlette.patch import patch as patch_starlette
from ddtrace.contrib.starlette.patch import unpatch as unpatch_starlette
from ddtrace.propagation import http as http_propagation
from tests.utils import DummyTracer
from tests.utils import TracerSpanContainer
from tests.utils import override_config
from tests.utils import override_http_config
from tests.utils import snapshot

from . import app


@pytest.fixture
def tracer():
    original_tracer = ddtrace.tracer
    tracer = DummyTracer()
    if sys.version_info < (3, 7):
        # enable legacy asyncio support
        from ddtrace.contrib.asyncio.provider import AsyncioContextProvider

        tracer.configure(context_provider=AsyncioContextProvider())

    setattr(ddtrace, "tracer", tracer)
    fastapi_patch()
    yield tracer
    setattr(ddtrace, "tracer", original_tracer)
    fastapi_unpatch()


@pytest.fixture
def test_spans(tracer):
    container = TracerSpanContainer(tracer)
    yield container
    container.reset()


@pytest.fixture
def application(tracer):
    application = app.get_app()
    yield application


@pytest.fixture
def client(tracer):
    with TestClient(app.get_app()) as test_client:
        yield test_client


@pytest.fixture
def snapshot_app():
    fastapi_patch()
    application = app.get_app()
    yield application
    fastapi_unpatch()


@pytest.fixture
def snapshot_client(snapshot_app):
    with TestClient(snapshot_app) as test_client:
        yield test_client


def assert_serialize_span(serialize_span):
    assert serialize_span.service == "fastapi"
    assert serialize_span.name == "fastapi.serialize_response"
    assert serialize_span.error == 0


def test_read_homepage(client, tracer, test_spans):
    response = client.get("/", headers={"sleep": "False"})
    assert response.status_code == 200
    assert response.json() == {"Homepage Read": "Success"}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 2
    request_span, serialize_span = spans[0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/"
    assert request_span.get_tag("http.status_code") == "200"
    assert request_span.get_tag("http.query.string") is None
    assert request_span.get_tag("component") == "fastapi"

    assert serialize_span.service == "fastapi"
    assert serialize_span.name == "fastapi.serialize_response"
    assert serialize_span.error == 0


def test_read_item_success(client, tracer, test_spans):
    response = client.get("/items/foo", headers={"X-Token": "DataDog"})
    assert response.status_code == 200
    assert response.json() == {"id": "foo", "name": "Foo", "description": "This item's description is foo."}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 2
    request_span, serialize_span = spans[0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /items/{item_id}"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/items/foo"
    assert request_span.get_tag("http.status_code") == "200"
    assert request_span.get_tag("component") == "fastapi"

    assert_serialize_span(serialize_span)


def test_read_item_bad_token(client, tracer, test_spans):
    response = client.get("/items/bar", headers={"X-Token": "DataDoge"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid X-Token header"}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 1
    request_span = spans[0][0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /items/{item_id}"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/items/bar"
    assert request_span.get_tag("http.status_code") == "401"
    assert request_span.get_tag("component") == "fastapi"


def test_read_item_nonexistent_item(client, tracer, test_spans):
    response = client.get("/items/foobar", headers={"X-Token": "DataDog"})
    assert response.status_code == 404
    assert response.json() == {"detail": "Item not found"}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 1
    request_span = spans[0][0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /items/{item_id}"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/items/foobar"
    assert request_span.get_tag("http.status_code") == "404"
    assert request_span.get_tag("component") == "fastapi"


def test_read_item_query_string(client, tracer, test_spans):
    with override_http_config("fastapi", dict(trace_query_string=True)):
        response = client.get("/items/foo?q=query", headers={"X-Token": "DataDog"})

    assert response.status_code == 200
    assert response.json() == {"id": "foo", "name": "Foo", "description": "This item's description is foo."}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 2
    request_span, serialize_span = spans[0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /items/{item_id}"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/items/foo?q=query"
    assert request_span.get_tag("http.status_code") == "200"
    assert request_span.get_tag("http.query.string") == "q=query"
    assert request_span.get_tag("component") == "fastapi"

    assert_serialize_span(serialize_span)


def test_200_multi_query_string(client, tracer, test_spans):
    with override_http_config("fastapi", dict(trace_query_string=True)):
        r = client.get("/items/foo?name=Foo&q=query", headers={"X-Token": "DataDog"})

    assert r.status_code == 200
    assert r.json() == {"id": "foo", "name": "Foo", "description": "This item's description is foo."}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 2
    request_span, serialize_span = spans[0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /items/{item_id}"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/items/foo?name=Foo&q=query"
    assert request_span.get_tag("http.status_code") == "200"
    assert request_span.get_tag("http.query.string") == "name=Foo&q=query"
    assert request_span.get_tag("component") == "fastapi"

    assert_serialize_span(serialize_span)


def test_create_item_success(client, tracer, test_spans):
    response = client.post(
        "/items/",
        headers={"X-Token": "DataDog"},
        json={"id": "foobar", "name": "Foo Bar", "description": "The Foo Bartenders"},
    )
    assert response.status_code == 200
    assert response.json() == {"id": "foobar", "name": "Foo Bar", "description": "The Foo Bartenders"}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 2
    request_span, serialize_span = spans[0]

    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "POST /items/"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "POST"
    assert request_span.get_tag("http.url") == "http://testserver/items/"
    assert request_span.get_tag("http.status_code") == "200"
    assert request_span.get_tag("http.query.string") is None
    assert request_span.get_tag("component") == "fastapi"

    assert_serialize_span(serialize_span)


def test_create_item_bad_token(client, tracer, test_spans):
    response = client.post(
        "/items/",
        headers={"X-Token": "DataDoged"},
        json={"id": "foobar", "name": "Foo Bar", "description": "The Foo Bartenders"},
    )
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid X-Token header"}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 1
    request_span = spans[0][0]

    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "POST /items/"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "POST"
    assert request_span.get_tag("http.url") == "http://testserver/items/"
    assert request_span.get_tag("http.status_code") == "401"
    assert request_span.get_tag("http.query.string") is None
    assert request_span.get_tag("component") == "fastapi"


def test_create_item_duplicate_item(client, tracer, test_spans):
    response = client.post(
        "/items/",
        headers={"X-Token": "DataDog"},
        json={"id": "foo", "name": "Foo", "description": "Duplicate Foo Item"},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Item already exists"}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 1
    request_span = spans[0][0]

    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "POST /items/"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "POST"
    assert request_span.get_tag("http.url") == "http://testserver/items/"
    assert request_span.get_tag("http.status_code") == "400"
    assert request_span.get_tag("http.query.string") is None
    assert request_span.get_tag("component") == "fastapi"


def test_invalid_path(client, tracer, test_spans):
    response = client.get("/invalid_path")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 1
    request_span = spans[0][0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /invalid_path"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/invalid_path"
    assert request_span.get_tag("http.status_code") == "404"
    assert request_span.get_tag("component") == "fastapi"


def test_500_error_raised(client, tracer, test_spans):
    with pytest.raises(RuntimeError):
        client.get("/500", headers={"X-Token": "DataDog"})
    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 1

    request_span = spans[0][0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /500"
    assert request_span.error == 1
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/500"
    assert request_span.get_tag("http.status_code") == "500"
    assert request_span.get_tag(ERROR_MSG) == "Server error"
    assert request_span.get_tag("error.type") == "builtins.RuntimeError"
    assert request_span.get_tag("component") == "fastapi"
    assert 'raise RuntimeError("Server error")' in request_span.get_tag("error.stack")


def test_streaming_response(client, tracer, test_spans):
    response = client.get("/stream")
    assert response.status_code == 200
    assert response.text.endswith("streaming")

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 1
    request_span = spans[0][0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /stream"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/stream"
    assert request_span.get_tag("http.query.string") is None
    assert request_span.get_tag("http.status_code") == "200"
    assert request_span.get_tag("component") == "fastapi"


def test_file_response(client, tracer, test_spans):
    response = client.get("/file", headers={"X-Token": "DataDog"})
    assert response.status_code == 200
    assert response.text == "Datadog says hello!"

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 1
    request_span = spans[0][0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /file"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/file"
    assert request_span.get_tag("http.query.string") is None
    assert request_span.get_tag("http.status_code") == "200"
    assert request_span.get_tag("component") == "fastapi"


def test_path_param_aggregate(client, tracer, test_spans):
    response = client.get("/users/testUserID", headers={"X-Token": "DataDog"})
    assert response.status_code == 200
    assert response.json() == {"userid": "testUserID", "name": "Test User"}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 2
    request_span, serialize_span = spans[0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /users/{userid:str}"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/users/testUserID"
    assert request_span.get_tag("http.status_code") == "200"
    assert request_span.get_tag("component") == "fastapi"

    assert_serialize_span(serialize_span)


def test_mid_path_param_aggregate(client, tracer, test_spans):
    r = client.get("/users/testUserID/info", headers={"X-Token": "DataDog"})

    assert r.status_code == 200
    assert r.json() == {"User Info": "Here"}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 2
    request_span, serialize_span = spans[0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /users/{userid:str}/info"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/users/testUserID/info"
    assert request_span.get_tag("http.status_code") == "200"
    assert request_span.get_tag("component") == "fastapi"

    assert_serialize_span(serialize_span)


def test_multi_path_param_aggregate(client, tracer, test_spans):
    response = client.get("/users/testUserID/name", headers={"X-Token": "DataDog"})

    assert response.status_code == 200
    assert response.json() == {"User Attribute": "Test User"}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 2
    request_span, serialize_span = spans[0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /users/{userid:str}/{attribute:str}"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/users/testUserID/name"
    assert request_span.get_tag("http.status_code") == "200"
    assert request_span.get_tag("component") == "fastapi"

    assert_serialize_span(serialize_span)


def test_distributed_tracing(client, tracer, test_spans):
    headers = [
        (http_propagation.HTTP_HEADER_PARENT_ID, "5555"),
        (http_propagation.HTTP_HEADER_TRACE_ID, "9999"),
        ("sleep", "False"),
    ]
    response = client.get("http://testserver/", headers=dict(headers))

    assert response.status_code == 200
    assert response.json() == {"Homepage Read": "Success"}

    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 2
    request_span, serialize_span = spans[0]
    assert request_span.service == "fastapi"
    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /"
    assert request_span.error == 0
    assert request_span.get_tag("http.method") == "GET"
    assert request_span.get_tag("http.url") == "http://testserver/"
    assert request_span.get_tag("http.status_code") == "200"
    assert request_span.parent_id == 5555
    assert request_span.trace_id == 9999
    assert request_span.get_tag("component") == "fastapi"

    assert_serialize_span(serialize_span)


@pytest.mark.asyncio
async def test_multiple_requests(application, tracer, test_spans):
    with override_http_config("fastapi", dict(trace_query_string=True)):
        async with httpx.AsyncClient(app=application) as client:
            responses = await asyncio.gather(
                client.get("http://testserver/", headers={"sleep": "True"}),
                client.get("http://testserver/", headers={"sleep": "False"}),
            )

    assert len(responses) == 2
    assert [r.status_code for r in responses] == [200] * 2
    assert [r.json() for r in responses] == [{"Homepage Read": "Sleep"}, {"Homepage Read": "Success"}]

    spans = test_spans.pop_traces()
    assert len(spans) == 2
    assert len(spans[0]) == 2
    assert len(spans[1]) == 2

    r1_span, s1_span = spans[0]
    assert r1_span.service == "fastapi"
    assert r1_span.name == "fastapi.request"
    assert r1_span.resource == "GET /"
    assert r1_span.get_tag("http.method") == "GET"
    assert r1_span.get_tag("http.url") == "http://testserver/"

    assert_serialize_span(s1_span)

    r2_span, s2_span = spans[1]
    assert r2_span.service == "fastapi"
    assert r2_span.name == "fastapi.request"
    assert r2_span.resource == "GET /"
    assert r2_span.get_tag("http.method") == "GET"
    assert r2_span.get_tag("http.url") == "http://testserver/"
    assert r1_span.trace_id != r2_span.trace_id

    assert_serialize_span(s2_span)


def test_service_can_be_overridden(client, tracer, test_spans):
    with override_config("fastapi", dict(service_name="test-override-service")):
        response = client.get("/", headers={"sleep": "False"})
        assert response.status_code == 200

    spans = test_spans.pop_traces()
    assert len(spans) > 0

    span = spans[0][0]
    assert span.service == "test-override-service"


def test_w_patch_starlette(client, tracer, test_spans):
    patch_starlette()
    try:
        response = client.get("/file", headers={"X-Token": "DataDog"})
        assert response.status_code == 200
        assert response.text == "Datadog says hello!"

        spans = test_spans.pop_traces()
        assert len(spans) == 1
        assert len(spans[0]) == 1
        request_span = spans[0][0]
        assert request_span.service == "fastapi"
        assert request_span.name == "fastapi.request"
        assert request_span.resource == "GET /file"
        assert request_span.error == 0
        assert request_span.get_tag("http.method") == "GET"
        assert request_span.get_tag("http.url") == "http://testserver/file"
        assert request_span.get_tag("http.query.string") is None
        assert request_span.get_tag("http.status_code") == "200"
        assert request_span.get_tag("component") == "fastapi"
    finally:
        unpatch_starlette()


@snapshot()
def test_subapp_snapshot(snapshot_client):
    response = snapshot_client.get("/sub-app/hello/name")
    assert response.status_code == 200


@snapshot()
def test_subapp_no_aggregate_snapshot(snapshot_client):
    config.fastapi["aggregate_resources"] = False
    response = snapshot_client.get("/sub-app/hello/name")
    assert response.status_code == 200
    config.fastapi["aggregate_resources"] = True


@snapshot(token_override="tests.contrib.fastapi.test_fastapi.test_subapp_snapshot")
def test_subapp_w_starlette_patch_snapshot(snapshot_client):
    # Test that patching starlette doesn't affect the spans generated
    patch_starlette()
    try:
        response = snapshot_client.get("/sub-app/hello/name")
        assert response.status_code == 200
    finally:
        unpatch_starlette()


@snapshot()
def test_table_query_snapshot(snapshot_client):
    r_post = snapshot_client.post(
        "/items/",
        headers={"X-Token": "DataDog"},
        json={"id": "test_id", "name": "Test Name", "description": "This request adds a new entry to the test db"},
    )
    assert r_post.status_code == 200
    assert r_post.json() == {
        "id": "test_id",
        "name": "Test Name",
        "description": "This request adds a new entry to the test db",
    }

    r_get = snapshot_client.get("/items/test_id", headers={"X-Token": "DataDog"})
    assert r_get.status_code == 200
    assert r_get.json() == {
        "id": "test_id",
        "name": "Test Name",
        "description": "This request adds a new entry to the test db",
    }


def test_background_task(client, tracer, test_spans):
    """Tests if background tasks have been excluded from span duration"""
    response = client.get("/asynctask")
    assert response.status_code == 200
    assert response.json() == "task added"
    spans = test_spans.pop_traces()
    assert len(spans) == 1
    assert len(spans[0]) == 2
    request_span, serialize_span = spans[0]

    assert request_span.name == "fastapi.request"
    assert request_span.resource == "GET /asynctask"
    # typical duration without background task should be in less than 10 ms
    # duration with background task will take approximately 1.1s
    assert request_span.duration < 1


@pytest.mark.parametrize("host", ["hostserver", "hostserver:5454"])
def test_host_header(client, tracer, test_spans, host):
    """Tests if background tasks have been excluded from span duration"""
    r = client.get("/asynctask", headers={"host": host})
    assert r.status_code == 200

    assert test_spans.spans
    request_span = test_spans.spans[0]
    assert request_span.get_tag("http.url") == "http://%s/asynctask" % (host,)
