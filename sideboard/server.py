from __future__ import unicode_literals
import os
import ssl
import sys

import cherrypy
import ldap3

import sideboard
from sideboard.internal import connection_checker
from sideboard.jsonrpc import _make_jsonrpc_handler
from sideboard.websockets import WebSocketDispatcher, WebSocketRoot
from sideboard.lib import log, listify, config, render_with_templates, services, threadlocal


def jsonrpc_auth(body):
    if 'username' not in cherrypy.session:
        raise cherrypy.HTTPError(401, 'not logged in')


def ldap_auth(username, password):
    if not username or not password:
        return False

    try:
        ssl_material = (
            config['ldap.cacert'], config['ldap.cert'], config['ldap.key']
        )
        server_kwargs = {}
        tls_kwargs = {}

        if config['ldap.url'].startswith('ldaps') or any(ssl_material):
            server_kwargs['use_ssl'] = True
        else:
            server_kwargs['use_ssl'] = False
        server_kwargs['host'] = config['ldap.url']

        if config['ldap.cacert']:
            tls_kwargs['ca_certs_file'] = config['ldap.cacert']
            # if we specify a CA certs file, assume we want to validate it
            tls_kwargs['validate'] = ssl.CERT_REQUIRED

        if tls_kwargs:
            server_kwargs['tls'] = ldap3.Tls(**tls_kwargs)

        server = ldap3.Server(**server_kwargs)

    except:
        log.error('Error initializing LDAP server', exc_info=True)
        raise

    # attempt to bind on each base DN that was configured
    for basedn in listify(config['ldap.basedn']):
        dn = '{}={},{}'.format(config['ldap.userattr'], username, basedn)
        log.debug('attempting to bind with dn {}', dn)
        try:
            connection = ldap3.Connection(server, user=dn, password=password)
            connection.start_tls()
            is_bound = connection.bind()
        except:
            log.warning("Error binding to LDAP server with dn", exc_info=True)
            raise

        if is_bound:
            return True

    # we couldn't auth on anything
    return False


@render_with_templates(config['template_dir'])
class Root(object):
    def default(self, *args, **kwargs):
        raise cherrypy.HTTPRedirect(config['default_url'])

    def logout(self, return_to='/'):
        cherrypy.session.pop('username', None)
        raise cherrypy.HTTPRedirect('login?return_to=%s' % return_to)

    def login(self, username='', password='', message='', return_to=''):
        if username:
            if ldap_auth(username, password):
                cherrypy.session['username'] = username
                raise cherrypy.HTTPRedirect(return_to)
            else:
                message = 'Invalid credentials'
        
        return {
            'message': message,
            'username': username,
            'return_to': return_to
        }

    def list_plugins(self):
        from sideboard.internal.imports import _module_cache
        plugins = {}

        for plugin, modules in _module_cache.items():
            if plugin not in plugins:
                plugins[plugin] = {
                    'name': ' '.join(plugin.split('_')).title(),
                    'version': getattr(modules[plugin], '__version__', None),
                    'paths': []
                }
        for path, app in cherrypy.tree.apps.items():
            if path:  # exclude what Sideboard itself mounts
                plugin = app.root.__module__.split('.')[0]
                plugins[plugin]['paths'].append(path)

        return {
            'plugins': plugins,
            'version': getattr(sideboard, '__version__', None)
        }

    def connections(self):
        return {'connections': connection_checker.check_all()}

    ws = WebSocketRoot()
    wsrpc = WebSocketRoot()

    json = _make_jsonrpc_handler(services.get_services(), precall=jsonrpc_auth)
    jsonrpc = _make_jsonrpc_handler(services.get_services(),
                                   precall=lambda body: threadlocal.reset(
                                       username=cherrypy.session.get('username'),
                                       client=body.get('websocket_client')))


class SideboardWebSocket(WebSocketDispatcher):
    """
    This web socket handler will be used by browsers connecting to Sideboard web
    sites.  Therefore, the authentication mechanism is the default approach
    of checking the session for a username and rejecting unauthenticated users.
    """
    services = services.get_services()

    @classmethod
    def check_authentication(cls):
        host, origin = cherrypy.request.headers['host'], cherrypy.request.headers['origin']
        if ('//' + host) not in origin:
            log.error('Javascript websocket connections must follow same-origin policy; origin {!r} does not match host {!r}', origin, host)
            raise ValueError('Origin and Host headers do not match')

        if config['ws.auth_required'] and 'username' not in cherrypy.session:
            log.warning('websocket connections to this address must have a valid session')
            raise ValueError('you are not logged in')

        return cherrypy.session.get('username', '<UNAUTHENTICATED>')


class SideboardRpcWebSocket(SideboardWebSocket):
    """
    This web socket handler will be used by programs wishing to call services
    exposed by plugins over web socket connections.  There is no authentication
    performed because we assume that our external-facing web server will require
    a valid client cert to access this resource.
    """

    @classmethod
    def check_authentication(cls):
        return 'rpc'


def reset_threadlocal():
    threadlocal.reset(username=cherrypy.session.get('username'))

cherrypy.tools.reset_threadlocal = cherrypy.Tool('before_handler', reset_threadlocal)


app_config = {
    '/static': {
        'tools.staticdir.on': True,
        'tools.staticdir.dir': os.path.join(config['module_root'], 'static')
    },
    '/ws': {
        'tools.websockets.on': True,
        'tools.websockets.handler_cls': SideboardWebSocket
    },
    '/wsrpc': {
        'tools.websockets.on': True,
        'tools.websockets.handler_cls': SideboardRpcWebSocket
    }
}
if config['debug']:
    app_config['/docs'] = {
        'tools.staticdir.on': True,
        'tools.staticdir.dir': os.path.join(config['module_root'], 'docs', 'html'),
        'tools.staticdir.index': 'index.html'
    }
cherrypy_config = {}
for setting, value in config['cherrypy'].items():
    if isinstance(value, basestring):
        if value.isdigit():
            value = int(value)
        elif value.lower() in ['true', 'false']:
            value = value.lower() == 'true'
        else:
            value = value.encode('utf-8')
    cherrypy_config[setting] = value
cherrypy.config.update(cherrypy_config)


# on Python 2, we need bytestrings for CherryPy config, see https://bitbucket.org/cherrypy/cherrypy/issue/1184
def recursive_coerce(d):
    if isinstance(d, dict):
        for k, v in d.items():
            if sys.version_info[:2] == (2, 7) and isinstance(k, unicode):
                del d[k]
                d[k.encode('utf-8')] = recursive_coerce(v)
    return d


def mount(root, script_name='', config=None):
    assert script_name not in cherrypy.tree.apps, '{} has already been mounted, probably by another plugin'.format(script_name)
    return orig_mount(root, script_name.encode(), recursive_coerce(config))

orig_mount = cherrypy.tree.mount
cherrypy.tree.mount = mount
cherrypy.tree.mount(Root(), '', app_config)
