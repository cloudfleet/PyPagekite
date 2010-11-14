#!/usr/bin/python -u
#
# pagekite.py, Copyright 2010, the Beanstalks Project ehf.
#                                  and Bjarni Runar Einarsson
#
# FIXME: Implement epoll() support.
# FIXME: Stress test this thing: when do we need a C rewrite?
# FIXME: Make multi-process, use the FD-over-socket trick? Threads=>GIL=>bleh
# FIXME: Add XMPP and incoming SMTP support.
# FIXME: Add a basic HTTP and HTTPS server for configuring, monitoring and
#        proof of concept. 
# FIXME: Add throttling, bandwidth shaping and auto-slowdown for freebies?
# FIXME: Add support for dedicated ports (PageKitePNP, ha ha).
# FIXME: Add direct (un-tunneled) proxying as well.
# FIXME: Create a derivative BaseHTTPServer which doesn't actually listen()
#        on a real socket, but instead communicates with the tunnel directly.
# FIXME: Add a scheduler for deferred/periodic processing.
# FIXME: Move DynDNS updates to a separate thread, blocking on them is dumb.
# FIXME: Security: Add same-origin cookie enforcement to front-end. Or is
#        that pointless due to Javascript side-channels?
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
#
##[ Hacking guide! ]############################################################
#
# Hello! Welcome to my source code.
#
# Here's a brief intro to how the program is structured, to encourage people
# to hack and improve.
#
#  * The PageKite object contains the master configuration and some related
#    routines. It takes care of parsing configuration files and implements
#    things like the authentication protocol. It also contains the main event
#    loop, which is select() or epoll() based. In short, it's the boss.
#
#  * The Connections object keeps track of which tunnels and user connections
#    are open at any given time and which protocol/domain pairs they belong to.
#    It gets passed around as an argument quite a lot - not too elegant.
#
#  * The Selectable and it's *Parser subclasses incrementally build up basic
#    parsers for the supported protocols. Note that none of the protocols
#    are fully implemented, we only implement the bare minimum required to
#    figure out which back-end should handle a given request, and then forward
#    the bytes unmodified over that channel. As a result, the current HTTP
#    proxy code is not HTTP 1.1 compliant - but if you put it behind Varnish
#    or some other decent reverse-proxy, then *the combination* is.
#
#  * The UserConn object represents connections on behalf of users. It can
#    be created as a FrontEnd, which will find the right tunnel and send
#    traffic to the back-end PageKite process, where a BackEnd UserConn
#    will be created to connect to the actual HTTP server.
# 
#  * The Tunnel object represents one end of a PageKite tunnel and is also
#    created either as a FrontEnd or BackEnd, depending on which end it is.
#    Tunnels handle multiplexing and demultiplexing all the traffic for
#    a given back-end so multiple requests can share a single TCP/IP
#    connection.
#
# Although most of the work done by pagekite.py happens in an event-loop 
# on a single thread, there are some exceptions:
#
#  * The AuthThread handles checking whether an incoming tunnel request is
#    allowed or not; authentication requests may end up blocking and waiting
#    for each other, but the main work of proxying data back and forth won't
#    be blocked.
#
#  * The HttpUiThread implements a basic HTTP (or HTTPS) server, for basic
#    monitoring and interactive configuration.
#
# WARNING: The UI threading code assumes it is running in CPython, where the
#          GIL makes snooping across the thread-boundary relatively safe, even
#          without explicit locking. Beware!
#
#
PROTOVER = '0.8'
APPVER = '0.3.5'
AUTHOR = 'Bjarni Runar Einarsson, http://bre.klaki.net/'
WWWHOME = 'http://pagekite.net/'
DOC = """\
pagekite.py is Copyright 2010, the Beanstalks Project ehf. 
     v%s                         http://pagekite.net/

This the reference implementation of the PageKite tunneling protocol,
both the front- and back-end. This following protocols are supported:

  HTTP    - HTTP 1.1 only, requires a valid HTTP Host: header
  HTTPS   - Recent versions of TLS only, requires the SNI extension.
  XMPP    - ...unfinished... (FIXME)
  SMTP    - ...unfinished... (FIXME)

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License. For the full text of the
license, see: http://www.gnu.org/licenses/agpl-3.0.html

Usage:

  pagekite.py [options]

Common Options:

 --optfile=X    -o X    Read options from file X. Default is ~/.pagekiterc.
 --httpd=X:P    -H X:P  Enable the HTTP user interface on hostname X, port P.
 --pemfile=X    -P X    Use X as a PEM key for the HTTPS UI. (FIXME)
 --httppass=X   -X X    Require password X to access the UI.
 --nozchunks            Disable zlib tunnel compression.
 --buffers       N      Buffer at most N kB of back-end data before blocking.
 --logfile=F    -L F    Log to file F.
 --daemonize    -Z      Run as a daemon.
 --runas        -U U:G  Set UID:GID after opening our listening sockets.
 --pidfile=P    -I P    Write PID to the named file.
 --clean                Skip loading the default configuration file.              
 --nocrashreport        Don't send anonymous crash reports to PageKite.net.
 --defaults             Set some reasonable default setings.
 --settings             Dump the current settings to STDOUT, formatted as
                       an options file would be.

Front-end Options:

 --isfrontend   -f      Enable front-end mode.
 --authdomain=X -A X    Use X as a remote authentication domain.
 --host=H       -h H    Listen on H (hostname).
 --ports=A,B,C  -p A,B  Listen on ports A, B, C, ...
 --protos=A,B,C         Accept the listed protocols for tunneling.

 --domain=proto,proto2,pN:domain:secret
                  Accept tunneling requests for the named protocols and
                 specified domain, using the given secret.  A * may be
               used as a wildcard for subdomains. (FIXME)

Back-end Options:

 --all          -a      Terminate early if any tunnels fail to register.
 --dyndns=X     -D X    Register changes with DynDNS provider X.  X can either
                       be simply the name of one of the 'built-in' providers,
                      or a URL format string for ad-hoc updating.

 --frontends=N:X:P      Choose N front-ends from X (a DNS domain name), port P.
 --frontend=host:port   Connect to the named front-end server.
 --new          -N      Don't attempt to connect to the domain's old front-end.           
 --socksify=S:P         Connect via SOCKS server S, port P (requires socks.py)
 --torify=S:P           Same as socksify, but more paranoid.

 --backend=proto:domain:host:port:secret
                  Configure a back-end service on host:port, using
                 protocol proto and the given domain. As a special
                case, if host and port are left blank and the proto
               is HTTP or HTTPS, the built-in server will be used.

About the options file:

The options file contains the same options as are available to the command
line, with the restriction that there be exactly one "argument" per line.

The leading '--' may also be omitted for readability, and for the same reason
it is recommended to use the long form of the options in the configuration
file (also, as the short form may not always parse correctly).

Blank lines and lines beginning with # (comments) are stripped from the
options file before it is parsed.  It is perfectly acceptable to have multiple
options files, and options files can include other options files.


Examples:

# Create a config-file with default options, and then edit it.
pagekite.py --defaults --settings > ~/.pagekite.rc
vim ~/.pagekite.rc 

# Run pagekite with the HTTP UI, for interactive configuration.
pagekite.py --httpd=localhost:8888
firefox http://localhost:8888/

# Fly a PageKite on pagekite.net for somedomain.com, and register the new
# front-ends with the No-IP Dynamic DNS provider.
pagekite.py \\
       --frontends=1:frontends.b5p.us:2222 \\
       --dyndns=user:pass@no-ip.com \\
       --backend=http:somedomain.com:localhost:80:mygreatsecret

""" % APPVER

MAGIC_PREFIX = '/~:PageKite:~/'
MAGIC_PATH = '%sv%s' % (MAGIC_PREFIX, PROTOVER)
MAGIC_PATHS = (MAGIC_PATH, '/Beanstalk~Magic~Beans/0.2')

OPT_FLAGS = 'o:H:P:X:L:ZI:fA:R:h:p:aD:U:N'
OPT_ARGS = ['noloop', 'clean', 'nocrashreport',
            'optfile=', 'httpd=', 'pemfile=', 'httppass=',
            'logfile=', 'daemonize', 'nodaemonize', 'runas=', 'pidfile=',
            'isfrontend', 'noisfrontend', 'settings', 'defaults', 'domain=',
            'authdomain=', 'register=', 'host=', 'ports=', 'protos=',
            'backend=', 'frontend=', 'frontends=', 'torify=', 'socksify=',
            'new', 'all', 'noall', 'dyndns=', 'backend=', 'nozchunks',
            'buffers=']

AUTH_ERRORS           = '128.'
AUTH_ERR_USER_UNKNOWN = '128.0.0.0'
AUTH_ERR_INVALID      = '128.0.0.1'

BE_PROTO = 0
BE_DOMAIN = 1
BE_BACKEND = 2
BE_SECRET = 3

DYNDNS = {
  'pagekite.net': ('http://up.b5p.us/'
                   '?hostname=%(domain)s&myip=%(ips)s&sign=%(sign)s'),
  'beanstalks.net': ('http://up.b5p.us/'
                     '?hostname=%(domain)s&myip=%(ips)s&sign=%(sign)s'),
  'dyndns.org': ('https://%(username)s:%(password)s@members.dyndns.org'
                 '/nic/update?wildcard=NOCHG&backmx=NOCHG'
                 '&hostname=%(domain)s&myip=%(ip)s'),
  'no-ip.com': ('https://%(username)s:%(password)s@members.dyndns.org'
                '/nic/update?wildcard=NOCHG&backmx=NOCHG'
                '&hostname=%(domain)s&myip=%(ip)s'),
}

import base64
from cgi import escape as escape_html
import getopt
import os
import random
import re
import select
import socket
rawsocket = socket.socket

import struct
import sys
import threading
import time
import traceback
import urllib
import zlib

try:
  import syslog
except Exception:
  pass
 
import BaseHTTPServer
try:
  from urlparse import parse_qs, urlparse
except Exception, e:
  from cgi import parse_qs
  from urlparse import urlparse

