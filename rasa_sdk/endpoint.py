import argparse
import logging
import os
import types
import zlib
import json
from typing import List, Text, Union, Optional
from ssl import SSLContext
import requests

from sanic import Sanic, response
from sanic.response import HTTPResponse
from sanic.request import Request
from sanic_cors import CORS

from rasa_sdk import utils
from rasa_sdk.cli.arguments import add_endpoint_arguments
from rasa_sdk.constants import DEFAULT_SERVER_PORT
from rasa_sdk.executor import ActionExecutor
from rasa_sdk.interfaces import ActionExecutionRejection, ActionNotFoundException
from rasa_sdk.plugin import plugin_manager

logger = logging.getLogger(__name__)


def configure_cors(
    app: Sanic, cors_origins: Union[Text, List[Text], None] = ""
) -> None:
    """Configure CORS origins for the given app."""

    CORS(
        app, resources={r"/*": {"origins": cors_origins or ""}}, automatic_options=True
    )


def create_ssl_context(
    ssl_certificate: Optional[Text],
    ssl_keyfile: Optional[Text],
    ssl_password: Optional[Text] = None,
) -> Optional[SSLContext]:
    """Create a SSL context if a certificate is passed."""

    if ssl_certificate:
        import ssl

        ssl_context = ssl.create_default_context(purpose=ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(
            ssl_certificate, keyfile=ssl_keyfile, password=ssl_password
        )
        return ssl_context
    else:
        return None


def create_argument_parser():
    """Parse all the command line arguments for the run script."""

    parser = argparse.ArgumentParser(description="starts the action endpoint")
    add_endpoint_arguments(parser)
    utils.add_logging_level_option_arguments(parser)
    utils.add_logging_file_arguments(parser)
    return parser


def create_app(
    action_package_name: Union[Text, types.ModuleType],
    cors_origins: Union[Text, List[Text], None] = "*",
    auto_reload: bool = False,
    enable_forwarding: bool = False,
    forward_ip: str = "0.0.0.0",
    forward_port: int = 5056
) -> Sanic:
    """Create a Sanic application and return it.

    Args:
        action_package_name: Name of the package or module to load actions
            from.
        cors_origins: CORS origins to allow.
        auto_reload: When `True`, auto-reloading of actions is enabled.
        enable_forwarding: Whether to enable forwarding to another action server.
        forward_ip: IP address of the second action server.
        forward_port: Port of the second action server.

    Returns:
        A new Sanic application ready to be run.
    """
    app = Sanic("rasa_sdk", configure_logging=False)

    configure_cors(app, cors_origins)

    executor = ActionExecutor()
    executor.register_package(action_package_name)

    @app.get("/health")
    async def health(_) -> HTTPResponse:
        """Ping endpoint to check if the server is running and well."""
        body = {"status": "ok"}
        return response.json(body, status=200)

    @app.post("/webhook")
    async def webhook(request: Request) -> HTTPResponse:
        """Webhook to retrieve action calls."""
        if request.headers.get("Content-Encoding") == "deflate":
            # Decompress the request data using zlib
            decompressed_data = zlib.decompress(request.body)
            # Load the JSON data from the decompressed request data
            action_call = json.loads(decompressed_data)
        else:
            action_call = request.json
        if action_call is None:
            body = {"error": "Invalid body request"}
            return response.json(body, status=400)

        utils.check_version_compatibility(action_call.get("version"))

        if auto_reload:
            executor.reload()

        try:
            result = await executor.run(action_call)
            # print("Result from executor:", result)  # Print the result
        except ActionExecutionRejection as e:
            logger.debug(e)
            body = {"error": e.message, "action_name": e.action_name}
            return response.json(body, status=400)
        except ActionNotFoundException as e:
            if enable_forwarding:
                try:
                    # Forward the request to another server
                    forward_url = f"http://{forward_ip}:{forward_port}/webhook"
                    reply = requests.post(forward_url, json=action_call)
                    reply.raise_for_status()
                    result_from_other_server = reply.json()
                    result = result_from_other_server
                    # print("Result from other server:", result_from_other_server)  # Print the result
                except requests.RequestException as ex:
                    logger.error(f"Forwarding request failed: {ex}")
                    body = {"error": "Forwarding request failed", "action_name": e.action_name}
                    return response.json(body, status=504)
            else:
                logger.error(e)
                body = {"error": e.message, "action_name": e.action_name}
                return response.json(body, status=404)

        return response.json(result, status=200)

    @app.get("/actions")
    async def actions(_) -> HTTPResponse:
        """List all registered actions."""
        if auto_reload:
            executor.reload()

        body = [{"name": k} for k in executor.actions.keys()]
        return response.json(body, status=200)

    @app.exception(Exception)
    async def exception_handler(request, exception: Exception):
        logger.error(
            msg=f"Exception occurred during execution of request {request}",
            exc_info=exception,
        )
        body = {"error": str(exception), "request_body": request.json}
        return response.json(body, status=500)

    return app


def run(
    action_package_name: Union[Text, types.ModuleType],
    port: int = DEFAULT_SERVER_PORT,
    cors_origins: Union[Text, List[Text], None] = "*",
    ssl_certificate: Optional[Text] = None,
    ssl_keyfile: Optional[Text] = None,
    ssl_password: Optional[Text] = None,
    auto_reload: bool = False,
    enable_forwarding: bool = False,
    forward_ip: str = "0.0.0.0",
    forward_port: int = 5056
) -> None:
    """Starts the action endpoint server with given config values."""
    logger.info("Starting action endpoint server...")
    app = create_app(
        action_package_name, cors_origins=cors_origins, auto_reload=auto_reload,
        enable_forwarding=enable_forwarding, forward_ip=forward_ip, forward_port=forward_port
    )
    ## Attach additional sanic extensions: listeners, middleware and routing
    logger.info("Starting plugins...")
    plugin_manager().hook.attach_sanic_app_extensions(app=app)
    ssl_context = create_ssl_context(ssl_certificate, ssl_keyfile, ssl_password)
    protocol = "https" if ssl_context else "http"
    host = os.environ.get("SANIC_HOST", "0.0.0.0")

    logger.info(f"Action endpoint is up and running on {protocol}://{host}:{port}")
    if enable_forwarding:
        logger.info(f"Forwarding to second Action endpoint on {protocol}://{forward_ip}:{forward_port}")
    app.run(host, port, ssl=ssl_context, workers=utils.number_of_sanic_workers())


if __name__ == "__main__":
    import rasa_sdk.__main__

    rasa_sdk.__main__.main()
