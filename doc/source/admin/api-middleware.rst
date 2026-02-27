.. _api-middleware:

======================
Custom API Middleware
======================

Ironic supports loading custom WSGI middleware via stevedore entry points.
This allows operators to add custom request processing such as validation,
logging, rate limiting, or other functionality without modifying Ironic
source code.

This follows the same plugin pattern used for inspection hooks and hardware
interfaces.

Enabling Custom Middleware
==========================

To enable custom middleware, configure the ``[api] middleware`` option in
``/etc/ironic/ironic.conf`` with a comma-separated list of middleware names::

    [api]
    middleware = my-middleware,another-middleware

Middleware are applied in the order specified, wrapping the API application
from inside out. The last middleware in the list will be the outermost
wrapper and will process requests first.

Writing Custom Middleware
=========================

Custom middleware must be a callable that accepts a WSGI application and
returns a wrapped WSGI application. The simplest form is a class with
``__init__`` and ``__call__`` methods:

.. code-block:: python

    class MyMiddleware:
        """Example WSGI middleware."""

        def __init__(self, application):
            self.application = application

        def __call__(self, environ, start_response):
            # Pre-process request here
            # ...

            # Call the wrapped application
            return self.application(environ, start_response)

For more complex middleware, you can use the ``webob`` library:

.. code-block:: python

    import webob
    import webob.dec

    class MyMiddleware:
        """Example middleware using webob."""

        def __init__(self, application):
            self.application = application

        @webob.dec.wsgify
        def __call__(self, req):
            # Validate request
            if not self._is_valid(req):
                return webob.Response(status=400, json={'error': 'Invalid'})

            # Pass through to the application
            return req.get_response(self.application)

Registering Middleware
======================

Middleware are registered via the ``ironic.api.middleware`` entry point
namespace. In your package's ``pyproject.toml``:

.. code-block:: toml

    [project.entry-points."ironic.api.middleware"]
    my-middleware = "my_package.middleware:MyMiddleware"

Or in ``setup.cfg``:

.. code-block:: ini

    [entry_points]
    ironic.api.middleware =
        my-middleware = my_package.middleware:MyMiddleware

After installing the package, the middleware can be enabled by adding
``my-middleware`` to the ``[api] middleware`` configuration option.

Example: Request Validation Middleware
======================================

Here's an example middleware that validates portgroup names:

.. code-block:: python

    import json
    import re
    import webob
    import webob.dec

    class PortgroupNameValidationMiddleware:
        """Validate portgroup names match a required format."""

        PATTERN = re.compile(r'^.+-port-channel(\d+)$')

        def __init__(self, application):
            self.application = application

        @webob.dec.wsgify
        def __call__(self, req):
            if (req.method == 'POST'
                    and req.path_info.rstrip('/') == '/v1/portgroups'):
                try:
                    body = json.loads(req.body)
                    name = body.get('name', '')
                    if not self.PATTERN.match(name):
                        return webob.Response(
                            status=400,
                            json={'error': 'Invalid portgroup name format'}
                        )
                except json.JSONDecodeError:
                    pass  # Let Ironic handle malformed JSON

            return req.get_response(self.application)

Register it in ``pyproject.toml``:

.. code-block:: toml

    [project.entry-points."ironic.api.middleware"]
    portgroup-validation = "my_package:PortgroupNameValidationMiddleware"

Enable it in ``ironic.conf``:

.. code-block:: ini

    [api]
    middleware = portgroup-validation
