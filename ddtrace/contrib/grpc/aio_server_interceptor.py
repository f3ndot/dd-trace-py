import inspect
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Iterable
from typing import Union

import grpc
from grpc import aio
from grpc.aio._typing import RequestIterableType
from grpc.aio._typing import RequestType
from grpc.aio._typing import ResponseIterableType
from grpc.aio._typing import ResponseType

from ddtrace import Pin
from ddtrace import Span
from ddtrace import config
from ddtrace.vendor import wrapt

from .. import trace_utils
from ...constants import ANALYTICS_SAMPLE_RATE_KEY
from ...constants import ERROR_MSG
from ...constants import ERROR_TYPE
from ...constants import SPAN_MEASURED_KEY
from ...ext import SpanTypes
from ...internal.compat import to_unicode
from ..grpc import constants
from ..grpc.utils import set_grpc_method_meta


Continuation = Callable[[grpc.HandlerCallDetails], Awaitable[grpc.RpcMethodHandler]]


# Used to get a status code from integer
# as `grpc._cython.cygrpc._ServicerContext.code()` returns an integer.
_INT2CODE = {s.value[0]: s for s in grpc.StatusCode}


def _is_coroutine_rpc_method_handler(handler):
    # type: (grpc.RpcMethodHandler) -> bool
    if not handler.request_streaming and not handler.response_streaming:
        return inspect.iscoroutinefunction(handler.unary_unary)
    elif not handler.request_streaming and handler.response_streaming:
        return inspect.isasyncgenfunction(handler.unary_stream)
    elif handler.request_streaming and not handler.response_streaming:
        return inspect.iscoroutinefunction(handler.stream_unary)
    else:
        return inspect.isasyncgenfunction(handler.stream_stream)


def create_aio_server_interceptor(pin):
    # type: (Pin) -> _ServerInterceptor
    async def interceptor_function(
        continuation,  # type: Continuation
        handler_call_details,  # type: grpc.HandlerCallDetails
    ):
        # type: (...) -> Union[grpc.RpcMethodHandler, _TracedAioRpcMethodHandler, _TracedRpcMethodHandler, None]
        rpc_method_handler = await continuation(handler_call_details)

        # continuation returns an RpcMethodHandler instance if the RPC is
        # considered serviced, or None otherwise
        # https://grpc.github.io/grpc/python/grpc.html#grpc.ServerInterceptor.intercept_service

        if rpc_method_handler is None:
            return None

        if _is_coroutine_rpc_method_handler(rpc_method_handler):
            return _TracedAioRpcMethodHandler(pin, handler_call_details, rpc_method_handler)
        else:
            return _TracedRpcMethodHandler(pin, handler_call_details, rpc_method_handler)

    return _ServerInterceptor(interceptor_function)


def _handle_server_exception(
    servicer_context,  # type: Union[None, grpc.ServicerContext]
    span,  # type: Span
):
    # type: (...) -> None
    span.error = 1
    if servicer_context is None:
        return
    if hasattr(servicer_context, "details"):
        span.set_tag_str(ERROR_MSG, to_unicode(servicer_context.details()))
    if hasattr(servicer_context, "code") and servicer_context.code() != 0 and servicer_context.code() in _INT2CODE:
        span.set_tag_str(ERROR_TYPE, to_unicode(_INT2CODE[servicer_context.code()]))


async def _wrap_aio_stream_response(
    behavior,  # type: Callable[[Union[RequestIterableType, RequestType], aio.ServicerContext], ResponseIterableType]
    request_or_iterator,  # type: Union[RequestIterableType, RequestType]
    servicer_context,  # type: aio.ServicerContext
    span,  # type: Span
):
    # type: (...) -> ResponseIterableType
    try:
        call = behavior(request_or_iterator, servicer_context)
        async for response in call:
            yield response
    except Exception:
        span.set_traceback()
        _handle_server_exception(servicer_context, span)
        raise
    finally:
        span.finish()


async def _wrap_aio_unary_response(
    behavior,  # type: Callable[[Union[RequestIterableType, RequestType], aio.ServicerContext], Awaitable[ResponseType]]
    request_or_iterator,  # type: Union[RequestIterableType, RequestType]
    servicer_context,  # type: aio.ServicerContext
    span,  # type: Span
):
    # type: (...) -> ResponseType
    try:
        return await behavior(request_or_iterator, servicer_context)
    except Exception:
        span.set_traceback()
        _handle_server_exception(servicer_context, span)
        raise
    finally:
        span.finish()


def _wrap_stream_response(
    behavior,  # type: Callable[[Any, grpc.ServicerContext], Iterable[Any]]
    request_or_iterator,  # type: Any
    servicer_context,  # type: grpc.ServicerContext
    span,  # type: Span
):
    # type: (...) -> Iterable[Any]
    try:
        for response in behavior(request_or_iterator, servicer_context):
            yield response
    except Exception:
        span.set_traceback()
        _handle_server_exception(servicer_context, span)
        raise
    finally:
        span.finish()


def _wrap_unary_response(
    behavior,  # type: Callable[[Any, grpc.ServicerContext], Any]
    request_or_iterator,  # type: Any
    servicer_context,  # type: grpc.ServicerContext
    span,  # type: Span
):
    # type: (...) -> Any
    try:
        return behavior(request_or_iterator, servicer_context)
    except Exception:
        span.set_traceback()
        _handle_server_exception(servicer_context, span)
        raise
    finally:
        span.finish()


