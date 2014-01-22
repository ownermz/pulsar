import sys
import logging
import traceback
from collections import namedtuple

logger = logging.getLogger('pulsar')
async_exec_info = namedtuple('async_exec_info', 'error_class error trace')
log_exc_info = ('error', 'critical')


def is_relevant_tb(tb):
    return not ('__skip_traceback__' in tb.tb_frame.f_locals or
                '__unittest' in tb.tb_frame.f_globals)


def tb_length(tb):
    length = 0
    while tb and is_relevant_tb(tb):
        length += 1
        tb = tb.tb_next
    return length


def format_exception(exctype, value, tb):
    trace = getattr(value, '__async_traceback__', None)
    while tb and not is_relevant_tb(tb):
        tb = tb.tb_next
    length = tb_length(tb)
    if length or not trace:
        tb = traceback.format_exception(exctype, value, tb, length)
    if trace:
        if tb:
            tb = tb[:-1]
            tb.extend(trace[1:])
        else:
            tb = trace
    value.__async_traceback__ = tb
    value.__traceback__ = None
    return tb


if sys.version_info >= (3, 0):

    class _TracebackLogger:
        __slots__ = ['exc', 'tb']

        def __init__(self, exc):
            self.exc = exc
            self.tb = None

        def activate(self):
            exc = self.exc
            if exc is not None:
                self.exc = None
                self.tb = traceback.format_exception(exc.__class__, exc,
                                                     exc.__traceback__)

        def clear(self):
            self.exc = None
            self.tb = None

        def __del__(self):
            if self.tb:
                logger.error('Future/Task exception was never retrieved:\n%s',
                             ''.join(self.tb))

else:

    class _TracebackLogger:
        __slots__ = ['exc', 'tb']

        def __init__(self, exc):
            self.exc = exc
            self.tb = format_exception(*sys.exc_info())

        def activate(self):
            self.exc = None

        def clear(self):
            self.exc = None
            self.tb = None

        def __del__(self):
            if self.tb and self.exc is None:
                logger.error('Future/Task exception was never retrieved:\n%s',
                             ''.join(self.tb))
