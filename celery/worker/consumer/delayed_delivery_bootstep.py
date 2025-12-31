"""Native delayed delivery bootstep for Celery workers.

This module provides the DelayedDeliveryBootstep which handles setup
of native delayed delivery via the transport-agnostic interface.

This is separate from the legacy DelayedDelivery bootstep which handles
RabbitMQ quorum queue specific delayed delivery.
"""
from typing import Iterator, List, Set, Union

from kombu.utils.functional import retry_over_time

from celery import Celery, bootsteps
from celery.utils.log import get_logger
from celery.utils.native_delayed_delivery import (
    should_use_native_delayed_delivery,
    validate_native_delayed_delivery_config,
)
from celery.worker.consumer import Consumer, Tasks

__all__ = ('DelayedDeliveryBootstep',)

logger = get_logger(__name__)


# Default retry settings
RETRY_INTERVAL = 1.0  # seconds between retries
MAX_RETRIES = 3       # maximum number of retries
RETRIED_EXCEPTIONS = (ConnectionRefusedError, OSError)


class DelayedDeliveryBootstep(bootsteps.StartStopStep):
    """Bootstep that sets up native delayed delivery via the transport interface.

    This component handles setup of native delayed delivery for transports that
    implement the `supports_native_delayed_delivery` property and
    `setup_native_delayed_delivery` method.

    This is independent of the legacy RabbitMQ quorum queue delayed delivery
    which is handled by the separate DelayedDelivery bootstep.

    Responsibilities:
        - Validating that the transport supports native delayed delivery
        - Calling the transport's setup_native_delayed_delivery method
        - Handling connection failures gracefully with retries
    """

    requires = (Tasks,)

    def include_if(self, c: Consumer) -> bool:
        """Determine if this bootstep should be included.

        Args:
            c: The Celery consumer instance

        Returns:
            bool: True if native delayed delivery is enabled and transport supports it
        """
        transport = c.app.connection_for_write().transport
        return should_use_native_delayed_delivery(c.app, transport)

    def start(self, c: Consumer) -> None:
        """Initialize native delayed delivery via the transport interface.

        Args:
            c: The Celery consumer instance

        Raises:
            ImproperlyConfigured: If native delayed delivery is enabled
                but the transport doesn't support it
        """
        app: Celery = c.app

        # Validate config (raises if enabled but transport doesn't support it)
        with app.connection_for_write() as connection:
            validate_native_delayed_delivery_config(app, connection.transport)

        broker_urls = self._get_broker_urls(app.conf.broker_url)
        setup_errors = []

        for broker_url in broker_urls:
            try:
                retry_over_time(
                    self._setup_delayed_delivery,
                    args=(c, broker_url),
                    catch=RETRIED_EXCEPTIONS,
                    errback=self._on_retry,
                    interval_start=RETRY_INTERVAL,
                    max_retries=MAX_RETRIES,
                )
            except Exception as e:
                logger.warning(
                    "Failed to setup general delayed delivery for %r: %s",
                    broker_url, str(e)
                )
                setup_errors.append((broker_url, e))

        if len(setup_errors) == len(broker_urls):
            logger.critical(
                "Failed to setup general delayed delivery for all broker URLs. "
                "Native delayed delivery will not be available."
            )

    def _setup_delayed_delivery(self, c: Consumer, broker_url: str) -> None:
        """Set up delayed delivery for a specific broker URL.

        Args:
            c: The Celery consumer instance
            broker_url: The broker URL to configure

        Raises:
            ConnectionRefusedError: If connection to the broker fails
            OSError: If there are network-related issues
            Exception: For other unexpected errors during setup
        """
        with c.app.connection_for_write(url=broker_url) as connection:
            transport = connection.transport
            queues = c.app.amqp.queues.values()

            logger.debug(
                "Setting up general delayed delivery for broker %r",
                broker_url
            )

            transport.setup_native_delayed_delivery(
                connection=connection,
                queues=queues,
            )

            logger.info(
                "Successfully set up general delayed delivery for %r",
                broker_url
            )

    def _on_retry(self, exc: Exception, interval_range: Iterator[float], intervals_count: int) -> float:
        """Callback for retry attempts.

        Args:
            exc: The exception that triggered the retry
            interval_range: An iterator which returns the time in seconds to sleep next
            intervals_count: Number of retry attempts so far
        """
        interval = next(interval_range)
        logger.warning(
            "Retrying general delayed delivery setup (attempt %d/%d) after error: %s. "
            "Sleeping %.2f seconds.",
            intervals_count + 1, MAX_RETRIES, str(exc), interval
        )
        return interval

    def _get_broker_urls(self, broker_urls: Union[str, List[str]]) -> Set[str]:
        """Get broker URLs from configuration.

        Args:
            broker_urls: Broker URLs, either as a semicolon-separated string
                  or as a list of strings

        Returns:
            Set of broker URLs
        """
        if isinstance(broker_urls, str):
            return {url for url in broker_urls.split(";")}
        return set(broker_urls)
