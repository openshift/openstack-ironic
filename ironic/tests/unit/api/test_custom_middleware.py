#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""
Tests for pluggable API middleware loaded via stevedore entry points.

This demonstrates how external packages can register custom WSGI middleware
via the ironic.api.middleware entry point namespace.
"""

import json
import re
import unittest
from unittest import mock

from oslo_config import cfg
import webob
import webob.dec

from ironic.api import app
from ironic.conf import CONF


# Example middleware: Portgroup name validation
# This is a simplified version of what an external package might provide
# to validate portgroup names match a required format.

PORTGROUP_NAME_PATTERN = re.compile(r"^.+-port-channel(\d+)$")
PORT_CHANNEL_MIN = 100
PORT_CHANNEL_MAX = 998


class PortgroupNameValidationMiddleware:
    """Example WSGI middleware that validates portgroup names.

    This demonstrates a real-world use case: validating that portgroup
    names match a required format for integration with external switch
    configuration systems.

    Intercepts POST /v1/portgroups requests and validates that names
    match the format: {node_name}-port-channel{number}
    """

    def __init__(self, application):
        self.application = application

    @webob.dec.wsgify
    def __call__(self, req):
        # Only validate portgroup create requests
        if (req.method == "POST"
                and req.path_info.rstrip("/") == "/v1/portgroups"):
            return self._validate_create(req)
        return req.get_response(self.application)

    def _validate_create(self, req):
        """Validate portgroup name on create."""
        try:
            body = json.loads(req.body)
        except (json.JSONDecodeError, ValueError):
            # Let Ironic handle malformed JSON
            return req.get_response(self.application)

        name = body.get("name")
        if not name:
            return self._error_response(
                "Portgroup name is required and cannot be empty."
            )

        match = PORTGROUP_NAME_PATTERN.match(name)
        if not match:
            return self._error_response(
                f"Portgroup name '{name}' must match format "
                "'{node_name}-port-channel{{number}}'"
            )

        port_channel_num = int(match.group(1))
        if not (PORT_CHANNEL_MIN <= port_channel_num <= PORT_CHANNEL_MAX):
            return self._error_response(
                f"Portgroup name '{name}' has invalid port-channel number "
                f"{port_channel_num}. Must be between {PORT_CHANNEL_MIN} "
                f"and {PORT_CHANNEL_MAX}."
            )

        return req.get_response(self.application)

    def _error_response(self, message):
        """Return a 400 Bad Request response."""
        error_body = {
            "error_message": json.dumps({
                "faultstring": message,
                "faultcode": "Client",
                "debuginfo": None
            })
        }
        return webob.Response(
            status=400,
            content_type="application/json",
            body=json.dumps(error_body).encode("utf-8"),
        )


class TestLoadCustomMiddleware(unittest.TestCase):
    """Tests for the _load_custom_middleware function."""

    def test_load_no_middleware(self):
        """Test that no middleware is loaded when config is empty."""
        test_app = mock.MagicMock()
        result = app._load_custom_middleware(test_app, [])
        # With empty list, the original app should be returned unchanged
        self.assertEqual(test_app, result)

    @mock.patch('stevedore.NamedExtensionManager', autospec=True)
    def test_load_portgroup_validation_middleware(self, mock_manager):
        """Test loading portgroup name validation middleware."""
        test_app = mock.MagicMock()

        # Create a mock extension for portgroup validation
        mock_ext = mock.MagicMock()
        mock_ext.name = 'portgroup-name-validation'
        mock_ext.plugin = PortgroupNameValidationMiddleware

        mock_manager.return_value = [mock_ext]

        result = app._load_custom_middleware(
            test_app, ['portgroup-name-validation']
        )

        # Verify stevedore was called correctly
        mock_manager.assert_called_once_with(
            'ironic.api.middleware',
            names=['portgroup-name-validation'],
            invoke_on_load=False,
            on_missing_entrypoints_callback=app._missing_middleware_callback,
            name_order=True,
        )

        # Verify the middleware was applied
        self.assertIsInstance(result, PortgroupNameValidationMiddleware)
        self.assertEqual(result.application, test_app)

    @mock.patch('stevedore.NamedExtensionManager', autospec=True)
    def test_load_multiple_middleware(self, mock_manager):
        """Test loading multiple middleware in order."""
        test_app = mock.MagicMock()

        # Create mock extensions
        mock_ext1 = mock.MagicMock()
        mock_ext1.name = 'portgroup-name-validation'
        mock_ext1.plugin = PortgroupNameValidationMiddleware

        mock_ext2 = mock.MagicMock()
        mock_ext2.name = 'another-middleware'
        mock_ext2.plugin = PortgroupNameValidationMiddleware

        mock_manager.return_value = [mock_ext1, mock_ext2]

        result = app._load_custom_middleware(
            test_app, ['portgroup-name-validation', 'another-middleware']
        )

        # The result should be middleware2 wrapping middleware1 wrapping app
        self.assertIsInstance(result, PortgroupNameValidationMiddleware)
        self.assertIsInstance(
            result.application, PortgroupNameValidationMiddleware
        )
        self.assertEqual(result.application.application, test_app)


class TestMissingMiddlewareCallback(unittest.TestCase):
    """Tests for the _missing_middleware_callback function."""

    def test_missing_middleware_raises_runtime_error(self):
        """Test that missing middleware raises RuntimeError."""
        missing_names = ['missing1', 'missing2']
        self.assertRaises(
            RuntimeError,
            app._missing_middleware_callback,
            missing_names
        )

    def test_missing_middleware_error_message(self):
        """Test that error message contains missing middleware names."""
        missing_names = ['portgroup-validation', 'rate-limiter']
        try:
            app._missing_middleware_callback(missing_names)
            self.fail("Expected RuntimeError to be raised")
        except RuntimeError as e:
            self.assertIn('portgroup-validation', str(e))
            self.assertIn('rate-limiter', str(e))


class TestMiddlewareConfigOption(unittest.TestCase):
    """Tests for the [api] middleware config option."""

    def setUp(self):
        # Register the api config options if not already registered
        from ironic.conf import api as api_conf
        try:
            api_conf.register_opts(CONF)
        except cfg.DuplicateOptError:
            pass  # Already registered

    def test_middleware_config_default_empty(self):
        """Test that middleware config defaults to empty list."""
        self.assertEqual([], CONF.api.middleware)

    def test_middleware_config_accepts_list(self):
        """Test that middleware config accepts a list of names."""
        CONF.set_override(
            'middleware',
            ['portgroup-name-validation', 'rate-limiter'],
            group='api'
        )
        try:
            self.assertEqual(
                ['portgroup-name-validation', 'rate-limiter'],
                CONF.api.middleware
            )
        finally:
            CONF.clear_override('middleware', group='api')


class TestPortgroupNameValidationMiddleware(unittest.TestCase):
    """Tests for the example portgroup name validation middleware.

    These tests demonstrate how the middleware validates portgroup names
    to ensure they match the required format for undersync integration.
    """

    def setUp(self):
        # Create a simple WSGI app that returns 200 OK
        def simple_app(environ, start_response):
            start_response('200 OK', [('Content-Type', 'application/json')])
            return [b'{"result": "ok"}']
        self.middleware = PortgroupNameValidationMiddleware(simple_app)

    def _make_request(self, method, path, body=None):
        """Create a webob Request for testing."""
        req = webob.Request.blank(path)
        req.method = method
        if body:
            req.body = json.dumps(body).encode('utf-8')
            req.content_type = 'application/json'
        return req

    def test_valid_portgroup_name(self):
        """Test that valid portgroup names are accepted."""
        req = self._make_request(
            'POST', '/v1/portgroups',
            {'name': 'server01-port-channel100', 'node_uuid': 'test-uuid'}
        )
        resp = req.get_response(self.middleware)
        # Should pass through to the app (returns 200)
        self.assertEqual(200, resp.status_int)

    def test_invalid_portgroup_name_format(self):
        """Test that invalid portgroup name format is rejected."""
        req = self._make_request(
            'POST', '/v1/portgroups',
            {'name': 'invalid-name', 'node_uuid': 'test-uuid'}
        )
        resp = req.get_response(self.middleware)
        self.assertEqual(400, resp.status_int)
        body = json.loads(resp.body)
        error = json.loads(body['error_message'])
        self.assertIn('must match format', error['faultstring'])

    def test_request_passes_through(self):
        """Test that non-portgroup requests pass through unchanged."""
        req = self._make_request('GET', '/v1/nodes')
        resp = req.get_response(self.middleware)
        self.assertEqual(200, resp.status_int)

    def test_portgroup_get_request_passes_through(self):
        """Test that GET requests to portgroups pass through."""
        req = self._make_request('GET', '/v1/portgroups')
        resp = req.get_response(self.middleware)
        self.assertEqual(200, resp.status_int)