def _create_span(pin, handler_call_details, method_kind):
    # type: (Pin, grpc.HandlerCallDetails, str) -> Span
    tracer = pin.tracer
    headers = dict(handler_call_details.invocation_metadata)

    trace_utils.activate_distributed_headers(tracer, int_config=config.grpc_aio_server, request_headers=headers)

    span = tracer.trace(
        "grpc",
        span_type=SpanTypes.GRPC,
        service=trace_utils.int_service(pin, config.grpc_aio_server),
        resource=handler_call_details.method,
    )

    # set component tag equal to name of integration
    span.set_tag_str("component", config.grpc_aio_server.integration_name)

    span.set_tag(SPAN_MEASURED_KEY)

    set_grpc_method_meta(span, handler_call_details.method, method_kind)
    span.set_tag_str(constants.GRPC_SPAN_KIND_KEY, constants.GRPC_SPAN_KIND_VALUE_SERVER)

    sample_rate = config.grpc_aio_server.get_analytics_sample_rate()
    if sample_rate is not None:
        span.set_tag(ANALYTICS_SAMPLE_RATE_KEY, sample_rate)

    if pin.tags:
        span.set_tags(pin.tags)

    return span


class _TracedAioRpcMethodHandler(wrapt.ObjectProxy):
    def __init__(self, pin, handler_call_details, wrapped):
        # type: (Pin, grpc.HandlerCallDetails, grpc.RpcMethodHandler) -> None
        super(_TracedAioRpcMethodHandler, self).__init__(wrapped)
        self._pin = pin
        self._handler_call_details = handler_call_details

    async def unary_unary(self, request, context):
        # type: (RequestType, aio.ServicerContext) -> ResponseType
        span = _create_span(self._pin, self._handler_call_details, constants.GRPC_METHOD_KIND_UNARY)
        return await _wrap_aio_unary_response(self.__wrapped__.unary_unary, request, context, span)

    async def unary_stream(self, request, context):
        # type: (RequestType, aio.ServicerContext) -> ResponseIterableType
        span = _create_span(self._pin, self._handler_call_details, constants.GRPC_METHOD_KIND_SERVER_STREAMING)
        async for response in _wrap_aio_stream_response(self.__wrapped__.unary_stream, request, context, span):
            yield response

    async def stream_unary(self, request_iterator, context):
        # type: (RequestIterableType, aio.ServicerContext) -> ResponseType
        span = _create_span(self._pin, self._handler_call_details, constants.GRPC_METHOD_KIND_CLIENT_STREAMING)
        return await _wrap_aio_unary_response(self.__wrapped__.stream_unary, request_iterator, context, span)

    async def stream_stream(self, request_iterator, context):
        # type: (RequestIterableType, aio.ServicerContext) -> ResponseIterableType
        span = _create_span(self._pin, self._handler_call_details, constants.GRPC_METHOD_KIND_BIDI_STREAMING)
        async for response in _wrap_aio_stream_response(
            self.__wrapped__.stream_stream, request_iterator, context, span
        ):
            yield response


class _TracedRpcMethodHandler(wrapt.ObjectProxy):
    def __init__(self, pin, handler_call_details, wrapped):
        # type: (Pin, grpc.HandlerCallDetails, grpc.RpcMethodHandler) -> None
        super(_TracedRpcMethodHandler, self).__init__(wrapped)
        self._pin = pin
        self._handler_call_details = handler_call_details

    def unary_unary(self, request, context):
        # type: (Any, grpc.ServicerContext) -> Any
        span = _create_span(self._pin, self._handler_call_details, constants.GRPC_METHOD_KIND_UNARY)
        return _wrap_unary_response(self.__wrapped__.unary_unary, request, context, span)

    def unary_stream(self, request, context):
        # type: (Any, grpc.ServicerContext) -> Iterable[Any]
        span = _create_span(self._pin, self._handler_call_details, constants.GRPC_METHOD_KIND_SERVER_STREAMING)
        for response in _wrap_stream_response(self.__wrapped__.unary_stream, request, context, span):
            yield response

    def stream_unary(self, request_iterator, context):
        # type: (Iterable[Any], grpc.ServicerContext) -> Any
        span = _create_span(self._pin, self._handler_call_details, constants.GRPC_METHOD_KIND_CLIENT_STREAMING)
        return _wrap_unary_response(self.__wrapped__.stream_unary, request_iterator, context, span)

    def stream_stream(self, request_iterator, context):
        # type: (Iterable[Any], grpc.ServicerContext) -> Iterable[Any]
        span = _create_span(self._pin, self._handler_call_details, constants.GRPC_METHOD_KIND_BIDI_STREAMING)
        for response in _wrap_stream_response(self.__wrapped__.stream_stream, request_iterator, context, span):
            yield response


class _ServerInterceptor(aio.ServerInterceptor):
    def __init__(self, interceptor_function):
        self._fn = interceptor_function

    async def intercept_service(
        self,
        continuation,  # type: Continuation
        handler_call_details,  # type: grpc.HandlerCallDetails
    ):
        # type: (...) -> Union[grpc.RpcMethodHandler, _TracedAioRpcMethodHandler]
        return await self._fn(continuation, handler_call_details)