try:
  import hashlib
  def sha1hex(data):
    hl = hashlib.sha1()
    hl.update(data)
    return hl.hexdigest()
except Exception:
  import sha
  def sha1hex(data):
    return sha.new(data).hexdigest() 


class MockYamonD(object):
  def __init__(self, sspec, server=None, handler=None): pass
  def vmax(self, var, value): pass
  def vscale(self, var, ratio, add=0): pass
  def vset(self, var, value): pass
  def vmin(self, var, value): pass
  def lcreate(self, listn, elems): pass
  def ladd(self, listn, value): pass
  def render_vars_text(self): return ''
  def quit(self): pass
  def run(self): pass
 
try:
  import yamond
  YamonD=yamond.YamonD
except Exception:
  YamonD=MockYamonD


gSecret = None
def globalSecret():
  global gSecret
  if not gSecret: gSecret = '%8.8x%8.8x%8.8x' % (random.randint(0, 0x7FFFFFFE), 
                                                 time.time(),
                                                 random.randint(0, 0x7FFFFFFE))
  return gSecret

def signToken(token=None, secret=None, payload='', length=36):
  """
  This will generate a random token with a signature which could only have come
  from this server.  If a token is provided, it is re-signed so the original
  can be compared with what we would have generated, for verification purposes.

  Note: This is only as secure as random.randint() is random.
  """
  if not secret: secret = globalSecret()
  if not token: token = '%8.8x' % (random.randint(0, 0x7FFFFFFD)+1)
  return token[0:8] + sha1hex(secret + payload + token[0:8])[0:length-8]


class ConfigError(Exception):
  pass

class ConnectError(Exception):
  pass


def HTTP_PageKiteRequest(server, backends, tokens=None, nozchunks=False,
                          testtoken=None):
  req = ['POST %s HTTP/1.1\r\n' % MAGIC_PATH,
         'Host: %s\r\n' % server,
         'Content-Type: application/octet-stream\r\n',
         'Transfer-Encoding: chunked\r\n']

  if not nozchunks:
    req.append('X-PageKite-Features: ZChunks\r\n')
         
  tokens = tokens or {}
  for d in backends.keys():
    token = d in tokens and tokens[d] or ''
    data = '%s:%s:%s' % (d, signToken(token=globalSecret(),
                                      payload=globalSecret(),
                                      secret=server),
                         token)
    sign = signToken(secret=backends[d][BE_SECRET], payload=data, token=testtoken)
    req.append('X-PageKite: %s:%s\r\n' % (data, sign))

  req.append('\r\nOK\r\n')
  return ''.join(req)

def HTTP_ResponseHeader(code, title, mimetype='text/html'):
  return ('HTTP/1.1 %s %s\r\nContent-Type: %s\r\nPragma: no-cache\r\n'
          'Expires: 0\r\nCache-Control: no-store\r\nConnection: close'
          '\r\n') % (code, title, mimetype)

def HTTP_Header(name, value):
  return '%s: %s\r\n' % (name, value)

def HTTP_StartBody():
  return '\r\n'

def HTTP_Response(code, title, body, mimetype='text/html', headers=None):
  data = [HTTP_ResponseHeader(code, title, mimetype)]
  if headers: data.extend(headers)
  data.extend([HTTP_StartBody(), ''.join(body)])
  return ''.join(data)

# FIXME: Replace all these GIF images with little json snippets for better UI.
def HTTP_NoFeConnection():
  return HTTP_Response(200, 'OK', base64.decodestring(
    'R0lGODlhCgAKAMQCAN4hIf/+/v///+EzM+AuLvGkpORISPW+vudgYOhiYvKpqeZY'
    'WPbAwOdaWup1dfOurvW7u++Rkepycu6PjwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAACH5BAEAAAIALAAAAAAKAAoAAAUtoCAcyEA0jyhEQOs6AuPO'
    'QJHQrjEAQe+3O98PcMMBDAdjTTDBSVSQEmGhEIUAADs='),
      headers=[HTTP_Header('X-PageKite-Status', 'Down-FE')],
      mimetype='image/gif')

def HTTP_NoBeConnection():
  # FIXME: Make this different...
  return HTTP_Response(200, 'OK', base64.decodestring(
    'R0lGODlhCgAKAPcAAI9hE6t2Fv/GAf/NH//RMf/hd7u6uv/mj/ntq8XExMbFxc7N'
    'zc/Ozv/xwfj31+jn5+vq6v///////wAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAACH5BAEAABIALAAAAAAKAAoAAAhDACUIlBAgwMCDARo4MHiQ'
    '4IEGDAcGKAAAAESEBCoiiBhgQEYABzYK7OiRQIEDBgMIEDCgokmUKlcOKFkgZcGb'
    'BSUEBAA7'),
      headers=[HTTP_Header('X-PageKite-Status', 'Down-BE')],
      mimetype='image/gif')
                            
def HTTP_GoodBeConnection():
  return HTTP_Response(200, 'OK', base64.decodestring(
    'R0lGODlhCgAKANUCAEKtP0StQf8AAG2/a97w3qbYpd/x3mu/aajZp/b79vT69Mnn'
    'yK7crXTDcqraqcfmxtLr0VG0T0ivRpbRlF24Wr7jveHy4Pv9+53UnPn8+cjnx4LI'
    'gNfu1v///37HfKfZpq/crmG6XgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAACH5BAEAAAIALAAAAAAKAAoAAAZIQIGAUDgMEASh4BEANAGA'
    'xRAaaHoYAAPCCZUoOIDPAdCAQhIRgJGiAG0uE+igAMB0MhYoAFmtJEJcBgILVU8B'
    'GkpEAwMOggJBADs='),
      headers=[HTTP_Header('X-PageKite-Status', 'OK')],
      mimetype='image/gif')
 
def HTTP_Unavailable(where, proto, domain, comment=''):
  return HTTP_Response(200, 'OK', 
                       ['<html><body><h1>Sorry! (', where, ')</h1>',
                        '<p>The ', proto.upper(),' <a href="', WWWHOME, '">',
                        '<i>pageKite</i></a> for <b>', domain, 
                        '</b> is unavailable at the moment.</p>',
                        '<p>Please try again later.</p>',
                        '</body><!-- ', comment, ' --></html>'])

LOG = []

def LogValues(values, testtime=None):
  words = [(kv[0], ('%s' % kv[1]).replace('\t', ' ')
                                 .replace('\r', ' ')
                                 .replace('\n', ' ')
                                 .replace('; ', ', ')
                                 .strip()) for kv in values]
  words.append(('ts', '%x' % (testtime or time.time())))
  wdict = dict(words)
  LOG.append(wdict)
  if len(LOG) > 100: LOG.pop(0)
  return (words, wdict)
 
def LogSyslog(values):
  words, wdict = LogValues(values)
  if 'err' in wdict:
    syslog.syslog(syslog.LOG_ERR, '; '.join(['='.join(x) for x in words]))
  elif 'debug' in wdict:
    syslog.syslog(syslog.LOG_DEBUG, '; '.join(['='.join(x) for x in words]))
  else:
    syslog.syslog(syslog.LOG_INFO, '; '.join(['='.join(x) for x in words]))

def LogStdout(values):
  words, wdict = LogValues(values)
  print '; '.join(['='.join(x) for x in words])

Log = LogStdout

def LogError(msg, parms=None):
  emsg = [('err', msg)]
  if parms: emsg.extend(parms)
  Log(emsg)

def LogDebug(msg, parms=None):
  emsg = [('debug', msg)]
  if parms: emsg.extend(parms)
  Log(emsg)


# FIXME: This could easily be a pool of threads to let us handle more
#        than one incoming request at a time.
class AuthThread(threading.Thread):
  """Handle authentication work in a separate thread."""
  
  def __init__(self, conns):
    threading.Thread.__init__(self)
    self.qc = threading.Condition()
    self.jobs = []
    self.conns = conns

  def check(self, requests, callback):
    self.qc.acquire()
    self.jobs.append((requests, callback))
    self.qc.notify()
    self.qc.release()

  def quit(self):
    self.qc.acquire()
    self.keep_running = False
    self.qc.notify()
    self.qc.release()

  def run(self):
    self.keep_running = True
    self.qc.acquire()
    while self.keep_running:
      if self.jobs:
        (requests, callback) = self.jobs.pop(0)
        self.qc.release()

        results = []
        for (proto, domain, srand, token, sign, prefix) in requests:
          what = '%s:%s:%s' % (proto, domain, srand)
          if not token or not sign:
            results.append(('%s-SignThis' % prefix,
                            '%s:%s' % (what, signToken(payload=what))))
          elif not self.conns.config.GetDomainQuota(proto, domain, srand, token, sign):
            results.append(('%s-Invalid' % prefix, what))
          elif self.conns.Tunnel(proto, domain) is not None:
            # FIXME: Allow multiple backends!
            results.append(('%s-Duplicate' % prefix, what))
          else:
            results.append(('%s-OK' % prefix, what))

        callback(results) 

        self.qc.acquire()
      else:
        self.qc.wait()
      
    self.buffering = 0
    self.qc.release()


def fmt_size(count):
  if count > 2*(1024*1024*1024):
    return '%dGB' % (count / (1024*1024*1024))
  if count > 2*(1024*1024):
    return '%dMB' % (count / (1024*1024))
  if count > 2*(1024):
    return '%dKB' % (count / 1024)
  return '%dB' % count


