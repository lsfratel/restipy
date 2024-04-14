from __future__ import annotations

import email
import email.parser
import email.policy
import io
import json
import shutil
import tempfile
import typing as t
from urllib.parse import parse_qsl, urljoin

from restipy.core.exceptions import HTTPException
from restipy.utils.helpers import UploadedFile, env_to_h

if t.TYPE_CHECKING:
    from restipy.core.application import RestiPy


class Request:
    """
    Represents a request to the RestiPy application.

    The `Request` class encapsulates the details of an incoming HTTP request,
    providing access to the request parameters, headers, body, and other
    relevant information. It is used internally by the RestiPy application to
    handle and process incoming requests.

    The `Request` class provides properties and methods to access various
    aspects of the request, such as the HTTP method, path, query parameters,
    headers, and request body. It also handles parsing of the request body,
    including JSON and form data.

    This class is not intended to be used directly by application developers,
    but rather is used internally by the RestiPy framework to handle incoming
    requests.
    """

    __slots__ = (
        '_params',
        '_headers',
        '_files',
        '_form',
        'env',
        'ctx',
    )

    def __init__(self, environ: dict, params: dict = {}) -> None:
        """
        Initializes a new instance of the Request class.

        Args:
            `environ (dict):` The WSGI environment dictionary.
            `params (dict, optional):` The request parameters. Defaults to an
                empty dictionary.
        """
        self.env = environ
        self.ctx: dict[str, t.Any] = {}

        self._params = params
        self._headers: t.Any = None
        self._files: dict[str, UploadedFile] = {}
        self._form: dict[str, t.Any] = {}

    def _read_body(self):
        """
        Reads and returns the request body.

        Returns:
            `bytes or None:` The request body, or None if the request method is
                not 'POST', 'PUT', or 'PATCH'.
        """
        if self.method not in ('POST', 'PUT', 'PATCH'):
            return None

        content_length = self.content_length

        try:
            max_body_size = self.app.config.MAX_BODY_SIZE
        except Exception as e:
            raise HTTPException(
                'Missing MAX_BODY_SIZE in settings.',
                status_code=500,
                code='MISSING_MAX_BODY_SIZE',
            ) from e

        if content_length > max_body_size:
            raise HTTPException(
                'Request body too large.',
                status_code=413,
                code='REQUEST_BODY_TOO_LARGE',
            )

        input = self.env['wsgi.input']
        toread = max(content_length, max_body_size)
        readbytes = 0

        while readbytes < toread:
            chunk = input.read(64 * 1024)
            if not chunk:
                break
            readbytes += len(chunk)
            yield chunk

    def _parse_url_encoded_form(self):
        """
        Parses the URL-encoded form data from the request body.

        If the `_form` attribute is already populated, it returns that.
        Otherwise, it reads the request body using the `_read_body()` method,
        decodes it, and parses the URL-encoded form data into a dictionary,
        which is stored in the `_form` attribute and returned.

        If the request body is invalid, it raises an `HTTPException` with a
        400 status code and a `INVALID_REQUEST_BODY` error code.
        """
        if self._form:
            return self._form

        body = b''

        for chunk in self._read_body():
            body += chunk

        try:
            self._form = dict(parse_qsl(body.decode()))
        except Exception as e:
            raise HTTPException(
                'Invalid request body.',
                status_code=400,
                code='INVALID_REQUEST_BODY',
            ) from e

        if self._form:
            return self._form

    def _get_multipart_message(self):
        """
        Parses the request body as a multipart message using the
        email.parser.BytesFeedParser.

        The method reads the content type from the request and feeds it to the
        parser, followed by the request body. The parser is then closed to
        return the parsed multipart message.

        Returns:
            `email.message.Message:` The parsed multipart message.
        """
        parser = email.parser.BytesFeedParser(policy=email.policy.HTTP)
        parser.feed(('content-type: %s\r\n' % self.content_type).encode())
        parser.feed('\r\n'.encode())

        for chunk in self._read_body():
            parser.feed(chunk)

        return parser.close()

    def _parse_multipart_files(self):
        """
        Parses multipart form data from the request body and returns a
        dictionary of uploaded files.

        If the request body contains multipart form data, this method will
        parse the body and extract any uploaded files. The uploaded files are
        stored in a dictionary, where the keys are the field names from the
        form data, and the values are `UploadedFile` objects containing the
        file metadata and content.

        If the request body does not contain multipart form data, or if there
        are no uploaded files, this method will return an empty dictionary.
        """
        if self._files:
            return self._files

        message = self._get_multipart_message()

        if not message.is_multipart():
            return

        for part in message.iter_parts():  # type: ignore
            if not part.get_filename():
                continue

            name = part.get_param('name', header='content-disposition')

            with tempfile.NamedTemporaryFile(delete=False) as tmp_file:
                shutil.copyfileobj(
                    io.BytesIO(part.get_payload(decode=True)), tmp_file
                )
                self._files[name] = UploadedFile(
                    filename=part.get_filename(),
                    content_type=part.get_content_type(),
                    filepath=tmp_file.name,
                )

        if not self._files:
            return

        return self._files

    def _parse_multipart_form(self):
        """
        Parses the multipart form data from the request body.

        If the form data has already been parsed, it returns the cached form
        data. Otherwise, it reads the request body, parses the multipart
        message, and extracts the form fields. The form fields are stored in
        the `_form` attribute for future access.

        Returns:
            `dict:` A dictionary of the form fields, where the keys are the
                field names and the values are the field values.
        """
        if self._form:
            return self._form

        message = self._get_multipart_message()

        if not message.is_multipart():
            return

        for part in message.iter_parts():  # type: ignore
            if part.get_filename():
                continue

            name = part.get_param('name', header='content-disposition')
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                payload = payload.decode()
            self._form[name] = payload

        if self._form:
            return self._form

    def _parse_json_data(self):
        """
        Parses the request body as JSON data and stores it in the `_form`
        attribute.

        If the request content type is `application/json` or
        `application/json-rpc`, this method attempts to parse the request body
        as JSON. If the parsing is successful, the parsed JSON data is stored
        in the `_form` attribute and returned. If the parsing fails, an
        `HTTPException` with a 400 status code and `INVALID_REQUEST_BODY`
        code is raised.
        """
        if self._form:
            return self._form

        ctype = self.content_type.lower().split(';')[0]

        if ctype not in ('application/json', 'application/json-rpc'):
            return

        body = b''

        for chunk in self._read_body():
            body += chunk

        if not body:
            return

        try:
            self._form = json.loads(body.decode())
        except Exception as e:
            raise HTTPException(
                'Malformed JSON body.',
                status_code=400,
                code='INVALID_REQUEST_BODY',
            ) from e

        return self._form

    @property
    def app(self) -> RestiPy:
        """
        The RestiPy application instance.

        Returns:
            `RestiPy:` The RestiPy application instance.
        """
        return self.env['restipy.app']

    @property
    def params(self) -> dict[str, t.Any]:
        """
        The request parameters.

        Returns:
            `dict:` The request parameters.
        """
        return self._params

    @params.setter
    def set_params(self, params: dict):
        """
        Set the parameters for the request.

        Args:
            `params (dict):` A dictionary containing the parameters to be set.
        """
        self._params = params

    @property
    def json(self) -> dict[str, t.Any] | None:
        """
        Returns the request body parsed as JSON, or `None` if the body is
        empty or not valid JSON.

        Returns:
            `dict[str, t.Any] | None:` The parsed JSON data, or `None` if the
                body is empty or not valid JSON.
        """
        return self._parse_json_data()

    @property
    def form(self) -> dict[str, str] | None:
        """
        Returns the form data from the request, parsed from the request body.

        If the request content type is `multipart/`, the form data is parsed
        from the multipart form. Otherwise, it is parsed from the URL-encoded
        form.

        Returns:
            `dict[str, str] | None:` The form data, or `None` if there is no
                form data.
        """
        if self.content_type.startswith('multipart/'):
            return self._parse_multipart_form()
        return self._parse_url_encoded_form()

    @property
    def files(self):
        return self._parse_multipart_files()

    @property
    def header(self) -> dict[str, str]:
        """
        The request headers.

        Returns:
            `dict:` The request headers.
        """
        if self._headers is None:
            self._headers = {
                env_to_h(k)[5:]: v
                for k, v in self.env.items()
                if k.startswith('HTTP_')
            }
        return self._headers

    @property
    def origin(self) -> str:
        """
        The origin of the request.

        Returns:
            `str:` The origin of the request.
        """
        return f'{self.protocol}://{self.host}'.lower()

    @property
    def url(self) -> str:
        """
        The full URL of the request.

        Returns:
            `str:` The full URL of the request.
        """
        return urljoin(self.origin, self.path).rstrip('/')

    @property
    def href(self) -> str:
        """
        The href of the request.

        Returns:
            `str:` The href of the request.
        """
        return f'{self.url}?{self.env.get("QUERY_STRING", "")}'

    @property
    def method(self) -> str:
        """
        The HTTP method of the request.

        Returns:
            `str:` The HTTP method of the request.
        """
        return self.env.get('REQUEST_METHOD', 'GET').upper()

    @property
    def path(self) -> str:
        """
        The path of the request.

        Returns:
            `str:` The path of the request.
        """
        return self.env.get('PATH_INFO', '/')

    @property
    def query(self) -> dict[str, list[t.Any]] | None:
        """
        The query parameters of the request.

        Returns:
            `dict or None:` The query parameters of the request, or None if
                there are no query parameters.
        """
        qs = self.env.get('QUERY_STRING')
        if qs is None:
            return
        return dict(parse_qsl(qs))

    @property
    def host(self) -> None:
        """
        The host of the request.

        Returns:
            `str or None:` The host of the request.
        """
        return self.env.get('HTTP_HOST')

    @property
    def charset(self) -> str | None:
        """
        The charset of the request.

        Returns:
            `str or None:` The charset of the request.
        """
        if self.content_type is None:
            return None
        for part in self.content_type.split(';'):
            if 'charset=' in part:
                return part.split('=')[1].strip()
        return None

    @property
    def content_length(self) -> int:
        """
        The content length of the request.

        Returns:
            `int:` The content length of the request.
        """
        return int(self.env.get('CONTENT_LENGTH', '0'))

    @property
    def protocol(self) -> str:
        """
        The protocol of the request.

        Returns:
            `str:` The protocol of the request.
        """
        return self.env.get('wsgi.url_scheme', 'http').upper()

    @property
    def secure(self) -> bool:
        """
        Whether the request is secure (HTTPS).

        Returns:
            `bool:` True if the request is secure, False otherwise.
        """
        return self.protocol == 'HTTPS'

    @property
    def accept(self) -> str | None:
        """
        The accept header of the request.

        Returns:
            `str or None:` The accept header of the request.
        """
        accept = self.env.get('HTTP_ACCEPT')
        if accept:
            return accept.lower()
        return accept

    @property
    def content_type(self) -> str:
        """
        The content type of the request.

        Returns:
            `str:` The content type of the request.
        """
        return self.env.get('CONTENT_TYPE', '')

    def __repr__(self) -> str:
        """
        Returns a string representation of the Request object.

        Returns:
            `str:` A string representation of the Request object.
        """
        return f'<Request {self.method} {self.path}>'
