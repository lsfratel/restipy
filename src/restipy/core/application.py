import importlib
import inspect
import os
import re
import traceback
import typing as t
from types import ModuleType

from restipy.core.exceptions import HTTPException, RestiPyException
from restipy.core.middleware import Middleware
from restipy.core.request import Request
from restipy.core.response import Response
from restipy.core.view import View


class RestiPy:
    """
    The RestiPy class is the main entry point for the RestiPy web framework. It
    provides methods for bootstrapping the application, managing routes and
    middleware, and handling WSGI requests.

    The `__init__` method initializes the internal data structures for
    managing routes, middleware, and configuration. The `bootstrap` method
    sets up the application by loading the configuration, importing views and
    middleware.

    The `_add_view` method adds a view to the application's route mapping. The
    `_import_module` and `_get_module_members` methods are utility functions
    for importing modules and getting their members.

    The `_import_view` and `_import_middleware` methods are responsible for
    importing and adding views and middleware to the application, respectively.
    The `add_middleware` method allows adding middleware to the application.

    The `match` method matches the incoming request path and HTTP method to a
    registered route, and returns the matched view and any extracted parameters
    from the path.

    The `__call__` and `wsgi` methods handle the WSGI callable interface,
    processing the incoming request and returning the response.

    The `process_request` method is the core of the request processing logic,
    executing any registered middleware and the matched view's handler.
    """

    def __init__(self) -> None:
        """
        Initializes a new instance of the RestiPy class.

        This constructor sets up the internal data structures for managing
        routes, middleware, and configuration.

        Attributes:
            `_routes (dict[str, list[Route]]):` A dictionary mapping HTTP
                methods to lists of routes.
            `config (ModuleType):` The module containing the application
                settings.
            `_before_route_m (list[Callable]):` A list of middleware functions
                to be executed before a route is matched.
            `_before_m (list[Callable]):` A list of middleware functions to be
                executed before a request is processed.
            `_after_m (list[Callable]):` A list of middleware functions to be
                executed after a request is processed.
        """
        self.config: ModuleType

        self._routes: dict[str, list[View]] = {}

        self._before_route_m: list[t.Callable] = []
        self._before_m: list[t.Callable] = []
        self._after_m: list[t.Callable] = []

    def bootstrap(self, settings: ModuleType):
        """
        Bootstraps the application by setting the configuration, importing
        views, and middleware.

        Args:
            `settings (ModuleType):` The module containing the application
                settings.
        """
        self.config = settings

        for view in settings.VIEWS:
            self._import_view(view)

        for middleware in settings.MIDDLEWARES:
            self._import_middleware(middleware)

    def _add_view(
        self,
        view: View,
    ) -> None:
        """
        Adds a view to the application's routing table.

        Args:
            `view (View):` The view to be added.

        This method compiles the route pattern in the view, and then adds the
        view to the appropriate list of routes based on the HTTP methods it
        supports. If a list of routes for a particular HTTP method does not
        yet exist, it is created.
        """
        if not isinstance(view.route, str):
            view.route = re.compile(view.route)

        for method in view.methods:
            method = method.upper()
            if method not in self._routes:
                self._routes[method] = []
            self._routes[method].append(view)

    def _import_module(self, filename: str) -> ModuleType:
        """
        Import a module given its filename.

        Args:
            `filename (str):` The filename of the module to import.

        Returns:
            `ModuleType:` The imported module.
        """
        if os.path.sep in filename:
            filename = filename.replace(os.path.sep, '.')
        return importlib.import_module(filename)

    def _get_module_members(self, module: ModuleType, mt=inspect.isclass):
        """
        Get the members of a module that satisfy a given condition.

        Args:
            `module (ModuleType)`: The module to inspect.
            `mt (Callable)`: The condition that the members should satisfy.
                Defaults to `inspect.isclass`.

        Yields:
            `Tuple[str, Any]:` A tuple containing the name and member that
                satisfy the condition.
        """
        for name, member in inspect.getmembers(module, mt):
            if member.__module__ == module.__name__:
                yield (name, member)

    def _import_view(self, view: str):
        """
        Import a view module and add its routes to the application.

        Args:
            `view (str):` The name of the view module to import.
        """
        module = self._import_module(view)
        for _, mview in self._get_module_members(module):
            if not issubclass(mview, View):
                continue
            self._add_view(mview(self))

    def _import_middleware(self, middleware: str):
        """
        Imports and adds a middleware to the application.

        Args:
            `middleware (str):` The fully qualified name of the middleware
                class.
        """
        module = self._import_module(middleware)
        for _, member in self._get_module_members(module):
            if not issubclass(member, Middleware):
                continue
            self.add_middleware(member(self))

    def add_middleware(self, middleware: Middleware):
        """
        Adds a middleware to the application.

        Args:
            `middleware:` The middleware object to be added.

        This method adds the specified middleware to the application's list of
        middlewares.

        The middleware will be executed in the order they are added, before and
        after the route handlers.
        """
        self._before_route_m.append(middleware.before_route)
        self._before_m.append(middleware.before_handler)
        self._after_m.append(middleware.after_handler)

    def match(
        self, path: str, method: str
    ) -> tuple[View, dict[str, str | t.Any]]:
        """
        Matches the given path and method to a route in the application.

        Args:
            `path (str):` The path to match against the routes.
            `method (str):` The HTTP method to match against the routes.

        Returns:
            `tuple[Route, dict[str, str | t.Any]]:` A tuple containing the
                matched route and a dictionary of captured parameters from
                the path.

        Raises:
            `HTTPException:` If no matching route is found.
        """
        methods = [method]
        if method == 'HEAD':
            methods.append('GET')
        for method in methods:
            routes = self._routes.get(method) or []
            for view in routes:
                if match := view.route.match(path):  # type: ignore
                    return view, match.groupdict()
        raise HTTPException(
            'Route not found.', status_code=404, code='ROUTE_NOT_FOUND'
        )

    def __call__(
        self, env: dict, start_response: t.Callable
    ) -> t.Iterable[bytes]:
        """
        Handle the WSGI callable interface.

        Args:
            `env (dict):` The WSGI environment dictionary.
            `start_response (callable):` The WSGI start_response callable.

        Returns:
            `Iterable[bytes]:` An iterable of response bytes.
        """
        return self.wsgi(env, start_response)

    def wsgi(self, env: dict, start_response: t.Callable) -> t.Iterable[bytes]:
        """
        Handle the WSGI request and response.

        Args:
            `env (dict):` The WSGI environment dictionary.
            `start_response (callable):` The WSGI start_response callable.

        Returns:
            `Iterable[bytes]:` An iterable of response bytes.
        """
        method = env.get('REQUEST_METHOD', 'GET').upper()
        out = self.process_request(env)

        data, status, headers = out.get_response()

        start_response(status, headers)

        if method == 'HEAD':
            return []

        yield data

    def process_request(self, env: dict) -> Response:
        """
        Process the incoming request and return a response.

        Args:
            `env (dict):` The WSGI environment dictionary.

        Returns:
            `Response:` The response object.

        Raises:
            `HTTPException:` If an HTTP exception occurs.
            `Exception:` If any other exception occurs.
        """
        env['restipy.app'] = self
        req = Request(env)
        out: t.Optional[Response] = None

        try:
            """
            Process the incoming request through any registered middleware
            before routing the request.

            The middleware is executed in the order they were registered.
            If any middleware returns a `Response` object, the request
            processing is short-circuited and the response is returned
            immediately.

            Args:
                `req (Request):` The incoming request object.

            Returns:
                `Response:` The response object, if a middleware returned one.
            """
            for middleware in self._before_route_m:
                out = middleware(req)
                if isinstance(out, Response):
                    return out

            """
            Match the incoming request path and HTTP method to a registered
            route.

            Args:
                `req (Request):` The incoming request object.

            Returns:
                `Tuple[Route, dict]:` The matched route and any extracted
                    parameters from the path.
            """
            view, params = self.match(req.path, req.method)

            req.set_params = params

            """
            Process the incoming request through any registered middleware
            before routing the request.

            The middleware is executed in the order they were registered.
            If any middleware returns a `Response` object, the request
            processing is short-circuited and the response is returned
            immediately.

            Args:
                `req (Request):` The incoming request object.

            Returns:
                `Response:` The response object, if a middleware returned one.
            """
            for middleware in self._before_m:
                out = middleware(req)
                if isinstance(out, Response):
                    return out

            """
            Process the incoming request through the registered view.

            This block of code is responsible for executing the before,
            handler, and after hooks of the matched view. If any of these
            hooks return a `Response` object, the request processing is
            short-circuited and the response is returned immediately.

            If the view handler does not return a `Response` object, an
            exception is raised. If a `RestiPyException` is raised during the
            request processing, the `on_exception` hook of the view is
            executed, and the returned response is returned.
            """
            try:
                out = view.before_handler(req)
                if isinstance(out, Response):
                    return out
                out = view.handler(req)
                if not isinstance(out, Response):
                    raise RestiPyException(
                        'Route handler must return a Response object.'
                    )
                view.after_handler(req, out)
            except RestiPyException as e:
                out = view.on_exception(req, e)
                if not isinstance(out, Response):
                    raise RestiPyException(
                        'Route exception must return Response object.'
                    ) from e
                return out

            for middleware in self._after_m:
                middleware(req, out)

            return out
        except HTTPException as e:
            return Response(
                e.get_response(), status_code=e.status_code, headers=e.headers
            )
        except Exception as e:
            """
            Handles an unhandled exception that occurred during the request
            processing.

            This code block is executed when an unhandled exception occurs
            during the request processing. It logs the exception traceback to
            the WSGI error stream, and returns a JSON response with an error
            message and the exception traceback.

            If the application is running in debug mode
            (self.config.DEBUG is True), the response will include the full
            exception traceback. Otherwise, a generic "Something went wrong,
            try again later" error message is returned.

            Args:
                `e (Exception):` The unhandled exception that occurred.

            Returns:
                `Response:` A JSON response with an error message and, if in
                    debug mode, the exception traceback.
            """
            traceback.print_exc()
            stacktrace = traceback.format_exc()
            env['wsgi.errors'].write(stacktrace)
            env['wsgi.errors'].flush()
            if self.config.DEBUG is False:
                return Response(
                    {
                        'code': 'INTERNAL_SERVER_ERROR',
                        'error': 'Something went wrong, try again later.',
                    },
                    status_code=500,
                )
            return Response(
                {
                    'code': 'INTERNAL_SERVER_ERROR',
                    'error': str(e),
                    'stacktrace': stacktrace.splitlines(),
                },
                status_code=500,
            )