class UiRequestHandler(BaseHTTPServer.BaseHTTPRequestHandler):

  TEMPLATE_TEXT = ('%(body)s')
  TEMPLATE_HTML = ('<html><head>\n'
               '<link rel="stylesheet" media="screen, screen"'
                ' href="http://pagekite.net/css/pagekite.css"'
                ' type="text/css" title="Default stylesheet" />\n'
               '<title>%(title)s - %(prog)s v%(ver)s</title>\n'
              '</head><body>\n'
               '<h1>%(title)s</h1>\n'
               '<div id=body>%(body)s</div>\n'
               '<div id=footer><hr><i>Powered by <b>pagekite.py'
                ' v%(ver)s</b> and'
                ' <a href="' + WWWHOME + '"><i>pageKite.net</i></a>.<br>'
                'Local time is %(now)s.</i></div>\n'
              '</body></html>\n')
 
  def log_message(self, format, *args):
    Log([('uireq', format % args)])

  def html_overview(self):
    conns = self.server.conns
    backends = self.server.pkite.backends

    html = [(
      '<div id=welcome><p>Welcome to your <i>pageKite</i> control panel!</p></div>\n'
      '<p id=links>[ <a href="log.html">Logs</a>, '
                    '<a href="/conns/">Connections</a> ]</p>\n'
      '<div id=live><h2>Flying kites:</h2><ul>\n'
    )]

    for tid in conns.tunnels:
      proto, domain = tid.split(':')
      if tid in backends:
        backend = backends[tid][BE_BACKEND]
        if proto.startswith('http'):
          binfo = '<a href="%s://%s">%s</a>' % (proto, backend, backend)
        else:
          binfo = '<b>%s</b>' % backend
      else:
        binfo = '<i>none</i>'

      if proto.startswith('http'):
        tinfo = '%s: <a href="%s://%s">%s</a>' % (proto, proto, domain, domain)
      else:
        tinfo = '%s: <b>%s</b>' % (proto, domain) 

      for tunnel in conns.tunnels[tid]:
        html.append(('<li><span class=tid>%s</span></b>'
                     ' (<span class=ips>%s</span> to'
                     ' <span class=backend>%s</span>,'
                     ' <span class=bytes>%s in, %s out</span>)'
                     '</li>\n') % (tinfo,
                                   tunnel.server_name.split(':')[0],
                                   binfo,
                                   fmt_size(tunnel.all_in + tunnel.read_bytes),
                                   fmt_size(tunnel.all_out + tunnel.wrote_bytes))) 
    if not conns.tunnels:
      html.append('<i>None</i>')
    
    html.append(
      '</ul></div>\n'
    )
    return {
      'title': 'Control Panel',
      'body': ''.join(html)
    }

  def txt_log(self):
    return '\n'.join(['%s' % x for x in LOG])

  def html_log(self, path):
    debug = path.find('debug') >= 0
    httpd = path.find('httpd') >= 0
    alllog = path.find('all') >= 0
    html = ['<p id=links>[ <a href="/">Control Panel</a> | Logs: '
                         ' <a href="log.html">normal</a>,'
                         ' <a href="debug-log.html">debug</a>,'
                         ' <a href="httpd-log.html">httpd</a>,'
                         ' <a href="all-log.html">all</a>,'
                         ' <a href="log.txt">raw</a> ]</p>'
            '<table>']
    lines = []
    for line in LOG:
      if not alllog and ('debug' in line) != debug: continue
      if not alllog and ('uireq' in line) != httpd: continue

      keys = line.keys()
      keys.sort()
      lhtml = ('<tr><td colspan=3><b>%s</b></td>'
               '</tr>' % time.strftime('%Y-%m-%d %H:%M:%S',
                                       time.localtime(int(line['ts'], 16))))
      for key in keys:
        if key != 'ts':
          lhtml += ('<tr><td></td><td align=right>%s&nbsp;=</td><td>%s</td>'
                    '</tr>' % (key, escape_html(line[key])))
      lines.insert(0, lhtml)

    html.extend(lines)
    html.append('</table>')
    return {
      'title': 'Log viewer, recent events',
      'body': ''.join(html)
    }

  def html_conns(self):
    html = ['<ul>']
    sids = SELECTABLES.keys()
    sids.sort(reverse=True)
    for sid in sids:
      sel = SELECTABLES[sid]
      html.append('<li><a href="/conn/%s">%s</a>%s'
                  ' ' % (sid, escape_html(str(sel)),
                         sel.dead and ' ' or ' <i>alive</i>'))
    html.append('</ul>')
    return {
      'title': 'Connection log',
      'body': ''.join(html)
    }

  def html_conn(self, path):
    sid = int(path[len('/conn/'):])
    if sid in SELECTABLES:
      html = ['<h2>%s</h2>' % escape_html('%s' % SELECTABLES[sid]),
              SELECTABLES[sid].html()]
    else:
      html = ['<h2>Connection %s not found. Expired?</h2>' % sid]
    return {
      'title': 'Connection details',
      'body': ''.join(html)
    }

  def begin_headers(self, code, mimetype):
    self.send_response(code)
    self.send_header('Cache-Control', 'no-store')
    self.send_header('Pragma', 'no-cache')
    self.send_header('Content-Type', mimetype)

  def do_GET(self):
    (scheme, netloc, path, params, query, frag) = urlparse(self.path) 

    data = {
      'prog': (sys.argv[0] or 'pagekite.py').split('/')[-1],
      'now': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime()),
      'ver': APPVER
    }

    authenticated = False
    if self.server.pkite.ui_password: 
      auth = self.headers.get('authorization')
      if auth:
        (how, ab64) = auth.split()
        if how.lower() == 'basic':
          (uid, password) = base64.b64decode(ab64).split(':')
          authenticated = (password == self.server.pkite.ui_password)
      if not authenticated:
        self.begin_headers(401, 'text/html')
        self.send_header('WWW-Authenticate',
                         'Basic realm=PK%d' % (time.time()/3600))
        self.end_headers()
        data['title'] = data['body'] = 'Authentication required.'
        self.wfile.write(self.TEMPLATE_HTML % data)
        return
    
    if path.endswith('.txt'):
      template = self.TEMPLATE_TEXT
      self.begin_headers(200, 'text/plain')
    else:
      template = self.TEMPLATE_HTML
      self.begin_headers(200, 'text/html')
    self.end_headers()

    qs = parse_qs(query)

    if path == '/vars.txt':
      data['body'] = self.server.pkite.yamond.render_vars_text()

    elif path == '/log.txt':        data['body'] = self.txt_log()
    elif path.endswith('log.html'): data.update(self.html_log(path))
    elif path == '/conns/':         data.update(self.html_conns())
    elif path.startswith('/conn/'): data.update(self.html_conn(path))
    else: data.update(self.html_overview())
        
    self.wfile.write(template % data)

class UiHttpServer(BaseHTTPServer.HTTPServer):
  def __init__(self, sspec, pkite, conns, handler=UiRequestHandler):
    BaseHTTPServer.HTTPServer.__init__(self, sspec, handler)
    self.pkite = pkite
    self.conns = conns
    pkite.yamond = YamonD(sspec)

class HttpUiThread(threading.Thread):
  """Handle HTTP UI in a separate thread."""

  def __init__(self, pkite, conns, 
               server=UiHttpServer, handler=UiRequestHandler):
    threading.Thread.__init__(self)
    self.ui_sspec = pkite.ui_sspec
    self.httpd = server(self.ui_sspec, pkite, conns, handler=handler)
    self.httpd.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.serve = True

  def quit(self):
    self.serve = False
    knock = rawsocket(socket.AF_INET, socket.SOCK_STREAM)
    knock.connect(self.ui_sspec)
    knock.close()

  def run(self):
    while self.serve:
      self.httpd.handle_request()
    LogDebug('HttpUiThread: done')
    self.httpd.socket.close()


HTTP_METHODS = ['OPTIONS', 'GET', 'POST', 'PUT']
HTTP_VERSIONS = ['HTTP/1.0', 'HTTP/1.1']

class HttpParser(object):
  """Parse an HTTP request, line-by-line."""

  IN_REQUEST = 1
  IN_HEADERS = 2
  IN_BODY = 3
  IN_RESPONSE = 4
  PARSE_FAILED = -1

  def __init__(self, lines=None, state=None, testbody=False):
    self.state = state or self.IN_REQUEST
    self.method = None
    self.path = None
    self.version = None
    self.code = None
    self.message = None
    self.headers = []
    self.lines = []
    self.body_result = testbody

    if lines is not None:
      for line in lines:
        if not self.Parse(line): break

  def ParseResponse(self, line):
    self.version, self.code, self.message = line.split()

    if not self.version.upper() in HTTP_VERSIONS:
      LogError('Invalid version: %s' % self.version)
      return False

    self.state = self.IN_HEADERS
    return True

  def ParseRequest(self, line):
    self.method, self.path, self.version = line.split()

    if not self.method in HTTP_METHODS:
      LogError('Invalid method: %s' % self.method)
      return False

    if not self.version.upper() in HTTP_VERSIONS:
      LogError('Invalid version: %s' % self.version)
      return False

    self.state = self.IN_HEADERS
    return True

  def ParseHeader(self, line):
    if line in ('', '\r', '\n', '\r\n'):
      self.state = self.IN_BODY
      return True

    header, value = line.split(':', 1)
    if value and value.startswith(' '): value = value[1:]

    self.headers.append((header.lower(), value)) 
    return True

  def ParseBody(self, line):
    # Could be overridden by subclasses, for now we just play dumb.
    return self.body_result

  def Parse(self, line):
    self.lines.append(line)
    try:
      if (self.state == self.IN_RESPONSE):
        return self.ParseResponse(line)

      elif (self.state == self.IN_REQUEST):
        return self.ParseRequest(line)

      elif (self.state == self.IN_HEADERS):
        return self.ParseHeader(line)

      elif (self.state == self.IN_BODY):
        return self.ParseBody(line)

    except ValueError, err:
      LogError('Parse failed: %s, %s, %s' % (self.state, err, self.lines))

    self.state = self.PARSE_FAILED
    return False

  def Header(self, header):
    return [h[1].strip() for h in self.headers if h[0] == header.lower()]


def obfuIp(ip):
  quads = ('%s' % ip).split('.')
  return '~%s' % '.'.join([q for q in quads[2:]])

selectable_id = 0
buffered_bytes = 0
SELECTABLES = {}

