"""Unit tests for the native delayed delivery bootstep."""

import logging
from unittest.mock import MagicMock, Mock, patch

import pytest

from celery.worker.consumer.native_delayed import NativeDelayedDeliveryStep


class test_NativeDelayedDeliveryStep:
    """Tests for NativeDelayedDeliveryStep bootstep."""

    @pytest.fixture(autouse=True)
    def setup_logging(self, caplog):
        """Enable logging capture for all tests."""
        caplog.set_level(logging.DEBUG)

    def test_init(self):
        """Test bootstep initialization."""
        parent = Mock()
        step = NativeDelayedDeliveryStep(parent)
        assert step._setup_called is False

    def test_start_transport_not_supported(self, caplog):
        """Test that start does nothing for unsupported transports."""
        parent = Mock()
        parent.connection.transport.supports_native_delayed_delivery = False

        step = NativeDelayedDeliveryStep(parent)
        step.start(parent)

        assert step._setup_called is False
        assert 'Transport does not support native delayed delivery' in caplog.text

    def test_start_no_task_consumer(self, caplog):
        """Test that start warns when task consumer is not available."""
        parent = Mock()
        parent.connection.transport.supports_native_delayed_delivery = True
        parent.task_consumer = None

        step = NativeDelayedDeliveryStep(parent)
        step.start(parent)

        assert step._setup_called is False
        assert 'Task consumer not available' in caplog.text

    def test_start_no_queues(self, caplog):
        """Test that start warns when no queues are configured."""
        parent = Mock()
        parent.connection.transport.supports_native_delayed_delivery = True
        parent.task_consumer.queues = []

        step = NativeDelayedDeliveryStep(parent)
        step.start(parent)

        assert step._setup_called is False
        assert 'No queues to setup' in caplog.text

    def test_start_calls_setup(self, caplog):
        """Test that start calls channel's setup method."""
        parent = Mock()
        parent.connection.transport.supports_native_delayed_delivery = True

        # Mock queues
        queue1 = Mock()
        queue1.name = 'queue1'
        queue2 = Mock()
        queue2.name = 'queue2'
        parent.task_consumer.queues = [queue1, queue2]

        # Mock channel with setup method
        setup_mock = Mock()
        parent.connection.default_channel.setup_native_delayed_delivery = setup_mock

        step = NativeDelayedDeliveryStep(parent)
        step.start(parent)

        assert step._setup_called is True
        setup_mock.assert_called_once_with(['queue1', 'queue2'])
        assert 'Starting native delayed delivery for 2 queue(s)' in caplog.text

    def test_start_setup_not_callable(self, caplog):
        """Test warning when setup method is not available."""
        parent = Mock()
        parent.connection.transport.supports_native_delayed_delivery = True

        queue = Mock()
        queue.name = 'queue1'
        parent.task_consumer.queues = [queue]

        # No setup method on channel
        del parent.connection.default_channel.setup_native_delayed_delivery

        step = NativeDelayedDeliveryStep(parent)
        step.start(parent)

        assert step._setup_called is False
        assert 'channel.setup_native_delayed_delivery is not available' in caplog.text

    def test_start_setup_raises_exception(self, caplog):
        """Test error handling when setup raises an exception."""
        parent = Mock()
        parent.connection.transport.supports_native_delayed_delivery = True

        queue = Mock()
        queue.name = 'queue1'
        parent.task_consumer.queues = [queue]

        # Setup raises exception
        parent.connection.default_channel.setup_native_delayed_delivery.side_effect = \
            RuntimeError('Setup failed')

        step = NativeDelayedDeliveryStep(parent)
        step.start(parent)

        assert step._setup_called is False
        assert 'Failed to setup native delayed delivery' in caplog.text

    def test_stop_not_setup(self):
        """Test that stop does nothing if setup wasn't called."""
        parent = Mock()

        step = NativeDelayedDeliveryStep(parent)
        step._setup_called = False
        step.stop(parent)

        # Verify teardown wasn't called
        parent.connection.default_channel.teardown_native_delayed_delivery.assert_not_called()

    def test_stop_calls_teardown(self, caplog):
        """Test that stop calls channel's teardown method."""
        parent = Mock()
        teardown_mock = Mock()
        parent.connection.default_channel.teardown_native_delayed_delivery = teardown_mock

        step = NativeDelayedDeliveryStep(parent)
        step._setup_called = True
        step.stop(parent)

        assert step._setup_called is False
        teardown_mock.assert_called_once()
        assert 'Stopping native delayed delivery' in caplog.text

    def test_stop_teardown_not_callable(self):
        """Test stop when teardown method is not available."""
        parent = Mock()
        del parent.connection.default_channel.teardown_native_delayed_delivery

        step = NativeDelayedDeliveryStep(parent)
        step._setup_called = True
        step.stop(parent)

        # Should reset _setup_called even if teardown not available
        assert step._setup_called is False

    def test_stop_teardown_raises_exception(self, caplog):
        """Test error handling when teardown raises an exception."""
        parent = Mock()
        parent.connection.default_channel.teardown_native_delayed_delivery.side_effect = \
            RuntimeError('Teardown failed')

        step = NativeDelayedDeliveryStep(parent)
        step._setup_called = True
        step.stop(parent)

        # Should still reset _setup_called on error
        assert step._setup_called is False
        assert 'Failed to teardown native delayed delivery' in caplog.text

    def test_requires_tasks(self):
        """Test that bootstep requires Tasks."""
        from celery.worker.consumer.tasks import Tasks
        assert Tasks in NativeDelayedDeliveryStep.requires

    def test_include_if_enabled(self):
        """Test include_if returns True when setting is enabled."""
        parent = Mock()
        parent.app.conf.broker_native_delayed_delivery_enabled = True

        step = NativeDelayedDeliveryStep(parent)
        assert step.include_if(parent) is True

    def test_include_if_disabled(self):
        """Test include_if returns False when setting is disabled."""
        parent = Mock()
        parent.app.conf.broker_native_delayed_delivery_enabled = False

        step = NativeDelayedDeliveryStep(parent)
        assert step.include_if(parent) is False
