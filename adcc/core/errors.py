"""Core exceptions that do not depend on HTTP or a host platform."""


class ConfigSchemaError(ValueError):
    """The persisted configuration does not match a supported schema."""


class FutureConfigSchemaError(ConfigSchemaError):
    """The persisted schema is newer than this application understands."""

