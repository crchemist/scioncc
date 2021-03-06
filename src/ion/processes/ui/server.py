#!/usr/bin/env python

"""Provides a general purpose HTTP server that can be configured with content location,
service gateway and web sockets"""

__author__ = 'Michael Meisinger, Stephen Henrie'

import flask
from flask import Flask, Response, request, render_template
from flask_socketio import SocketIO, SocketIOServer
from flask_oauthlib.provider import OAuth2Provider
import gevent
from gevent.wsgi import WSGIServer
from datetime import datetime, timedelta

from pyon.public import StandaloneProcess, log, BadRequest, NotFound, OT, get_ion_ts_millis
from pyon.util.containers import named_any, current_time_millis
from ion.util.ui_utils import build_json_response, build_json_error, get_arg, get_auth, set_auth, clear_auth, OAuthClientObj, OAuthTokenObj

from interface.services.core.iidentity_management_service import IdentityManagementServiceProcessClient
from interface.objects import SecurityToken, TokenTypeEnum

DEFAULT_WEB_SERVER_HOSTNAME = ""
DEFAULT_WEB_SERVER_PORT = 4000
DEFAULT_SESSION_TIMEOUT = 600
DEFAULT_GATEWAY_PREFIX = "/service"

CFG_PREFIX = "process.ui_server"

# Initialize the main Flask app
app = Flask("ui_server", static_folder=None, template_folder=None)
oauth = OAuth2Provider(app)
ui_instance = None
client_cache = {}