class Selectable(object):
  """A wrapper around a socket, for use with select."""

  def __init__(self, fd=None, address=None, maxread=32000):
    self.SetFD(fd or rawsocket(socket.AF_INET, socket.SOCK_STREAM))
    self.maxread = maxread
    self.address = address
    self.created = self.bytes_logged = time.time()
    self.read_bytes = self.all_in = 0
    self.wrote_bytes = self.all_out = 0
    self.write_blocked = ''
    self.dead = False

    # FIXME: This should go away after testing!
    self.lastio = ['', '']

    global selectable_id
    selectable_id += 1
    self.sid = selectable_id

    if address:
      addr = address or ('x.x.x.x', 'x')
      self.log_id = 's%s/%s:%s' % (self.sid, obfuIp(addr[0]), addr[1])
    else:
      self.log_id = 's%s' % self.sid

    self.zw = None
    self.zlevel = 1
    self.zreset = False

    SELECTABLES[selectable_id] = self
    old = selectable_id-50
    ancient = selectable_id-5000
    if old in SELECTABLES:
      sel = SELECTABLES[old]
      if sel.dead or (sel.all_out + sel.wrote_bytes) == 0: del SELECTABLES[old]
    if ancient in SELECTABLES: del SELECTABLES[ancient]

  def __str__(self):
    return '%s: %s' % (self.log_id, self.__class__)

  def html(self):
    try:
      peer = self.fd.getpeername()
      sock = self.fd.getsockname()
    except Exception:
      peer = ('x.x.x.x', 'x')
      sock = ('x.x.x.x', 'x')

    return ('<b>Outgoing ZChunks</b>: %s<br>'
            '<b>Remote address</b>: %s<br>'
            '<b>Local address</b>: %s<br>'
            '<b>Bytes in / out</b>: %s / %s<br>'
            '<b>Created</b>: %s<br>'
            '<b>Status</b>: %s<br>'
            '<pre><b>Last recv:</b>\n%s</pre>'
            '<pre><b>Last sent:</b>\n%s</pre>'
            '\n') % (self.zw and ('level %d' % self.zlevel) or 'off',
                     self.dead and '-' or (obfuIp(peer[0]), peer[1]),
                     self.dead and '-' or (obfuIp(sock[0]), sock[1]),
                     fmt_size(self.all_in + self.read_bytes),
                     fmt_size(self.all_out + self.wrote_bytes),
                     time.strftime('%Y-%m-%d %H:%M:%S',
                                   time.localtime(self.created)),
                     self.dead and 'dead' or 'alive',
                     escape_html(self.lastio[0]),
                     escape_html(self.lastio[1]))

  def ResetZChunks(self):
    if self.zw:
      self.zreset = True
      self.zw = zlib.compressobj(self.zlevel)

  def EnableZChunks(self, level=1):
    LogDebug('Selectable::EnableZChunks: ZChunks enabled!')
    self.zlevel = level
    self.zw = zlib.compressobj(level)

  def SetFD(self, fd):
    self.fd = fd
    self.fd.setblocking(0)
    try:
      self.fd.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
      self.fd.setsockopt(socket.SOL_TCP, socket.TCP_KEEPIDLE, 60)
      self.fd.setsockopt(socket.SOL_TCP, socket.TCP_KEEPCNT, 10)
      self.fd.setsockopt(socket.SOL_TCP, socket.TCP_KEEPINTVL, 1)
    except Exception:
      # FIXME: Should we complain that this isn't supported by this OS?
      pass

  def SetConn(self, conn):
    self.SetFD(conn.fd)
    self.log_id = conn.log_id
    self.read_bytes = conn.read_bytes
    self.wrote_bytes = conn.wrote_bytes

  def Log(self, values):
    if self.log_id: values.append(('id', self.log_id))
    Log(values)

  def LogError(self, error, params=None):
    values = params or []
    if self.log_id: values.append(('id', self.log_id))
    LogError(error, values)

  def LogTraffic(self):
    if self.wrote_bytes or self.read_bytes:
      self.Log([('wrote', '%d' % self.wrote_bytes),
                ('read', '%d' % self.read_bytes)])

      self.all_out += self.wrote_bytes
      self.all_in += self.read_bytes

      self.bytes_logged = time.time()
      self.wrote_bytes = self.read_bytes = 0

  def Cleanup(self):
    self.dead = True
    self.fd.close()
    self.LogTraffic()

  def ProcessData(self, data):
    self.LogError('Selectable::ProcessData: Should be overridden!')
    return False

  def ReadData(self):
    try:
      data = self.fd.recv(self.maxread)
    except socket.error, err:
      LogDebug('Error reading socket: %s' % err)
      return False

    if data is None or data == '':
      return False
    else:
      self.lastio[0] = data
      self.read_bytes += len(data)
      if self.read_bytes > 102400: self.LogTraffic()
      return self.ProcessData(data)

  def Send(self, data):
    global buffered_bytes
    buffered_bytes -= len(self.write_blocked)

    sending = self.write_blocked+(''.join(data))
    self.lastio[1] = sending
    sent_bytes = 0
    if sending:
      try:
        sent_bytes = self.fd.send(sending)
        self.wrote_bytes += sent_bytes
#       print '> %s' % sending[0:sent_bytes]
      except socket.error, err:
        LogDebug('Error sending: %s' % err)

    self.write_blocked = sending[sent_bytes:]
    buffered_bytes += len(self.write_blocked)

    if self.wrote_bytes > 102400: self.LogTraffic()
    return True

  def SendChunked(self, data, compress=True, zhistory=None):
    rst = ''
    if self.zreset:
      self.zreset = False
      rst = 'R'

    # Stop compressing streams that just get bigger.
    if zhistory and (zhistory[0] < zhistory[1]): compress = False

    sdata = ''.join(data)
    if self.zw and compress:
      try:
        zdata = self.zw.compress(sdata) + self.zw.flush(zlib.Z_SYNC_FLUSH)
        if zhistory:
          zhistory[0] = len(sdata)
          zhistory[1] = len(zdata)
        LogDebug('Sending %d bytes as %d' % (len(sdata), len(zdata)))
        return self.Send(['%xZ%x%s\r\n%s' % (len(sdata), len(zdata), rst, zdata)])
      except zlib.error:
        LogDebug('Error compressing, resetting ZChunks.')
        self.ResetZChunks()

    return self.Send(['%x%s\r\n%s' % (len(sdata), rst, sdata)])

  def Flush(self):
    while self.write_blocked: self.Send([])


class Connections(object):
  """A container for connections (Selectables), config and tunnel info.""" 
  
  def __init__(self, config):
    self.config = config
    self.conns = []
    self.tunnels = {}
    self.auth = None

  def start(self, auth_thread=None):
    self.auth = auth_thread or AuthThread(self)
    self.auth.start()

  def Add(self, conn):
    self.conns.append(conn)

  def Remove(self, conn):
    if conn in self.conns:
      self.conns.remove(conn)
    for tid in self.tunnels.keys():
      if conn in self.tunnels[tid]:
        self.tunnels[tid].remove(conn)
        if not self.tunnels[tid]: del self.tunnels[tid]

  def Sockets(self):
    # FIXME: This is O(n)
    return [s.fd for s in self.conns if s.fd]

  def Blocked(self):
    # FIXME: This is O(n)
    return [s.fd for s in self.conns if s.fd and s.write_blocked]

  def CleanFds(self):
    evil = []
    for s in self.conns:
      try:
        if s.fd.closed(): evil.append(s)
      except Exception:
        evil.append(s)
    for s in evil:
      self.conns.remove(s) 

  def Connection(self, fd):
    for conn in self.conns:
      if conn.fd == fd:
        return conn
    return None

  def TunnelServers(self):
    servers = {}
    for tid in self.tunnels:
      for tunnel in self.tunnels[tid]:
        server = tunnel.server_name
        if server is not None:
          servers[server] = 1
    return servers.keys() 

  def Tunnel(self, proto, domain, conn=None):
    tid = '%s:%s' % (proto, domain)
    if conn is not None: 
      if tid not in self.tunnels:
        self.tunnels[tid] = []
      self.tunnels[tid].append(conn)

    if tid in self.tunnels:
      return self.tunnels[tid]
    else:
      return None


class LineParser(Selectable):
  """A Selectable which parses the input as lines of text."""

  def __init__(self, fd=None, address=None):
    Selectable.__init__(self, fd, address)
    self.leftovers = ''

  def html(self):
    return Selectable.html(self)

  def Cleanup(self):
    Selectable.Cleanup(self)

  def ProcessData(self, data):
    lines = (self.leftovers+data).splitlines(True)
    self.leftovers = ''

    while lines:
      line = lines[0]
      lines = lines[1:]
      if line.endswith('\n'):
        if self.ProcessLine(line, lines) is False:
          return False
      else:
        self.leftovers += line

    return True

  def ProcessLine(self, line, lines):
    self.LogError('LineParser::ProcessLine: Should be overridden!')
    return False


TLS_CLIENTHELLO = '%c' % 026

# FIXME: Add "port hints" and an ip<->backend cache, for making clever guesses
#        as to which HTTPS backend to use if SNI is missing.
# FIXME: XMPP support
class MagicProtocolParser(LineParser):
  """A Selectable which recognizes HTTP, TLS or XMPP preambles."""

  def __init__(self, fd=None, address=None):
    LineParser.__init__(self, fd, address)
    self.leftovers = ''
    self.might_be_tls = True
    self.is_tls = False

  def html(self):
    return ('<b>Detected TLS</b>: %s<br>'
            '%s') % (self.is_tls,
                     LineParser.html(self))

  def ProcessData(self, data):
    if self.might_be_tls:
      self.might_be_tls = False
      if not data.startswith(TLS_CLIENTHELLO):
        return LineParser.ProcessData(self, data)
      self.is_tls = True

    if self.is_tls:
      return self.ProcessTls(data)
    else:
      return LineParser.ProcessData(self, data)

  def GetMsg(self, data):
    mtype, ml24, mlen = struct.unpack('>BBH', data[0:4])    
    mlen += ml24 * 0x10000
    return mtype, data[4:4+mlen], data[4+mlen:]

  def GetClientHelloExtensions(self, msg):
    # Ugh, so many magic numbers! These are accumulated sizes of
    # the different fields we are ignoring in the TLS headers.
    slen = struct.unpack('>B', msg[34])[0]
    cslen = struct.unpack('>H', msg[35+slen:37+slen])[0]
    cmlen = struct.unpack('>B', msg[37+slen+cslen])[0]
    extofs = 34+1+2+1+2+slen+cslen+cmlen
    if extofs < len(msg): return msg[extofs:]
    return None

  def GetSniNames(self, extensions):
    names = []
    while extensions:
      etype, elen = struct.unpack('>HH', extensions[0:4])
      if etype == 0:
        # OK, we found an SNI extension, get the list.
        namelist = extensions[6:4+elen]  
        while namelist:
          ntype, nlen = struct.unpack('>BH', namelist[0:3])
          if ntype == 0: names.append(namelist[3:3+nlen].lower())
          namelist = namelist[3+nlen:]
      extensions = extensions[4+elen:]
    return names 

  def GetSni(self, data):
    hello, vmajor, vminor, mlen = struct.unpack('>BBBH', data[0:5])    
    data = data[5:]
    sni = []
    while data:
      mtype, msg, data = self.GetMsg(data)
      if mtype == 1:
        # ClientHello!
        sni.extend(self.GetSniNames(self.GetClientHelloExtensions(msg)))
    return sni

  def ProcessTls(self, data):
    self.LogError('TlsOrLineParser::ProcessTls: Should be overridden!')
    return False


