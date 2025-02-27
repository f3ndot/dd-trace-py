import sys

from ddtrace import config

from .constants import FUTURE_SPAN_KEY


def _finish_span(future):
    """
    Finish the span if it's attached to the given ``Future`` object.
    This method is a Tornado callback used to close a decorated function
    executed as a coroutine or as a synchronous function in another thread.
    """
    span = getattr(future, FUTURE_SPAN_KEY, None)

    if span:
        # `tornado.concurrent.Future` in PY3 tornado>=4.0,<5 has `exc_info`
        if callable(getattr(future, "exc_info", None)):
            # retrieve the exception from the coroutine object
            exc_info = future.exc_info()
            if exc_info:
                span.set_exc_info(*exc_info)
        elif callable(getattr(future, "exception", None)):
            # in tornado>=4.0,<5 with PY2 `concurrent.futures._base.Future`
            # `exception_info()` returns `(exception, traceback)` but
            # `exception()` only returns the first element in the tuple
            if callable(getattr(future, "exception_info", None)):
                exc, exc_tb = future.exception_info()
                if exc and exc_tb:
                    exc_type = type(exc)
                    span.set_exc_info(exc_type, exc, exc_tb)
            # in tornado>=5 with PY3, `tornado.concurrent.Future` is alias to
            # `asyncio.Future` in PY3 `exc_info` not available, instead use
            # exception method
            else:
                exc = future.exception()
                if exc:
                    # we expect exception object to have a traceback attached
                    if hasattr(exc, "__traceback__"):
                        exc_type = type(exc)
                        exc_tb = getattr(exc, "__traceback__", None)
                        span.set_exc_info(exc_type, exc, exc_tb)
                    # if all else fails use currently handled exception for
                    # current thread
                    else:
                        span.set_exc_info(*sys.exc_info())

        span.finish()


def wrap_executor(tracer, fn, args, kwargs, span_name, service=None, resource=None, span_type=None):
    """
    Wrap executor function used to change the default behavior of
    ``Tracer.wrap()`` method. A decorated Tornado function can be
    a regular function or a coroutine; if a coroutine is decorated, a
    span is attached to the returned ``Future`` and a callback is set
    so that it will close the span when the ``Future`` is done.
    """
    span = tracer.trace(span_name, service=service, resource=resource, span_type=span_type)

    # set component tag equal to name of integration
    span.set_tag_str("component", config.tornado.integration_name)

    # catch standard exceptions raised in synchronous executions
    try:
        future = fn(*args, **kwargs)

        # duck-typing: if it has `add_done_callback` it's a Future
        # object whatever is the underlying implementation
        if callable(getattr(future, "add_done_callback", None)):
            setattr(future, FUTURE_SPAN_KEY, span)
            future.add_done_callback(_finish_span)
        else:
            # we don't have a future so the `future` variable
            # holds the result of the function
            span.finish()
    except Exception:
        span.set_traceback()
        span.finish()
        raise

    return future