class UIServer(StandaloneProcess):
    """
    Process to start a generic UI server that can be extended with content and service gateway
    """
    def on_init(self):
        # Retain a pointer to this object for use in routes
        global ui_instance
        ui_instance = self

        # Main references to basic components (if initialized)
        self.http_server = None
        self.socket_io = None
        self.service_gateway = None
        self.oauth = oauth

        # Configuration
        self.server_enabled = self.CFG.get_safe(CFG_PREFIX + ".server.enabled") is True
        self.server_debug = self.CFG.get_safe(CFG_PREFIX + ".server.debug") is True
        # Note: this may be the empty string. Using localhost does not make the server publicly accessible
        self.server_hostname = self.CFG.get_safe(CFG_PREFIX + ".server.hostname", DEFAULT_WEB_SERVER_HOSTNAME)
        self.server_port = self.CFG.get_safe(CFG_PREFIX + ".server.port", DEFAULT_WEB_SERVER_PORT)
        self.server_log_access = self.CFG.get_safe(CFG_PREFIX + ".server.log_access") is True
        self.server_log_errors = self.CFG.get_safe(CFG_PREFIX + ".server.log_errors") is True
        self.server_socket_io = self.CFG.get_safe(CFG_PREFIX + ".server.socket_io") is True
        self.server_secret = self.CFG.get_safe(CFG_PREFIX + ".security.secret") or ""
        self.session_timeout = int(self.CFG.get_safe(CFG_PREFIX + ".security.session_timeout") or DEFAULT_SESSION_TIMEOUT)
        self.set_cors_headers = self.CFG.get_safe(CFG_PREFIX + ".server.set_cors") is True
        self.develop_mode = self.CFG.get_safe(CFG_PREFIX + ".server.develop_mode") is True

        self.oauth_enabled = self.CFG.get_safe(CFG_PREFIX + ".oauth.enabled") is True
        self.oauth_scope = self.CFG.get_safe(CFG_PREFIX + ".oauth.default_scope") or "scioncc"

        self.has_service_gateway = self.CFG.get_safe(CFG_PREFIX + ".service_gateway.enabled") is True
        self.service_gateway_prefix = self.CFG.get_safe(CFG_PREFIX + ".service_gateway.url_prefix", DEFAULT_GATEWAY_PREFIX)
        self.extensions = self.CFG.get_safe(CFG_PREFIX + ".extensions") or []
        self.extension_objs = []

        # TODO: What about https?
        self.base_url = "http://%s:%s" % (self.server_hostname or "localhost", self.server_port)
        self.gateway_base_url = None

        self.idm_client = IdentityManagementServiceProcessClient(process=self)

        # One time setup
        if self.server_enabled:
            app.secret_key = self.server_secret or self.__class__.__name__   # Enables encrypted session cookies

            if self.server_debug:
                app.debug = True

            if self.server_socket_io:
                self.socket_io = SocketIO(app)

            if self.has_service_gateway:
                from ion.services.service_gateway import ServiceGateway, sg_blueprint
                self.gateway_base_url = self.base_url + self.service_gateway_prefix
                self.service_gateway = ServiceGateway(process=self, config=self.CFG, response_class=app.response_class)

                app.register_blueprint(sg_blueprint, url_prefix=self.service_gateway_prefix)

            for ext_cls in self.extensions:
                try:
                    cls = named_any(ext_cls)
                except AttributeError as ae:
                    # Try to nail down the error
                    import importlib
                    importlib.import_module(ext_cls.rsplit(".", 1)[0])
                    raise

                self.extension_objs.append(cls())

            for ext_obj in self.extension_objs:
                ext_obj.on_init(self, app)
            if self.extensions:
                log.info("UI Server: %s extensions initialized", len(self.extensions))

            # Start the web server
            self.start_service()

            log.info("UI Server: Started server on %s" % self.base_url)

        else:
            log.warn("UI Server: Server disabled in config")

    def on_quit(self):
        self.stop_service()

    def start_service(self):
        """Starts the web server."""
        if self.http_server is not None:
            self.stop_service()

        if self.server_socket_io:
            self.http_server = SocketIOServer((self.server_hostname, self.server_port),
                                              app.wsgi_app,
                                              resource='socket.io',
                                              log=None)
            self.http_server._gl = gevent.spawn(self.http_server.serve_forever)
            log.info("UI Server: Providing web sockets (socket.io) server")
        else:
            self.http_server = WSGIServer((self.server_hostname, self.server_port),
                                          app,
                                          log=None)
            self.http_server.start()

        if self.service_gateway:
            self.service_gateway.start()
            log.info("UI Server: Service Gateway started on %s", self.gateway_base_url)

        for ext_obj in self.extension_objs:
            ext_obj.on_start()

        return True

    def stop_service(self):
        """Responsible for stopping the gevent based web server."""
        for ext_obj in self.extension_objs:
            ext_obj.on_stop()

        if self.http_server is not None:
            self.http_server.stop()

        if self.service_gateway:
            self.service_gateway.stop()

        # Need to terminate the server greenlet?
        return True

    # -------------------------------------------------------------------------
    # Authentication

    def auth_external(self, username, ext_user_id, ext_id_provider="ext"):
        """
        Given username and user identifier from an external identity provider (IdP),
        retrieve actor_id and establish user session. Return user info from session.
        Convention is that system local username is ext_id_provider + ":" + username,
        e.g. "ext_johnbean"
        Return NotFound if user not registered in system. Caller can react and create
        a user account through the normal system means
        @param username  the user name the user recognizes.
        @param ext_user_id  a unique identifier coming from the external IdP
        @param ext_id_provider  identifies the external IdP service
        """
        try:
            if ext_user_id and ext_id_provider and username:
                local_username = "%s_%s" % (ext_id_provider, username)
                actor_id = self.idm_client.find_actor_identity_by_username(local_username)
                user_info = self._get_user_info(actor_id, local_username)

                return build_json_response(user_info)

            else:
                raise BadRequest("External user info missing")

        except Exception:
            return build_json_error()

    def login(self):
        try:
            username = get_arg("username")
            password = get_arg("password")
            if username and password:
                actor_id = self.idm_client.check_actor_credentials(username, password)
                user_info = self._get_user_info(actor_id, username)
                return build_json_response(user_info)

            else:
                raise BadRequest("Username or password missing")

        except Exception:
            return build_json_error()

    def _get_user_info(self, actor_id, username=None):
        actor_user = self.idm_client.read_identity_details(actor_id)
        if actor_user.type_ != OT.UserIdentityDetails:
            raise BadRequest("Bad identity details")

        full_name = actor_user.contact.individual_names_given + " " + actor_user.contact.individual_name_family

        valid_until = int(get_ion_ts_millis() / 1000 + self.session_timeout)
        set_auth(actor_id, username, full_name, valid_until=valid_until, roles=actor_user.contact.roles)
        user_info = get_auth()
        return user_info

    def get_session(self):
        try:
            # Get session based on OAuth token
            auth_hdr = request.headers.get("authorization", None)
            if auth_hdr:
                valid, req = self.oauth.verify_request([self.oauth_scope])
                if valid:
                    actor_id = flask.g.oauth_user.get("actor_id", "")
                    actor_user = self.idm_client.read_actor_identity(actor_id)
                    session_attrs = dict(is_logged_in=True, is_registered=True, attributes={"roles":actor_user.details.contact.roles}, roles={})
                    if actor_user.session:
                        session_attrs.update(actor_user.session)

                    return build_json_response(session_attrs)

            # Support quick reload
            access_token = flask.session.get("access_token", None)
            actor_id = flask.session.get("actor_id", None)
            if access_token and actor_id:
                actor_user = self.idm_client.read_actor_identity(actor_id)
                session_attrs = dict(access_token=access_token, is_logged_in=True, is_registered=True, attributes={"roles":actor_user.details.contact.roles}, roles={})
                if actor_user.session:
                    session_attrs.update(actor_user.session)

                return build_json_response(session_attrs)

            # Get session from Flask session and cookie
            user_info = get_auth()
            if 0 < int(user_info.get("valid_until", 0)) * 1000 < current_time_millis():
                clear_auth()
                user_info = get_auth()
            return build_json_response(user_info)
        except Exception:
            return build_json_error()

    def logout(self):
        try:
            clear_auth()
            return build_json_response("OK")
        except Exception:
            return build_json_error()