class ChunkParser(Selectable):
  """A Selectable which parses the input as chunks."""

  def __init__(self, fd=None, address=None):
    Selectable.__init__(self, fd, address)
    self.want_cbytes = 0
    self.want_bytes = 0
    self.compressed = False
    self.header = ''
    self.chunk = ''
    self.zr = zlib.decompressobj()

  def html(self):
    return Selectable.html(self)

  def Cleanup(self):
    Selectable.Cleanup(self)

  def ProcessData(self, data):
    if self.want_bytes == 0:
      self.header += data
      if self.header.find('\r\n') < 0: return 1
      try:
        size, data = self.header.split('\r\n', 1)
        self.header = ''

        if size.endswith('R'):
          self.zr = zlib.decompressobj()
          size = size[0:-1]

        if 'Z' in size:
          csize, zsize = size.split('Z')
          self.compressed = True
          self.want_cbytes = int(csize, 16)
          self.want_bytes = int(zsize, 16)
        else:   
          self.compressed = False
          self.want_bytes = int(size, 16)

      except ValueError, err:
        self.LogError('ChunkParser::ProcessData: %s' % err)
        self.Log([('bad_data', data)])
        return False

      if self.want_bytes == 0:
        LogDebug('ChunkParser::ProcessData: end of chunk')
        return False

    process = data[:self.want_bytes]
    leftover = data[self.want_bytes:]

    self.chunk += process
    self.want_bytes -= len(process)

    result = 1
    if self.want_bytes == 0:
      if self.compressed:
        try:
          cchunk = self.zr.decompress(self.chunk)
        except zlib.error:
          cchunk = ''

        if len(cchunk) != self.want_cbytes:
          result = self.ProcessCorruptChunk(self.chunk)
        else:
          LogDebug('ChunkParser::ProcessData: inflated %d bytes to %d' % (len(self.chunk), self.want_cbytes))
          result = self.ProcessChunk(cchunk)
      else:
        result = self.ProcessChunk(self.chunk)
      self.chunk = ''
      if leftover:
        return self.ProcessData(leftover) and result

    return result

  def ProcessCorruptChunk(self, chunk):
    self.LogError('ChunkParser::ProcessData: ProcessCorruptChunk not overridden!')
    return False

  def ProcessChunk(self, chunk):
    self.LogError('ChunkParser::ProcessData: ProcessChunk not overridden!')
    return False


# FIXME: Add metrics to measure performance of tunnel, so we can prioritize
#        client DNS records accordingly.
class Tunnel(ChunkParser):
  """A Selectable representing a PageKite tunnel."""
  
  def __init__(self, conns):
    ChunkParser.__init__(self)

    # We want to be sure to read the entire chunk at once, including
    # headers to save cycles, so we double the size we're willing to 
    # read here.
    self.maxread *= 2

    self.server_name = 'x.x.x.x:x'
    self.conns = conns
    self.users = {}
    self.zhistory = {}
    self.backends = {}
    self.rtt = 100000

  def html(self):
    return ('<b>Server name</b>: %s<br>'
            '%s') % (self.server_name, ChunkParser.html(self))

  def Cleanup(self):
    # FIXME: Send good-byes to everyone?
    ChunkParser.Cleanup(self)

  def _FrontEnd(conn, body, conns):
    """This is what the front-end does when a back-end requests a new tunnel."""
    self = Tunnel(conns)
    requests = []
    try:
      for prefix in ('X-Beanstalk', 'X-PageKite'):
        for feature in conn.parser.Header(prefix+'-Features'):
          if feature == 'ZChunks': self.EnableZChunks(level=1)

        for bs in conn.parser.Header(prefix):
          # X-Beanstalk: proto:my.domain.com:token:signature
          proto, domain, srand, token, sign = bs.split(':') 
          requests.append((proto.lower(), domain.lower(), srand, token, sign,
                           prefix))
      
    except ValueError, err:
      self.LogError('Discarding connection: %s' % err)
      return None

    except socket.error, err:
      self.LogError('Discarding connection: %s')
      return None

    self.SetConn(conn)
    conns.auth.check(requests, lambda r: self.AuthCallback(conn, r))

    return self

  def AuthCallback(self, conn, results):
    
    output = [HTTP_ResponseHeader(200, 'OK'),
              HTTP_Header('Content-Transfer-Encoding', 'chunked'),
              HTTP_Header('X-PageKite-Features', 'ZChunks')]
    ok = {}
    for r in results:
      output.append('%s: %s\r\n' % r)
      if r[0] in ('X-PageKite-OK', 'X-Beanstalk-OK'): ok[r[1]] = 1

    output.append(HTTP_StartBody())
    self.Send(output)

    self.backends = ok.keys()
    if self.backends:
      for backend in self.backends:
        proto, domain, srand = backend.split(':')
        self.Log([('BE', 'FIXME?'), ('proto', proto), ('domain', domain)])
        self.conns.Tunnel(proto, domain, self)
      self.conns.Add(self) 
      return self
    else:
      conn.LogError('No tunnels configured, closing connection.')
      self.Cleanup()
      return None

  def _Connect(self, server, conns, tokens=None):
    if self.fd: self.fd.close()
    if conns.config.socks_server:
      import socks
      sock = socks.socksocket()
      self.SetFD(sock)
    else:
      self.SetFD(rawsocket(socket.AF_INET, socket.SOCK_STREAM))
    self.fd.setblocking(1)

    sspec = server.split(':')
    if len(sspec) > 1:
      self.fd.connect((sspec[0], int(sspec[1])))
    else:
      self.fd.connect((server, 80))

    self.Send(HTTP_PageKiteRequest(server, conns.config.backends, tokens,
                                    nozchunks=conns.config.disable_zchunks)) 
    self.Flush()

    data = ''
    while not data.endswith('\r\n\r\n'):
      buf = self.fd.recv(4096)
      if buf is None or buf == '':
        LogDebug('Remote end closed connection.')
        return None, None
      data += buf
      self.read_bytes += len(buf)
      self.lastio[0] = data

    self.fd.setblocking(0)
    parse = HttpParser(lines=data.splitlines(), state=HttpParser.IN_RESPONSE)

    return data, parse

  def _BackEnd(server, backends, require_all, conns):
    """This is the back-end end of a tunnel."""
    self = Tunnel(conns)
    self.backends = backends
    self.require_all = require_all
    self.server_name = server
    try:
      begin = time.time()
      data, parse = self._Connect(server, conns)
      if data and parse:

        tryagain = False
        tokens = {}
        for request in parse.Header('X-PageKite-SignThis'):
          proto, domain, srand, token = request.split(':')
          tokens['%s:%s' % (proto, domain)] = token
          tryagain = True
 
        if tryagain: 
          begin = time.time()
          data, parse = self._Connect(server, conns, tokens)

        if data and parse:
          if not conns.config.disable_zchunks:
            for feature in parse.Header('X-PageKite-Features'):
              if feature == 'ZChunks': self.EnableZChunks(level=9)

          for request in parse.Header('X-PageKite-OK'):
            proto, domain, srand = request.split(':')
            self.Log([('FE', self.server_name), ('proto', proto), ('domain', domain)])
            conns.Tunnel(proto, domain, self)

          for request in parse.Header('X-PageKite-Invalid'):
            proto, domain, srand = request.split(':')
            self.Log([('FE', self.server_name), ('err', 'Rejected'), ('proto', proto), ('domain', domain)])

          for request in parse.Header('X-PageKite-Duplicate'):
            proto, domain, srand = request.split(':')
            self.Log([('FE', self.server_name),
                      ('err', 'Duplicate'),
                      ('proto', proto),
                      ('domain', domain)])

        self.rtt = (time.time() - begin)
    
    except socket.error, e:
      return None

    conns.Add(self)

    return self

  FrontEnd = staticmethod(_FrontEnd)
  BackEnd = staticmethod(_BackEnd)

  def SendData(self, conn, data, sid=None, host=None, proto=None):
    sid = int(sid or conn.sid)
    if conn: self.users[sid] = conn
    if not sid in self.zhistory: self.zhistory[sid] = [0, 0]
    if host and proto:
      return self.SendChunked(['SID: %s\nProto: %s\nHost: %s\r\n\r\n' % (sid, proto, host), data],
                              zhistory=self.zhistory[sid]) 
    else:
      return self.SendChunked(['SID: %s\r\n\r\n' % sid, data],
                              zhistory=self.zhistory[sid]) 

  def Disconnect(self, conn, sid=None, sendeof=True):
    sid = int(sid or conn.sid)
    if sendeof:
      LogDebug('Sending EOF for %s' % sid)
      self.SendChunked('SID: %s\nEOF: 1\r\n\r\nBye!' % sid) 
    if sid in self.users:
      if self.users[sid] is not None: self.users[sid].Disconnect()
      del self.users[sid]
    if sid in self.zhistory:
      del self.zhistory[sid]

  def ResetRemoteZChunks(self):
    self.SendChunked('NOOP: 1\nZRST: 1\r\n\r\n!' % sid, compress=False) 

  def ProcessCorruptChunk(self, data):
    self.ResetRemoteZChunks()
    return True

  def Probe(self, host):
    for bid in self.conns.config.backends:
      be = self.conns.config.backends[bid]
      if be[BE_DOMAIN] == host:
        bhost, bport = be[BE_BACKEND].split(':')
        if self.conns.config.Ping(bhost, int(bport)) > 2: return False
    return True

  def ProcessChunk(self, data):
    try:
      headers, data = data.split('\r\n\r\n', 1)
      parse = HttpParser(lines=headers.splitlines(), 
                         state=HttpParser.IN_HEADERS)
    except ValueError:
      LogError('Tunnel::ProcessChunk: Corrupt packet!')
      return False

    if parse.Header('ZRST'): self.ResetZChunks() 
    if parse.Header('NOOP'): return True

    conn = None
    sid = None
    try:
      sid = int(parse.Header('SID')[0])
      eof = parse.Header('EOF')
    except IndexError, e:
      LogError('Tunnel::ProcessChunk: Corrupt packet!')
      return False

    if eof:
      LogDebug('Got EOF for %s' % sid)
      self.Disconnect(None, sid=sid, sendeof=False)
    else:
      if sid in self.users:
        conn = self.users[sid]
      else:
        proto = (parse.Header('Proto') or [''])[0].lower()
        host = (parse.Header('Host') or [''])[0].lower()
        if proto and host:
          if proto == 'probe':
            if self.Probe(host):
              self.SendChunked('SID: %s\r\n\r\n%s' % (
                                 sid, HTTP_GoodBeConnection() )) 
            else:
              self.SendChunked('SID: %s\r\n\r\n%s' % (
                                 sid, HTTP_NoBeConnection() )) 
          else:
            conn = UserConn.BackEnd(proto, host, sid, self)
            if proto == 'http' and not conn:
              self.SendChunked('SID: %s\r\n\r\n%s' % (
                                 sid, HTTP_Unavailable('be', proto, host) )) 
          if conn:
            self.users[sid] = conn

      if not conn:
        self.Disconnect(None, sid=sid)
      else:
        # FIXME: We should probably be adding X-Forwarded-For headers
        conn.Send(data)

    return True


