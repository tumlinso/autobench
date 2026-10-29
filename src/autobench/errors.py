class AutobenchError(Exception):
    """Expected user-facing Autobench failure."""


class RegistrationError(AutobenchError):
    pass


class SchedulerUnavailable(AutobenchError):
    pass


class UnsafeWorkspace(AutobenchError):
    pass