def enable_cors(resp):
    if ui_instance.develop_mode and ui_instance.set_cors_headers:
        if isinstance(resp, basestring):
            resp = Response(resp)
        elif isinstance(resp, tuple):
            resp, status_code = resp
            resp.status_code = status_code
        resp.headers["Access-Control-Allow-Headers"] = "Origin, X-Atmosphere-tracking-id, X-Atmosphere-Framework, X-Cache-Date, Content-Type, X-Atmosphere-Transport, *"
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS , PUT"
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Request-Headers"] = "Origin, X-Atmosphere-tracking-id, X-Atmosphere-Framework, X-Cache-Date, Content-Type, X-Atmosphere-Transport,  *"
    return resp


# -------------------------------------------------------------------------
# Authentication routes

@app.route('/auth/login', methods=['POST'])
def login_route():
    return enable_cors(ui_instance.login())


@app.route('/auth/session', methods=['GET', 'POST'])
def session_route():
    return enable_cors(ui_instance.get_session())


@app.route('/auth/logout', methods=['GET', 'POST'])
def logout_route():
    return enable_cors(ui_instance.logout())


# -------------------------------------------------------------------------
# OAuth2 routes and callbacks

@oauth.clientgetter
def load_client(client_id):
    log.info("OAuth:load_client(%s)", client_id)
    client = None
    if client_id and client_id in client_cache:
        client = client_cache[client_id]
    try:
        if client_id and not client:
            actor_id = ui_instance.idm_client.find_actor_identity_by_username("client:" + client_id)
            actor_obj = ui_instance.idm_client.read_actor_identity(actor_id)
            client = OAuthClientObj.from_actor_identity(actor_obj)
            client_cache[client_id] = client
    except (NotFound, BadRequest):
        pass
    if client:
        flask.g.client_id = client_id
    return client

@oauth.usergetter
def get_user(username, password, *args, **kwargs):
    # Required for password credential authentication
    log.info("OAuth:get_user(%s)", username)
    if username and password:
        try:
            actor_id = ui_instance.idm_client.check_actor_credentials(username, password)
            actor_user = ui_instance.idm_client.read_actor_identity(actor_id)
            if actor_user.details.type_ != OT.UserIdentityDetails:
                log.warn("Bad identity details")
                return None

            full_name = actor_user.details.contact.individual_names_given + " " + actor_user.details.contact.individual_name_family
            valid_until = int(get_ion_ts_millis() / 1000 + ui_instance.session_timeout)
            user_session = {"actor_id": actor_id, "user_id": actor_id, "username": username, "full_name": full_name, "valid_until": valid_until}
            actor_user.session = user_session
            ui_instance.container.resource_registry.update(actor_user)
            flask.g.actor_id = actor_id
            return user_session
        except NotFound:
            pass
    return None

