"""Native delayed delivery bootstep for transport-agnostic delayed message delivery.

This module provides a bootstep that manages native delayed delivery for
transports that support it. Unlike the existing delayed_delivery module
which is specific to RabbitMQ quorum queues, this module provides a
generic interface that any Kombu transport can implement.

Expected transport interface:
    - transport.supports_native_delayed_delivery: bool property
    - channel.setup_native_delayed_delivery(queues): Setup method
    - channel.teardown_native_delayed_delivery(): Teardown method
"""

from __future__ import annotations

from celery import bootsteps
from celery.utils.log import get_logger
from celery.worker.consumer.tasks import Tasks

__all__ = ('NativeDelayedDeliveryStep',)

logger = get_logger(__name__)


class NativeDelayedDeliveryStep(bootsteps.StartStopStep):
    """Bootstep that manages native delayed delivery for supporting transports.

    This bootstep:
    - Checks if broker_native_delayed_delivery_enabled is True
    - Checks if the transport supports native delayed delivery
    - Calls channel.setup_native_delayed_delivery() on start
    - Calls channel.teardown_native_delayed_delivery() on stop

    The bootstep is only included when the config setting is enabled.
    """

    requires = (Tasks,)

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self._setup_called = False

    def include_if(self, parent):
        """Determine if this bootstep should be included.

        Returns True if broker_native_delayed_delivery_enabled is True.

        Arguments:
            parent: The Consumer instance.

        Returns:
            bool: True if native delayed delivery is enabled.
        """
        return parent.app.conf.broker_native_delayed_delivery_enabled

    def start(self, parent):
        """Start native delayed delivery processing.

        Checks if the transport supports native delayed delivery and
        calls the transport's setup method with the list of queues
        being consumed.

        Arguments:
            parent: The Consumer instance.
        """
        connection = parent.connection
        transport = connection.transport

        if not getattr(transport, 'supports_native_delayed_delivery', False):
            logger.debug('Transport does not support native delayed delivery')
            return

        # Get list of queues being consumed
        if parent.task_consumer is None:
            logger.warning(
                'Task consumer not available, cannot setup native delayed delivery'
            )
            return

        queues = [q.name for q in parent.task_consumer.queues]
        if not queues:
            logger.warning('No queues to setup for native delayed delivery')
            return

        # Call transport's setup method via the channel
        channel = connection.default_channel
        setup = getattr(channel, 'setup_native_delayed_delivery', None)
        if callable(setup):
            logger.info(
                'Starting native delayed delivery for %d queue(s)', len(queues)
            )
            try:
                setup(queues)
                self._setup_called = True
            except Exception as exc:
                logger.error(
                    'Failed to setup native delayed delivery: %s', exc,
                    exc_info=True
                )
        else:
            logger.warning(
                'Transport claims native delayed delivery support but '
                'channel.setup_native_delayed_delivery is not available'
            )

    def stop(self, parent):
        """Stop native delayed delivery processing.

        Calls the transport's teardown method if setup was previously called.

        Arguments:
            parent: The Consumer instance.
        """
        if not self._setup_called:
            return

        connection = parent.connection
        channel = connection.default_channel

        teardown = getattr(channel, 'teardown_native_delayed_delivery', None)
        if callable(teardown):
            logger.info('Stopping native delayed delivery')
            try:
                teardown()
            except Exception as exc:
                logger.error(
                    'Failed to teardown native delayed delivery: %s', exc,
                    exc_info=True
                )
            finally:
                self._setup_called = False
        else:
            self._setup_called = False
