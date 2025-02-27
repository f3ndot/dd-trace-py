import consul

from ddtrace import config
from ddtrace.vendor.wrapt import wrap_function_wrapper as _w

from ...constants import ANALYTICS_SAMPLE_RATE_KEY
from ...constants import SPAN_MEASURED_KEY
from ...ext import SpanTypes
from ...ext import consul as consulx
from ...internal.utils import get_argument_value
from ...internal.utils.wrappers import unwrap as _u
from ...pin import Pin


_KV_FUNCS = ["put", "get", "delete"]


def patch():
    if getattr(consul, "__datadog_patch", False):
        return
    setattr(consul, "__datadog_patch", True)

    pin = Pin(service=consulx.SERVICE)
    pin.onto(consul.Consul.KV)

    for f_name in _KV_FUNCS:
        _w("consul", "Consul.KV.%s" % f_name, wrap_function(f_name))


def unpatch():
    if not getattr(consul, "__datadog_patch", False):
        return
    setattr(consul, "__datadog_patch", False)

    for f_name in _KV_FUNCS:
        _u(consul.Consul.KV, f_name)


def wrap_function(name):
    def trace_func(wrapped, instance, args, kwargs):
        pin = Pin.get_from(instance)
        if not pin or not pin.enabled():
            return wrapped(*args, **kwargs)

        # Only patch the synchronous implementation
        if not isinstance(instance.agent.http, consul.std.HTTPClient):
            return wrapped(*args, **kwargs)

        path = get_argument_value(args, kwargs, 0, "key")
        resource = name.upper()

        with pin.tracer.trace(consulx.CMD, service=pin.service, resource=resource, span_type=SpanTypes.HTTP) as span:
            # set component tag equal to name of integration
            span.set_tag_str("component", config.consul.integration_name)

            span.set_tag(SPAN_MEASURED_KEY)
            rate = config.consul.get_analytics_sample_rate()
            if rate is not None:
                span.set_tag(ANALYTICS_SAMPLE_RATE_KEY, rate)
            span.set_tag_str(consulx.KEY, path)
            span.set_tag_str(consulx.CMD, resource)
            return wrapped(*args, **kwargs)

    return trace_func