@oauth.grantgetter
def load_grant(client_id, code):
    print "### load_grant", client_id, code
    return {}

@oauth.grantsetter
def save_grant(client_id, code, request, *args, **kwargs):
    """Saves a grant made by the user on provider to client"""
    print "### save_grant", client_id, code
    # decide the expires time yourself
    expires = datetime.utcnow() + timedelta(seconds=100)
    grant = {}
    return grant

@oauth.tokengetter
def load_token(access_token=None, refresh_token=None):
    log.info("OAuth:load_token access=%s refresh=%s", access_token, refresh_token)
    try:
        if access_token:
            token_id = str("access_token_%s" % access_token)
            token_obj = ui_instance.container.object_store.read(token_id)
            token = OAuthTokenObj.from_security_token(token_obj)
            flask.g.oauth_user = token.user
            flask.g.actor_id = token.user["actor_id"]
            flask.session["access_token"] = access_token
            flask.session["actor_id"] = token.user["actor_id"]
            return token
        elif refresh_token:
            token_id = str("refresh_token_%s" % refresh_token)
            token_obj = ui_instance.container.object_store.read(token_id)
            token = OAuthTokenObj.from_security_token(token_obj)
            flask.g.oauth_user = token.user
            flask.g.actor_id = token.user["actor_id"]
            return token
    except NotFound:
        pass
    return None

@oauth.tokensetter
def save_token(token, request, *args, **kwargs):
    log.info("OAuth:save_token=%s", token)
    expires = str(get_ion_ts_millis() + 1000*token["expires_in"])
    actor_id = flask.g.actor_id
    access_token_str = str(token["access_token"])
    refresh_token_str = str(token["refresh_token"])
    scopes = str(token["scope"])

    # Access token
    token_obj = SecurityToken(token_type=TokenTypeEnum.OAUTH_ACCESS, token_string=access_token_str,
                              expires=expires, status="OPEN", actor_id=actor_id,
                              attributes=dict(access_token=access_token_str, refresh_token=refresh_token_str,
                                              scopes=scopes, client_id=flask.g.client_id))
    token_id = "access_token_%s" % access_token_str
    ui_instance.container.object_store.create(token_obj, token_id)

    # Refresh token
    token_obj = SecurityToken(token_type=TokenTypeEnum.OAUTH_REFRESH, token_string=refresh_token_str,
                              expires=expires, status="OPEN", actor_id=actor_id,
                              attributes=dict(access_token=access_token_str, refresh_token=refresh_token_str,
                                              scopes=scopes, client_id=flask.g.client_id))
    token_id = "refresh_token_%s" % refresh_token_str
    ui_instance.container.object_store.create(token_obj, token_id)

@app.route('/oauth/authorize', methods=['GET', 'POST'])
#@require_login
@oauth.authorize_handler
def authorize(*args, **kwargs):
    if request.method == 'GET':
        client_id = kwargs.get('client_id')
        client = {}
        kwargs['client'] = client
    template = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Authorization</title>
</head>
<body>
  <p>Client: {{ client.client_id }}</p>
  <p>User: {{ user.username }}</p>
  <form action="/oauth/authorize" method="post">
    <p>Allow access?</p>
    <input type="hidden" name="client_id" value="{{ client.client_id }}">
    <input type="hidden" name="scope" value="{{ scopes|join(' ') }}">
    <input type="hidden" name="response_type" value="{{ response_type }}">
    <input type="hidden" name="redirect_uri" value="{{ redirect_uri }}">
    {% if state %}
    <input type="hidden" name="state" value="{{ state }}">
    {% endif %}
    <input type="submit" name="confirm" value="yes">
    <input type="submit" name="confirm" value="no">
  </form>
</body>
"""
    return None

@app.route('/oauth/token', methods=['POST'])
@oauth.token_handler
def access_token():
    return None