class UserConn(Selectable):
  """A Selectable representing a user's connection."""
  
  def __init__(self):
    Selectable.__init__(self)
    self.tunnel = None

  def html(self):
    return ('<b>Tunnel</b>: <a href="/conn/%s">%s</a><br>'
            '%s') % (self.tunnel and self.tunnel.sid or '',
                     escape_html('%s' % (self.tunnel or '')),
                     Selectable.html(self))
 
  def Cleanup(self):
    self.tunnel.Disconnect(self)
    Selectable.Cleanup(self)

  def Disconnect(self):
    self.conns.Remove(self)
    Selectable.Cleanup(self)

  def _FrontEnd(conn, proto, host, body, conns):
    # This is when an external user connects to a server and requests a
    # web-page.  We have to give it to them!
    self = UserConn()
    self.conns = conns
    self.SetConn(conn)

    if ':' in host: host, port = host.split(':')
    self.proto = proto
    self.host = host
    tunnels = conns.Tunnel((proto == 'probe') and 'http' or proto, host)
    if tunnels: self.tunnel = tunnels[0]

    if self.tunnel and self.tunnel.SendData(self, ''.join(body), host=host, proto=proto):
      self.Log([('rhost', self.host), ('rproto', self.proto)])
      self.conns.Add(self)
      return self
    else:
      self.Log([('err', 'No back-end'), ('proto', self.proto), ('domain', self.host)])
      return None

  def _BackEnd(proto, host, sid, tunnel):
    # This is when we open a backend connection, because a user asked for it.
    self = UserConn()
    self.sid = sid
    self.proto = proto
    self.host = host 
    self.conns = tunnel.conns
    self.tunnel = tunnel

    backend = self.conns.config.GetBackendServer(proto, host)
    if not backend:
      self.Log([('err', 'No backend found'), ('proto', proto), ('domain', host)])
      return None

    try:
      self.SetFD(rawsocket(socket.AF_INET, socket.SOCK_STREAM))
      self.fd.setblocking(1)

      sspec = backend.split(':')
      if len(sspec) > 1:
        self.fd.connect((sspec[0], int(sspec[1])))
      else:
        self.fd.connect((backend, 80))

      self.fd.setblocking(0)

    except socket.error, err:
      self.Log([('err', '%s' % err), ('proto', proto), ('domain', host)])
      return None

    self.conns.Add(self)
    return self
    
  FrontEnd = staticmethod(_FrontEnd)
  BackEnd = staticmethod(_BackEnd)

  def ProcessData(self, data):
    return self.tunnel.SendData(self, data)


class UnknownConn(MagicProtocolParser):
  """This class is a connection which we're not sure what is yet."""

  def __init__(self, fd, address, conns):
    MagicProtocolParser.__init__(self, fd, address)
    self.parser = HttpParser()
    self.conns = conns
    self.conns.Add(self)
    self.host = None
    self.sid = -1

  def ProcessLine(self, line, lines):
    if not self.parser:
      return True

    if self.parser.Parse(line) is False:
      return False

    if (self.parser.state == self.parser.IN_BODY):
      hosts = self.parser.Header('Host')
      if not hosts:
        self.Send(HTTP_Response(400, 'Bad request', 
                  ['<html><body><h1>400 Bad request</h1>',
                   '<p>Invalid request, no Host: found.</p>',
                   '</body></html>']))
        return False

      if self.parser.method == 'POST' and self.parser.path in MAGIC_PATHS:
        if Tunnel.FrontEnd(self, lines, self.conns) is None: 
          return False
      else:
        if self.parser.path.startswith(MAGIC_PREFIX):
          magic_parts = self.parser.path.split('/')
          host = magic_parts[2]
          proto = 'probe'
        else:
          host = hosts[0].lower()
          magic_parts = None
          proto = 'http'

        if UserConn.FrontEnd(self, proto, host,
                             self.parser.lines + lines, self.conns) is None:
          if magic_parts:
            self.Send(HTTP_NoFeConnection())
          else:
            self.Send(HTTP_Unavailable('fe', proto, host))

          return False

      # We are done!
      self.dead = True
      self.conns.Remove(self)

      # Break any circular references we might have
      self.parser = None
      self.conns = None

    return True

  def ProcessTls(self, data):
    domains = self.GetSni(data)
    if domains:
      if UserConn.FrontEnd(self, 'https', domains[0], [data], self.conns) is None:
        return False

    # We are done!
    self.dead = True
    self.conns.Remove(self)

    # Break any circular references we might have
    self.parser = None
    self.conns = None
    return True


class Listener(Selectable):
  """This class listens for incoming connections and accepts them."""

  def __init__(self, host, port, conns, backlog=100, connclass=UnknownConn):
    Selectable.__init__(self)
    self.fd.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.fd.bind((host, port))
    self.fd.listen(backlog)
    self.fd.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    self.Log([('listen', '%s:%s' % (host, port))])

    self.connclass = connclass
    self.port = port
    self.conns = conns
    self.conns.Add(self)

  def __str__(self):
    return '%s port=%s' % (Selectable.__str__(self), self.port)

  def html(self):
    return '<p>Listening on port %s</p>' % self.port
 
  def ReadData(self):
    try:
      client, address = self.fd.accept()
      if client:
        uc = self.connclass(client, address, self.conns)
        self.Log([('accept', '%s:%s' % (obfuIp(address[0]), address[1]))])
        return True
    except Exception, e:
      LogDebug('Listener::ReadData: %s' % e)
    return False


