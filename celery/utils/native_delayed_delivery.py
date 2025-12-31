"""Native delayed delivery utilities for Celery.

This module provides utilities for checking and validating native delayed delivery
support across different message transports. It defines the interface that Celery
expects from Kombu transports that support native delayed delivery.

Native delayed delivery allows tasks with countdown/eta to be delayed at the broker
level rather than having workers hold messages until their execution time.
"""
from celery.exceptions import ImproperlyConfigured

__all__ = (
    'DELAY_HEADER',
    'should_use_native_delayed_delivery',
    'validate_native_delayed_delivery_config',
)

# Header name for delay information.
# Transports that support native delayed delivery read this header during publish
# to determine how long to delay message delivery.
DELAY_HEADER = 'x-celery-delay-seconds'


def should_use_native_delayed_delivery(app, transport) -> bool:
    """Check if native delayed delivery should be used via the new general interface.

    This function ONLY checks the new `broker_native_delayed_delivery_enabled` config.
    It is completely independent of the legacy quorum queue auto-detection, which
    remains handled separately in the existing code paths.

    Args:
        app: The Celery application instance.
        transport: The Kombu transport instance.

    Returns:
        bool: True if native delayed delivery is enabled and the transport supports it.
    """
    if not app.conf.broker_native_delayed_delivery_enabled:
        return False

    return getattr(transport, 'supports_native_delayed_delivery', False)


def validate_native_delayed_delivery_config(app, transport) -> None:
    """Validate native delayed delivery configuration at startup.

    This function should be called during worker startup to ensure that
    the native delayed delivery configuration is valid.

    Args:
        app: The Celery application instance.
        transport: The Kombu transport instance.

    Raises:
        ImproperlyConfigured: If broker_native_delayed_delivery_enabled is True
            but the transport does not support native delayed delivery.
    """
    if app.conf.broker_native_delayed_delivery_enabled:
        if not getattr(transport, 'supports_native_delayed_delivery', False):
            raise ImproperlyConfigured(
                f"broker_native_delayed_delivery_enabled is True but transport "
                f"'{transport.driver_type}' does not support native delayed delivery. "
                f"Either disable the setting or use a supported transport."
            )
