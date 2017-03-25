import socket
import asyncio
import logging
import collections

from email._header_value_parser import get_addr_spec, get_angle_addr
from email.errors import HeaderParseError
from public import public
from warnings import warn


try:
    import ssl
    from asyncio import sslproto
except ImportError:                                 # pragma: nocover
    _has_ssl = False
else:                                               # pragma: nocover
    _has_ssl = sslproto and hasattr(ssl, 'MemoryBIO')


__version__ = '1.0a4+'
__ident__ = 'Python SMTP {}'.format(__version__)
log = logging.getLogger('mail.log')


NEWLINE = '\n'
DATA_SIZE_DEFAULT = 33554432
EMPTYBYTES = b''


class Session:
    def __init__(self, loop):
        self.peer = None
        self.ssl = None
        self.host_name = None
        self.extended_smtp = False
        self.loop = loop


class Envelope:
    def __init__(self):
        self.mail_from = None
        self.mail_options = None
        self.smtp_utf8 = False
        self.content = None
        self.rcpt_tos = []
        self.rcpt_options = []


@public
class SMTP(asyncio.StreamReaderProtocol):
    command_size_limit = 512
    command_size_limits = collections.defaultdict(
        lambda x=command_size_limit: x)

    def __init__(self, handler,
                 *,
                 data_size_limit=DATA_SIZE_DEFAULT,
                 decode_data=False,
                 hostname=None,
                 tls_context=None,
                 require_starttls=False,
                 loop=None):
        self.__ident__ = __ident__
        self.loop = loop if loop else asyncio.get_event_loop()
        super().__init__(
            asyncio.StreamReader(loop=self.loop),
            client_connected_cb=self._client_connected_cb,
            loop=self.loop)
        self.event_handler = handler
        self.data_size_limit = data_size_limit
        self._decode_data = decode_data
        self.command_size_limits.clear()
        if hostname:
            self.hostname = hostname
        else:
            self.hostname = socket.getfqdn()
        self.tls_context = tls_context
        if tls_context:
            # Through rfc3207 part 4.1 certificate checking is part of SMTP
            # protocol, not SSL layer.
            self.tls_context.check_hostname = False
            self.tls_context.verify_mode = ssl.CERT_NONE
        self.require_starttls = tls_context and require_starttls
        self._tls_handshake_failed = False
        self._tls_protocol = None
        self.session = None
        self.envelope = None
        self.transport = None

    def _create_session(self):
        return Session(self.loop)

    def _create_envelope(self):
        return Envelope()

    @property
    def max_command_size_limit(self):
        try:
            return max(self.command_size_limits.values())
        except ValueError:
            return self.command_size_limit

    def connection_made(self, transport):
        # Reset state due to rfc3207 part 4.2.
        self._set_rset_state()
        self.session = self._create_session()
        self.session.peer = transport.get_extra_info('peername')
        is_instance = (_has_ssl and
                       isinstance(transport, sslproto._SSLProtocolTransport))
        if self.transport is not None and is_instance:   # pragma: nossl
            # It is STARTTLS connection over normal connection.
            self._reader._transport = transport
            self._writer._transport = transport
            self.transport = transport
            self.seen_greeting = ''
            # Do SSL certificate checking as rfc3207 part 4.1 says.
            # Why _extra is protected attribute?
            self.session.ssl = self._tls_protocol._extra
            if hasattr(self.event_handler, 'handle_tls_handshake'):
                auth = self.event_handler.handle_tls_handshake(self.session)
                self._tls_handshake_failed = not auth
            else:
                self._tls_handshake_failed = False
            self._over_ssl = True
        else:
            super().connection_made(transport)
            self.transport = transport
            log.info('Peer: %s', repr(self.session.peer))
            # Process the client's requests.
            self._connection_closed = False
            self._handler_coroutine = self.loop.create_task(
                self._handle_client())

    def connection_lost(self, exc):
        super().connection_lost(exc)
        self._writer.close()
        self._connection_closed = True

    def _client_connected_cb(self, reader, writer):
        # This is redundant since we subclass StreamReaderProtocol, but I like
        # the shorter names.
        self._reader = reader
        self._writer = writer

    def eof_received(self):
        self._handler_coroutine.cancel()
        return super().eof_received()

    def _set_post_data_state(self):
        """Reset state variables to their post-DATA state."""
        self.envelope = self._create_envelope()

    def _set_rset_state(self):
        """Reset all state variables except the greeting."""
        self._set_post_data_state()

    @asyncio.coroutine
    def push(self, msg):
        response = bytes(msg + '\r\n', encoding='ascii')
        self._writer.write(response)
        log.debug(response)
        yield from self._writer.drain()

    @asyncio.coroutine
    def handle_exception(self, error):
        if hasattr(self.event_handler, 'handle_exception'):
            yield from self.event_handler.handle_exception(error)

    @asyncio.coroutine
    def _handle_client(self):
        log.info('handling connection')
        yield from self.push(
            '220 {} {}'.format(self.hostname, self.__ident__))
        while not self._connection_closed:          # pragma: no branch
            # XXX Put the line limit stuff into the StreamReader?
            line = yield from self._reader.readline()
            try:
                # XXX this rstrip may not completely preserve old behavior.
                line = line.rstrip(b'\r\n')
                log.info('Data: %r', line)
                if not line:
                    yield from self.push('500 Error: bad syntax')
                    continue
                i = line.find(b' ')
                # Decode to string only the command name part, which must be
                # ASCII as per RFC.  If there is an argument, it is decoded to
                # UTF-8/surrogateescape so that non-UTF-8 data can be
                # re-encoded back to the original bytes when the SMTP command
                # is handled.
                if i < 0:
                    command = str(line.upper(), encoding='ascii')
                    arg = None
                else:
                    command = str(line[:i].upper(), encoding='ascii')
                    arg = line[i+1:].strip()
                    # Remote SMTP servers can send us UTF-8 content despite
                    # whether they've declared to do so or not.  Some old
                    # servers can send 8-bit data.  Use surrogateescape so
                    # that the fidelity of the decoding is preserved, and the
                    # original bytes can be retrieved.
                    arg = str(arg, encoding='utf-8', errors='surrogateescape')
                max_sz = (self.command_size_limits[command]
                          if self.session.extended_smtp
                          else self.command_size_limit)
                if len(line) > max_sz:
                    yield from self.push('500 Error: line too long')
                    continue
                if (self._tls_handshake_failed
                        and command != 'QUIT'):             # pragma: nossl
                    yield from self.push(
                        '554 Command refused due to lack of security')
                    continue
                if (self.require_starttls
                        and (not self._tls_protocol)
                        and (command not in ['EHLO', 'STARTTLS', 'QUIT'])):
                    # RFC3207 part 4
                    yield from self.push(
                        '530 Must issue a STARTTLS command first')
                    continue
                method = getattr(self, 'smtp_' + command, None)
                if not method:
                    yield from self.push(
                        '500 Error: command "%s" not recognized' % command)
                    continue
                yield from method(arg)
            except Exception as error:
                yield from self.push('500 Error: ({}) {}'.format(
                    error.__class__.__name__, str(error)))
                log.exception('SMTP session exception')
                yield from self.handle_exception(error)

    # SMTP and ESMTP commands
    @asyncio.coroutine
    def smtp_HELO(self, hostname):
        if not hostname:
            yield from self.push('501 Syntax: HELO hostname')
            return
        # See issue #21783 for a discussion of this behavior.
        if self.session.host_name:
            yield from self.push('503 Duplicate HELO/EHLO')
            return
        self._set_rset_state()
        self.session.host_name = hostname
        self.session.extended_smtp = False
        yield from self.push('250 %s' % self.hostname)

    @asyncio.coroutine
    def ehlo_hook(self):
        """Allow subclasses to extend EHLO responses.

        This hook is called just before the final, non-continuing
        `250 HELP` response.  Subclasses can add additional `250-<cmd>`
        responses for custom behavior.
        """
        pass

    @asyncio.coroutine
    def smtp_EHLO(self, arg):
        if not arg:
            yield from self.push('501 Syntax: EHLO hostname')
            return
        # See issue #21783 for a discussion of this behavior.
        if self.session.host_name:
            yield from self.push('503 Duplicate HELO/EHLO')
            return
        self._set_rset_state()
        self.session.host_name = arg
        self.session.extended_smtp = True
        yield from self.push('250-%s' % self.hostname)
        if self.data_size_limit:
            yield from self.push('250-SIZE %s' % self.data_size_limit)
            self.command_size_limits['MAIL'] += 26
        if not self._decode_data:
            yield from self.push('250-8BITMIME')
        yield from self.push('250-SMTPUTF8')
        self.command_size_limits['MAIL'] += 10
        if (self.tls_context and
                not self._tls_protocol and
                _has_ssl):                        # pragma: nossl
            yield from self.push('250-STARTTLS')
        yield from self.ehlo_hook()
        yield from self.push('250 HELP')

    @asyncio.coroutine
    def smtp_NOOP(self, arg):
        if arg:
            yield from self.push('501 Syntax: NOOP')
        else:
            yield from self.push('250 OK')

    @asyncio.coroutine
    def smtp_QUIT(self, arg):
        if arg:
            yield from self.push('501 Syntax: QUIT')
        else:
            yield from self.push('221 Bye')
            self._handler_coroutine.cancel()
            self.transport.close()

    @asyncio.coroutine
    def smtp_STARTTLS(self, arg):                   # pragma: nossl
        log.info('===> STARTTLS')
        if arg:
            yield from self.push('501 Syntax: STARTTLS')
            return
        if not (self.tls_context and _has_ssl):
            yield from self.push('454 TLS not available')
            return
        yield from self.push('220 Ready to start TLS')
        # Create SSL layer.
        self._tls_protocol = sslproto.SSLProtocol(
            self.loop,
            self,
            self.tls_context,
            None,
            server_side=True)
        # Reconfigure transport layer.
        socket_transport = self.transport
        socket_transport._protocol = self._tls_protocol
        # Reconfigure protocol layer. Cant understand why app transport is
        # protected property, if it MUST be used externally.
        self.transport = self._tls_protocol._app_transport
        # Start handshake.
        self._tls_protocol.connection_made(socket_transport)

    def _strip_command_keyword(self, keyword, arg):
        keylen = len(keyword)
        if arg[:keylen].upper() == keyword:
            return arg[keylen:].strip()
        return ''

    def _getaddr(self, arg):
        if not arg:
            return '', ''
        if arg.lstrip().startswith('<'):
            address, rest = get_angle_addr(arg)
        else:
            address, rest = get_addr_spec(arg)
        return address.addr_spec, rest

    def _getparams(self, params):
        # Return params as dictionary. Return None if not all parameters
        # appear to be syntactically valid according to RFC 1869.
        result = {}
        for param in params:
            param, eq, value = param.partition('=')
            if not param.isalnum() or eq and not value:
                return None
            result[param] = value if eq else True
        return result

    @asyncio.coroutine
    def smtp_HELP(self, arg):
        if arg:
            extended = ' [SP <mail-parameters>]'
            lc_arg = arg.upper()
            if lc_arg == 'EHLO':
                yield from self.push('250 Syntax: EHLO hostname')
            elif lc_arg == 'HELO':
                yield from self.push('250 Syntax: HELO hostname')
            elif lc_arg == 'MAIL':
                msg = '250 Syntax: MAIL FROM: <address>'
                if self.session.extended_smtp:
                    msg += extended
                yield from self.push(msg)
            elif lc_arg == 'RCPT':
                msg = '250 Syntax: RCPT TO: <address>'
                if self.session.extended_smtp:
                    msg += extended
                yield from self.push(msg)
            elif lc_arg == 'DATA':
                yield from self.push('250 Syntax: DATA')
            elif lc_arg == 'RSET':
                yield from self.push('250 Syntax: RSET')
            elif lc_arg == 'NOOP':
                yield from self.push('250 Syntax: NOOP')
            elif lc_arg == 'QUIT':
                yield from self.push('250 Syntax: QUIT')
            elif lc_arg == 'VRFY':
                yield from self.push('250 Syntax: VRFY <address>')
            else:
                yield from self.push(
                    '501 Supported commands: EHLO HELO MAIL RCPT '
                    'DATA RSET NOOP QUIT VRFY')
        else:
            yield from self.push(
                '250 Supported commands: EHLO HELO MAIL RCPT DATA '
                'RSET NOOP QUIT VRFY')

    @asyncio.coroutine
    def smtp_VRFY(self, arg):
        if arg:
            try:
                address, params = self._getaddr(arg)
            except HeaderParseError:
                address = None
            if address:
                yield from self.push(
                    '252 Cannot VRFY user, but will accept message '
                    'and attempt delivery')
            else:
                yield from self.push('502 Could not VRFY %s' % arg)
        else:
            yield from self.push('501 Syntax: VRFY <address>')

    @asyncio.coroutine
    def smtp_MAIL(self, arg):
        if not self.session.host_name:
            yield from self.push('503 Error: send HELO first')
            return
        log.debug('===> MAIL %s', arg)
        syntaxerr = '501 Syntax: MAIL FROM: <address>'
        if self.session.extended_smtp:
            syntaxerr += ' [SP <mail-parameters>]'
        if arg is None:
            yield from self.push(syntaxerr)
            return
        arg = self._strip_command_keyword('FROM:', arg)
        address, params = self._getaddr(arg)
        if not address:
            yield from self.push(syntaxerr)
            return
        if not self.session.extended_smtp and params:
            yield from self.push(syntaxerr)
            return
        if self.envelope.mail_from:
            yield from self.push('503 Error: nested MAIL command')
            return
        mail_options = params.upper().split()
        params = self._getparams(mail_options)
        if params is None:
            yield from self.push(syntaxerr)
            return
        if not self._decode_data:
            body = params.pop('BODY', '7BIT')
            if body not in ['7BIT', '8BITMIME']:
                yield from self.push(
                    '501 Error: BODY can only be one of 7BIT, 8BITMIME')
                return
        smtputf8 = params.pop('SMTPUTF8', False)
        if not isinstance(smtputf8, bool):
            yield from self.push('501 Error: SMTPUTF8 takes no arguments')
            return
        self.envelope.smtp_utf8 = smtputf8
        size = params.pop('SIZE', None)
        if size:
            if isinstance(size, bool) or not size.isdigit():
                yield from self.push(syntaxerr)
                return
            elif self.data_size_limit and int(size) > self.data_size_limit:
                yield from self.push(
                    '552 Error: message size exceeds fixed maximum message '
                    'size')
                return
        if len(params.keys()) > 0:
            yield from self.push(
                '555 MAIL FROM parameters not recognized or not implemented')
            return
        self.envelope.mail_from = address
        self.envelope.mail_options = mail_options
        log.info('sender: %s', address)
        yield from self.push('250 OK')

    @asyncio.coroutine
    def smtp_RCPT(self, arg):
        if not self.session.host_name:
            yield from self.push('503 Error: send HELO first')
            return
        log.debug('===> RCPT %s', arg)
        if not self.envelope.mail_from:
            yield from self.push('503 Error: need MAIL command')
            return
        syntaxerr = '501 Syntax: RCPT TO: <address>'
        if self.session.extended_smtp:
            syntaxerr += ' [SP <mail-parameters>]'
        if arg is None:
            yield from self.push(syntaxerr)
            return
        arg = self._strip_command_keyword('TO:', arg)
        address, params = self._getaddr(arg)
        if not address:
            yield from self.push(syntaxerr)
            return
        if not self.session.extended_smtp and params:
            yield from self.push(syntaxerr)
            return
        rcpt_options = params.upper().split()
        params = self._getparams(rcpt_options)
        if params is None:
            yield from self.push(syntaxerr)
            return
        # XXX currently there are no options we recognize.
        if len(params) > 0:
            yield from self.push(
                '555 RCPT TO parameters not recognized or not implemented')
            return
        self.envelope.rcpt_tos.append(address)
        self.envelope.rcpt_options.append(rcpt_options)
        log.info('recip: %s', address)
        yield from self.push('250 OK')

    @asyncio.coroutine
    def rset_hook(self):
        """Allow subclasses to hook into the RSET command."""
        pass

    @asyncio.coroutine
    def smtp_RSET(self, arg):
        if arg:
            yield from self.push('501 Syntax: RSET')
            return
        self._set_rset_state()
        yield from self.rset_hook()
        yield from self.push('250 OK')

    @asyncio.coroutine
    def smtp_DATA(self, arg):
        if not self.session.host_name:
            yield from self.push('503 Error: send HELO first')
            return
        if not self.envelope.rcpt_tos:
            yield from self.push('503 Error: need RCPT command')
            return
        if arg:
            yield from self.push('501 Syntax: DATA')
            return
        yield from self.push('354 End data with <CR><LF>.<CR><LF>')
        data = []
        num_bytes = 0
        size_exceeded = False
        while not self._connection_closed:          # pragma: no branch
            line = yield from self._reader.readline()
            if line == b'.\r\n':
                if data:
                    data[-1] = data[-1].rstrip(b'\r\n')
                break
            num_bytes += len(line)
            if (not size_exceeded and
                    self.data_size_limit and
                    num_bytes > self.data_size_limit):
                size_exceeded = True
                yield from self.push('552 Error: Too much mail data')
            if not size_exceeded:
                data.append(line)
        if size_exceeded:
            self._set_post_data_state()
            return
        # Remove extraneous carriage returns and de-transparency
        # according to RFC 5321, Section 4.5.2.
        for i in range(len(data)):
            text = data[i]
            if text and text[:1] == b'.':
                data[i] = text[1:]
        received_data = EMPTYBYTES.join(data)
        if self._decode_data:
            received_data = received_data.decode(
                'utf-8', errors='surrogateescape')
        self.envelope.content = received_data
        # Call the new API first if it's implemented.
        if hasattr(self.event_handler, 'handle_DATA'):
            args = (self.session, self.envelope)
            status = yield from self.event_handler.handle_DATA(*args)
        else:
            warn('Use handler.handle_DATA instead of .process_message()',
                 DeprecationWarning)
            args = (self.session.peer, self.envelope.mail_from,
                    self.envelope.rcpt_tos, self.envelope.content)
            if asyncio.iscoroutinefunction(self.event_handler.process_message):
                status = yield from self.event_handler.process_message(*args)
            else:
                status = self.event_handler.process_message(*args)
        self._set_post_data_state()
        if status:
            yield from self.push(status)
        else:
            yield from self.push('250 OK')

    # Commands that have not been implemented
    @asyncio.coroutine
    def smtp_EXPN(self, arg):
        yield from self.push('502 EXPN not implemented')