class PageKite(object):
  """Configuration and master select loop."""

  def __init__(self):
    self.isfrontend = False
    self.auth_domain = None
    self.server_host = ''
    self.server_ports = [80]
    self.server_protos = ['http', 'https']

    self.daemonize = False
    self.pidfile = None
    self.logfile = None
    self.setuid = None
    self.setgid = None
    self.ui_request_handler = UiRequestHandler
    self.ui_http_server = UiHttpServer
    self.ui_sspec = None
    self.ui_httpd = None
    self.ui_password = None
    self.yamond = MockYamonD(())
    self.disable_zchunks = False
    self.buffer_max = 256

    self.client_mode = 0

    self.socks_server = None
    self.require_all = False
    self.servers = []
    self.servers_manual = []
    self.servers_auto = None
    self.servers_new_only = False
    self.servers_no_ping = False
    self.servers_preferred = []

    self.dyndns = None
    self.last_updates = []
    self.backends = {}  # These are the backends we want tunnels for.
    self.conns = None
    self.looping = False

    self.crash_report_url = '%scgi-bin/crashes.pl' % WWWHOME
    self.rcfile_recursion = 0
    try:
      if os.getenv('USERPROFILE'):
        # Windows
        self.rcfile = os.path.join(os.getenv('USERPROFILE'), 'pagekite.cfg')
      else:
        # Everything else
        self.rcfile = os.path.join(os.getenv('HOME'), '.pagekite.rc')
    except Exception, e:
      # The above stuff may fail in some cases, e.g. on Android in SL4A.
      self.rcfile = 'pagekite.cfg'

  def PrintSettings(self):
    print '### Current settings for PageKite v%s. ###' % APPVER    
    print
    print '# HTTP control-panel settings:'
    print (self.ui_sspec and 'httpd=%s:%d' % self.ui_sspec or '#httpd=host:port')
    print (self.ui_password and 'httppass=%s' % self.ui_password or '#httppass=YOURSECRET')
    print
    print '# Back-end Options:'
    print (self.servers_auto and 'frontends=%d:%s:%d' % self.servers_auto or '#frontends=1:frontends.b5p.us:2222')
    if self.dyndns:
      provider, args = self.dyndns
      for prov in DYNDNS:
        if DYNDNS[prov] == provider and prov != 'beanstalks.net':
          args['prov'] = prov
      if 'prov' not in args:
        args['prov'] = provider
      if args['pass']:
        print 'dyndns=%(user)s:%(pass)s@%(prov)s' % args
      elif args['user']:
        print 'dyndns=%(user)s@%(prov)s' % args
      else:
        print 'dyndns=%(prov)s' % args
    else:
      print '#dyndns=pagekite.net OR' 
      print '#dyndns=user:pass@dyndns.org OR' 
      print '#dyndns=user:pass@no-ip.com' 
    bprinted=0
    for bid in self.backends:
      be = self.backends[bid]
      if be[BE_BACKEND]:
        print 'backend=%s:%s:%s' % (bid, be[BE_BACKEND], be[BE_SECRET])
        bprinted += 1
    if bprinted == 0:
      print '#backend=http:YOU.pagekite.me:localhost:80:SECRET'  
      print '#backend=https:YOU.pagekite.me:localhost:443:SECRET'  
    print (self.servers_new_only and 'new' or '#new')
    print (self.require_all and 'all' or '#all')
    print
    print
    print '### The following stuff can usually be ignored. ###'
    print
    print '# Includes (should usually be at the top of the file)'
    print '#optfile=/path/to/common/settings'
    print
    print '# Front-end Options:'
    print (self.isfrontend and 'isfrontend' or '#isfrontend')
    comment = (self.isfrontend and '' or '#')
    print (self.server_host and '%shost=%s' % (comment, self.server_host) or '#host=machine.domain.com')
    print '%sports=%s' % (comment, ','.join(['%s' % x for x in self.server_ports] or []))
    print '%sprotos=%s' % (comment, ','.join(['%s' % x for x in self.server_protos] or []))
    # FIXME: --register ?
    print (self.auth_domain and '%sauthdomain=%s' % (comment, self.auth_domain) or '#authdomain=foo.com')
    for bid in self.backends:
      be = self.backends[bid]
      if not be[BE_BACKEND]:
        print 'domain=%s:%s:%s' % (bid, be[BE_SECRET])
    print '#domain=http:*.pagekite.me:SECRET1'  
    print '#domain=http,https:THEM.pagekite.me:SECRET2'  

    print
    print '# Systems administration settings:'
    print (self.logfile and 'logfile=%s' % self.logfile or '#logfile=/path/file')
    print (self.daemonize and 'daemonize' % self.logfile or '#daemonize')
    if self.setuid and self.setgid:
      print 'runas=%s:%s' % (self.setuid, self.setgid)
    elif self.setuid:
      print 'runas=%s' % self.setuid
    else:
      print '#runas=uid:gid'
    print (self.pidfile and 'pidfile=%s' % self.pidfile or '#pidfile=/path/file')
    print

  def FallDown(self, message, help=True, noexit=False):
    if self.conns and self.conns.auth: self.conns.auth.quit()
    if self.ui_httpd: self.ui_httpd.quit()
    if help:
      print DOC
      print '*****'
    if message: print 'Error: %s' % message
    if not noexit: sys.exit(1)

  def GetBackendData(self, proto, domain, field, recurse=True):
    backend = '%s:%s' % (proto.lower(), domain.lower())
    if backend in self.backends: return self.backends[backend][field]  

    if recurse:
      dparts = domain.split('.')
      while len(dparts) > 1:
        dparts = dparts[1:]
        data = self.GetBackendData(proto, '.'.join(['*'] + dparts), field, recurse=False)
        if data: return data

    return None

  def GetBackendServer(self, proto, domain, recurse=True):
    server = self.GetBackendData(proto, domain, BE_BACKEND) 
    if server == '-': return None
    return server

  def IsSignatureValid(self, sign, secret, proto, domain, srand, token):
    return sign == signToken(token=sign, secret=secret,
                           payload='%s:%s:%s:%s' % (proto, domain, srand, token))

  def LookupDomainQuota(self, lookup):
    ip = socket.gethostbyname(lookup)

    # High bit not set, then access is granted and the "ip" is a quota.
    if not ip.startswith(AUTH_ERRORS):
      return 1024 # FIXME: Decode and return quota.
  
    # Errors on real errors are final.
    if ip != AUTH_ERR_USER_UNKNOWN: return None

    # User unknown, fall through to local test.
    return -1 

  def GetDomainQuota(self, proto, domain, srand, token, sign, recurse=True):
    if proto not in self.server_protos: return None

    bid = '%s:%s' % (proto, domain)
    data = '%s:%s' % (bid, srand)
    if not token or token == signToken(token=token, payload=data):
      if self.auth_domain:
        lookup = '.'.join([srand, token, sign, proto, domain, self.auth_domain])
        try:
          rv = self.LookupDomainQuota(lookup)
          if rv is None or rv >= 0: return rv
        except Exception:
          # Lookup failed, fall through to local test.
          pass

      secret = self.GetBackendData(proto, domain, BE_SECRET)
      if secret:
        if self.IsSignatureValid(sign, secret, proto, domain, srand, token):
          return 1024
        else:
          return None

    return None

  def ConfigureFromFile(self, filename=None):
    if not filename: filename = self.rcfile

    if self.rcfile_recursion > 25: 
      raise ConfigError('Nested too deep: %s' % filename)

    optfile = open(filename) 
    args = []
    for line in optfile:
      line = line.strip()
      if line and not line.startswith('#'):
        if not line.startswith('-'): line = '--%s' % line
        args.append(line)

    self.rcfile_recursion += 1
    self.Configure(args)
    self.rcfile_recursion -= 1
    return self

  def HelpAndExit(self):
    print DOC
    sys.exit(0)

  def Configure(self, argv):
    opts, args = getopt.getopt(argv, OPT_FLAGS, OPT_ARGS) 

    for opt, arg in opts:
      if opt in ('-o', '--optfile'): self.ConfigureFromFile(arg) 

      elif opt in ('-I', '--pidfile'): self.pidfile = arg
      elif opt in ('-L', '--logfile'): self.logfile = arg
      elif opt in ('-Z', '--daemonize'): self.daemonize = True
      elif opt in ('-U', '--runas'):
        import pwd
        import grp
        parts = arg.split(':')
        if len(parts) > 1:
          self.setuid, self.setgid = (pwd.getpwnam(parts[0])[2],
                                      grp.getgrnam(parts[1])[2])
        else:
          self.setuid = pwd.getpwnam(parts[0])[2]

      elif opt in ('-X', '--httppass'): self.ui_password = arg
      elif opt in ('-H', '--httpd'):
        parts = arg.split(':')
        host = parts[0] or 'localhost'
        if len(parts) > 1: 
          self.ui_sspec = (host, int(parts[1]))
        else:
          self.ui_sspec = (host, 80)

      elif opt in ('-D', '--dyndns'):
        if arg.startswith('http'):
          self.dyndns = (arg, {'user': '', 'pass': ''})
        elif '@' in arg:
          usrpwd, provider = arg.split('@', 1)
          if provider in DYNDNS: provider = DYNDNS[provider]
          if ':' in usrpwd:
            usr, pwd = usrpwd.split(':', 1)
            self.dyndns = (provider, {'user': usr, 'pass': pwd})
          else:
            self.dyndns = (provider, {'user': usrpwd, 'pass': ''})
        else:
          if arg in DYNDNS: arg = DYNDNS[arg]
          self.dyndns = (arg, {'user': '', 'pass': ''})

      elif opt in ('-p', '--ports'): self.server_ports = [int(x) for x in arg.split(',')]
      elif opt == '--protos': self.server_protos = [x.lower() for x in arg.split(',')]
      elif opt in ('-h', '--host'): self.server_host = arg
      elif opt in ('-A', '--authdomain'): self.auth_domain = arg
      elif opt in ('-f', '--isfrontend'): self.isfrontend = True

      elif opt in ('-a', '--all'): self.require_all = True
      elif opt in ('-N', '--new'): self.servers_new_only = True
      elif opt in ('--socksify', '--torify'): 
        try:
          import socks
          (host, port) = arg.split(':')
          socks.setdefaultproxy(socks.PROXY_TYPE_SOCKS5, host, int(port))
          self.socks_server = (host, port)
        except Exception, e:
          raise ConfigError("Please instally SocksiPy: "
                            " http://code.google.com/p/socksipy-branch/")

        if opt == '--torify':
          self.servers_new_only = True  # Disable initial DNS lookups (leaks)
          self.servers_no_ping = True   # Disable front-end pings
          self.crash_report_url = None  # Disable crash reports
          socks.wrapmodule(urllib)      # Make DynDNS updates go via tor

      elif opt == '--frontend': self.servers_manual.append(arg)
      elif opt == '--frontends':
        count, domain, port = arg.split(':')
        self.servers_auto = (int(count), domain, int(port))

      elif opt == '--backend':
        protos, domain, bhost, bport, secret = arg.split(':')
        for proto in protos.split(','): 
          bid = '%s:%s' % (proto.lower(), domain.lower())
          backend = '%s:%s' % (bhost.lower(), bport)
          self.backends[bid] = (proto.lower(), domain.lower(), backend, secret)

      elif opt == '--domain':
        protos, domain, secret = arg.split(':')
        for proto in protos.split(','): 
          bid = '%s:%s' % (proto, domain)
          self.backends[bid] = (proto, domain, None, secret)

      elif opt == '--nofrontend': self.isfrontend = False
      elif opt == '--nodaemonize': self.daemonize = False
      elif opt == '--noall': self.require_all = False
      elif opt == '--nozchunks': self.disable_zchunks = True
      elif opt == '--buffers': self.buffer_max = int(arg)
      elif opt == '--nocrashreport': self.crash_report_url = None
      elif opt == '--clean': pass
      elif opt == '--noloop': pass

      elif opt == '--defaults':
        self.ui_sspec = ('127.0.0.1', 9999) 
        self.dyndns = (DYNDNS['pagekite.net'], {'user': '', 'pass': ''})
        self.servers_auto = (1, 'frontends.b5p.us', 2222)

      elif opt == '--settings':
        self.PrintSettings()
        sys.exit(0)

      else:
        self.HelpAndExit()

    return self

  def CheckConfig(self):
    if not self.servers_manual and not self.servers_auto and not self.isfrontend:
      if not self.servers:
        raise ConfigError('Nothing to do!  List some servers, or run me as one.')      
    return self
          
  def CheckAllTunnels(self, conns):
    missing = []
    for backend in self.backends:
      if conns.Tunnel(domain) is None:
        missing.append(domain)
    if missing:
      self.FallDown('No tunnel for %s' % missing, help=False) 

  def Ping(self, host, port):
    if self.servers_no_ping: return 0

    start = time.time() 
    try:
      rawsocket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
    except Exception, e:
      LogDebug('Ping %s:%s failed: %s' % (host, port, e))
      return 100000 
    return (time.time() - start)

  def GetHostIpAddr(self, host):
    return socket.gethostbyname(host)

  def GetHostDetails(self, host):
    return socket.gethostbyname_ex(host)
 
  def ChooseFrontEnds(self):
    self.servers = []
    self.servers_preferred = []

    # Convert the hostnames into IP addresses...
    for server in self.servers_manual:
      (host, port) = server.split(':')
      try:
        ipaddr = self.GetHostIpAddr(host)
        server = '%s:%s' % (ipaddr, port)
        if server not in self.servers:
          self.servers.append(server)
          self.servers_preferred.append(ipaddr)
      except Exception, e:
        LogDebug('FIXME: Should narrow this down: %s' % e)

    # Lookup and choose from the auto-list (and our old domain).
    if self.servers_auto:
      (count, domain, port) = self.servers_auto

      # First, check for old addresses and always connect to those.
      if not self.servers_new_only:
        for bid in self.backends: 
          (proto, bdom) = bid.split(':')
          try:
            (hn, al, ips) = self.GetHostDetails(bdom)
            for ip in ips:
              server = '%s:%s' % (ip, port)
              if server not in self.servers: self.servers.append(server)
          except Exception, e:
            LogDebug('FIXME: Self lookup: %s, %s' % (bdom, e))

      try:
        (hn, al, ips) = socket.gethostbyname_ex(domain)
        times = [self.Ping(ip, port) for ip in ips]
        while count > 0 and ips:
          count -= 1
          mIdx = times.index(min(times)) 
          server = '%s:%s' % (ips[mIdx], port)
          if server not in self.servers:
            self.servers.append(server)
          if ips[mIdx] not in self.servers_preferred:
            self.servers_preferred.append(ips[mIdx])
          del times[mIdx]
          del ips[mIdx]
      except Exception, e:
        LogDebug('FIXME: Should narrow this down: %s' % e)

  def CreateTunnels(self, conns):
    live_servers = conns.TunnelServers()
    failures = 0
    connections = 0

    if self.backends:
      if not self.servers or len(self.servers) > len(live_servers):
        self.ChooseFrontEnds()

    for server in self.servers:
      if server not in live_servers:
        if Tunnel.BackEnd(server, self.backends, self.require_all, conns):
          Log([('connect', server)])
          connections += 1
        else:
          failures += 1
          Log([('err', 'Failed to connect'), ('FE', server)])

    if self.dyndns:
      updates = {}
      ddns_fmt, ddns_args = self.dyndns

      for bid in self.backends.keys():
        proto, domain = bid.split(':')
        if bid in conns.tunnels:
          ips = []
          bips = []
          for tunnel in conns.tunnels[bid]:
            ip = tunnel.server_name.split(':')[0]
            if not self.servers_preferred or ip in self.servers_preferred:
              ips.append(ip)
            else:
              bips.append(ip)
   
          if not ips: ips = bips

          if ips:
            iplist = ','.join(ips)
            payload = '%s:%s' % (domain, iplist)
            args = {}
            args.update(ddns_args)
            args.update({
              'domain': domain, 
              'ip': ips[0],
              'ips': iplist,
              'sign': signToken(secret=self.backends[bid][BE_SECRET],
                                payload=payload, length=100)
            })
            # FIXME: This may fail if different front-ends support different
            #        protocols. In practice, this should be rare.
            update = ddns_fmt % args
            if domain not in updates or len(update) < len(updates[domain]):
              updates[payload] = update

      last_updates = self.last_updates
      self.last_updates = []
      for update in updates:
        if update not in last_updates:
          try:
            result = ''.join(urllib.urlopen(updates[update]).readlines())
            self.last_updates.append(update)
            if result.startswith('good') or result.startswith('nochg'):
              Log([('dyndns', result), ('data', update)])
            else:
              LogError('DynDNS update failed: %s' % result, [('data', update)])
              failures += 1
          except Exception, e:
            LogError('DynDNS update failed: %s' % e, [('data', update)])
            failures += 1
      if not self.last_updates:
        self.last_updates = last_updates

    return failures

  def LogTo(self, filename):
    if filename == 'syslog':
      global Log
      Log = LogSyslog
      filename = '/dev/null'
      syslog.openlog((sys.argv[0] or 'pagekite.py').split('/')[-1],
                     syslog.LOG_PID, syslog.LOG_DAEMON)

    for fd in range(0, 1024): # Not MAXFD, but should be enough.
      try:
        os.close(fd)
      except Exception: # ERROR, fd wasn't open to begin with (ignored)
        pass  

    os.open(filename, os.O_RDWR | os.O_APPEND | os.O_CREAT)
    os.dup2(0, 1)
    os.dup2(0, 2)

  def Daemonize(self):
    # Fork once...
    if os.fork() != 0: os._exit(0)

    # Fork twice...
    os.setsid()
    if os.fork() != 0: os._exit(0)

  def SelectLoop(self):
    global buffered_bytes

    conns = self.conns
    last_tick = time.time()
    last_loop = time.time()
    retry = 5

    self.looping = True
    while self.looping:
      try:
        iready, oready, eready = select.select(conns.Sockets(),
                                               conns.Blocked(), [], 5)
      except KeyboardInterrupt, e:
        raise KeyboardInterrupt()
      except Exception, e:
        LogError('Select error: %s' % e)
        conns.CleanFds()
        
      now = time.time()
      if not iready and not oready:
        if now < last_loop + 1:
          LogError('Spinning')

        if now > last_tick + retry:
          last_tick = now

          # Reconnect if necessary, randomized exponential fallback.
          if self.CreateTunnels(conns) > 0:
            retry += random.random()*retry
            if retry > 300: retry = 300
          else:
            retry = 5

      for socket in oready:
        conn = conns.Connection(socket)
        if conn: conn.Send([])

      for socket in iready:
        conn = conns.Connection(socket)
        if buffered_bytes < 1024 * self.buffer_max:
          if conn and conn.ReadData() is False:
            conn.Cleanup()
            conns.Remove(conn)
        else:
          # Pause to let buffers clear...
          time.sleep(0.1)

      last_loop = now

    ## NOT REACHED ##

  def Loop(self):
    self.conns.start()
    if self.ui_httpd: self.ui_httpd.start()

    try:
      epoll = select.epoll()
    except Exception, msg:
      epoll = None 

    if epoll: LogDebug("FIXME: Should try epoll!")
    self.SelectLoop()

  def Start(self):
    conns = self.conns = Connections(self)

    # Create log-file
    if self.logfile:
      self.LogTo(self.logfile)

    # Set up our listeners if we are a server.
    if self.isfrontend:
      for port in self.server_ports:
        Listener(self.server_host, port, conns)

    # Start the UI thread
    if self.ui_sspec:
      # FIXME: ui_password, ui_pemfile
      self.ui_httpd = HttpUiThread(self, conns,
                                   handler=self.ui_request_handler,
                                   server=self.ui_http_server)

    # Daemonize!
    if self.daemonize:
      self.Daemonize()

    # Create PID file
    if self.pidfile:
      pf = open(self.pidfile, 'w')
      pf.write('%s\n' % os.getpid())
      pf.close()

    # Do this after creating the PID and log-files.
    if self.daemonize: os.chdir('/')

    # Drop privileges, if we have any.
    if self.setgid: os.setgid(self.setgid)
    if self.setuid: os.setuid(self.setuid)
    if self.setuid or self.setgid:
      Log([('uid', os.getuid()), ('gid', os.getgid())])

    # Next, create all the tunnels.
    self.CreateTunnels(conns)

    # Make sure we have what we need
    if self.require_all: self.CheckAllTunnels(conns)

    # Finally, run our select/epoll loop.
    Log([('started', 'pagekite.py'), ('version', APPVER)])
    self.Loop()

    Log([('stopping', 'pagekite.py')])
    if self.conns and self.conns.auth: self.conns.auth.quit()
    if self.ui_httpd: self.ui_httpd.quit()


