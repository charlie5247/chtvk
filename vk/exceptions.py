"""VK transport exception hierarchy."""


class VKAdapterError(Exception):
    pass


class ConfigurationError(VKAdapterError):
    pass


class ValidationError(VKAdapterError):
    pass


class TemporaryVKError(VKAdapterError):
    pass


class PermanentVKError(VKAdapterError):
    pass