##[ Main ]#####################################################################

if __name__ == '__main__':

  noexit = ('--noloop' not in sys.argv)
  crashes = 1

  while True:
    pk = PageKite()
    try:
      try:
        if '--clean' not in sys.argv:
          if os.path.exists(pk.rcfile): pk.ConfigureFromFile()
        pk.Configure(sys.argv[1:])
        pk.CheckConfig()
        pk.Start()

      except (ValueError, ConfigError, getopt.GetoptError), msg:
        pk.FallDown(msg)

      except KeyboardInterrupt, msg:
        pk.FallDown(None, help=False)

    except SystemExit:
      sys.exit(1)

    except Exception, msg:
      if pk.crash_report_url:
        try:
          print 'Submitting crash report to %s' % pk.crash_report_url
          LogDebug(''.join(urllib.urlopen(pk.crash_report_url, 
                                          urllib.urlencode({ 
                                            'crash': traceback.format_exc() 
                                          })).readlines()))
        except Exception:
          pass

      traceback.print_exc(file=sys.stderr)
      pk.FallDown(msg, help=False, noexit=noexit)

      # If we get this far, then we're looping. Clean up.
      for fd in pk.conns.Sockets(): fd.close()

      # Exponential fall-back.
      LogDebug('Restarting in %d seconds...' % (2 ** crashes))
      time.sleep(2 ** crashes)
      crashes += 1
      if crashes > 9: crashes = 9

# vi:ts=2 expandtab