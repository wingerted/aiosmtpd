.. _smtp:

================
 The SMTP class
================

At the heart of this module is the ``SMTP`` class.  This class implements the
`RFC 5321 <http://www.faqs.org/rfcs/rfc5321.html>`_ Simple Mail Transport
Protocol.  Often you won't run an ``SMTP`` instance directly, but instead will
use a :ref:`controller <controller>` instance to run the server in a subthread.

    >>> from aiosmtpd.controller import Controller

The ``SMTP`` class is itself a subclass of StreamReaderProtocol_.


.. _subclass:

Subclassing
===========

While behavior for common SMTP commands can be specified using :ref:`handlers
<handlers>`, more complex specializations such as adding custom SMTP commands
require subclassing the ``SMTP`` class.

For example, let's say you wanted to add a new SMTP command called ``PING``.
All methods implementing ``SMTP`` commands are prefixed with ``smtp_``; they
must also be coroutines.  Here's how you could implement this use case::

    >>> import asyncio
    >>> from aiosmtpd.smtp import SMTP as Server, syntax
    >>> class MyServer(Server):
    ...     @syntax('PING [ignored]')
    ...     async def smtp_PING(self, arg):
    ...         await self.push('259 Pong')

Now let's run this server in a controller::

    >>> from aiosmtpd.handlers import Sink
    >>> class MyController(Controller):
    ...     def factory(self):
    ...         return MyServer(self.handler)

    >>> controller = MyController(Sink())
    >>> controller.start()

..
    >>> # Arrange for the controller to be stopped at the end of this doctest.
    >>> ignore = resources.callback(controller.stop)

We can now connect to this server with an ``SMTP`` client.

    >>> from smtplib import SMTP as Client
    >>> client = Client(controller.hostname, controller.port)

Let's ping the server.  Since the ``PING`` command isn't an official ``SMTP``
command, we have to use the lower level interface to talk to it.

    >>> code, message = client.docmd('PING')
    >>> code
    259
    >>> message
    b'Pong'

Because we prefixed the ``smtp_PING()`` method with the ``@syntax()``
decorator, the command shows up in the ``HELP`` output.

    >>> print(client.help().decode('utf-8'))
    Supported commands: AUTH DATA EHLO HELO HELP MAIL NOOP PING QUIT RCPT RSET VRFY

And we can get more detailed help on the new command.

    >>> print(client.help('PING').decode('utf-8'))
    Syntax: PING [ignored]


Server hooks
============

.. warning:: These methods are deprecated.  See :ref:`handler hooks <hooks>`
             instead.

The ``SMTP`` server class also implements some hooks which your subclass can
override to provide additional responses.

``ehlo_hook()``
    This hook makes it possible for subclasses to return additional ``EHLO``
    responses.  This method, called *asynchronously* and taking no arguments,
    can do whatever it wants, including (most commonly) pushing new
    ``250-<command>`` responses to the client.  This hook is called just
    before the standard ``250 HELP`` which ends the ``EHLO`` response from the
    server.

``rset_hook()``
    This hook makes it possible to return additional ``RSET`` responses.  This
    method, called *asynchronously* and taking no arguments, is called just
    before the standard ``250 OK`` which ends the ``RSET`` response from the
    server.


SMTP API

.. class:: SMTP(handler, *, data_size_limit=33554432, enable_SMTPUTF8=False, decode_data=False, hostname=None, ident=None, tls_context=None, require_starttls=False, timeout=300, loop=None)

   *handler* is an instance of a :ref:`handler <handlers>` class.

   *data_size_limit* is the limit in number of bytes that is accepted for
   client SMTP commands.  It is returned to ESMTP clients in the ``250-SIZE``
   response.  The default is 33554432.

   *enable_SMTPUTF8* is a flag that when True causes the ESMTP ``SMTPUTF8``
   option to be returned to the client, and allows for UTF-8 content to be
   accepted.  The default is False.

   *decode_data* is a flag that when True, attempts to decode byte content in
   the ``DATA`` command, assigning the string value to the :ref:`envelope's
   <sessions_and_envelopes>` ``content`` attribute.  The default is False.

   *hostname* is the first part of the string returned in the ``220`` greeting
   response given to clients when they first connect to the server.  If not given,
   the system's fully-qualified domain name is used.

   *ident* is the second part of the string returned in the ``220`` greeting
   response that identifies the software name and version of the SMTP server
   to the client. If not given, a default Python SMTP ident is used.

   *tls_context* and *require_starttls*.  The ``STARTTLS`` option of ESMTP
   (and LMTP), defined in `RFC 3207`_, provides for secure connections to the
   server. For this option to be available, *tls_context* must be supplied,
   and *require_starttls* should be ``True``.  See :ref:`tls` for a more in
   depth discussion on enabling ``STARTTLS``.

   *timeout* is the number of seconds to wait between valid SMTP commands.
   After this time the connection will be closed by the server.  The default
   is 300 seconds, as per `RFC 2821`_.

   *loop* is the asyncio event loop to use.  If not given,
   :meth:`asyncio.new_event_loop()` is called to create the event loop.

   .. attribute:: event_handler

      The *handler* instance passed into the constructor.

   .. attribute:: data_size_limit

      The value of the *data_size_limit* argument passed into the constructor.

   .. attribute:: enable_SMTPUTF8

      The value of the *enable_SMTPUTF8* argument passed into the constructor.

   .. attribute:: hostname

      The ``220`` greeting hostname.  This will either be the value of the
      *hostname* argument passed into the constructor, or the system's fully
      qualified host name.

   .. attribute:: tls_context

      The value of the *tls_context* argument passed into the constructor.

   .. attribute:: require_starttls

      True if both the *tls_context* argument to the constructor was given
      **and** the *require_starttls* flag was True.

   .. attribute:: session

      The active :ref:`session <sessions_and_envelopes>` object, if there is
      one, otherwise None.

   .. attribute:: envelope

      The active :ref:`envelope <sessions_and_envelopes>` object, if there is
      one, otherwise None.

   .. attribute:: transport

      The active `asyncio transport`_ if there is one, otherwise None.

   .. attribute:: loop

      The event loop being used.  This will either be the given *loop*
      argument, or the new event loop that was created.

   .. method:: _create_session()

      A method subclasses can override to return custom ``Session`` instances.

   .. method:: _create_envelope()

      A method subclasses can override to return custom ``Envelope`` instances.

   .. method:: push(status)

      The method that subclasses and handlers should use to return statuses to
      SMTP clients.  This is a coroutine.  *status* can be a bytes object, but
      for convenience it is more likely to be a string.  If it's a string, it
      must be ASCII, unless *enable_SMTPUTF8* is True in which case it will be
      encoded as UTF-8.

   .. method:: smtp_<COMMAND>(arg)

      Coroutine methods implementing the SMTP protocol commands.  For example,
      ``smtp_HELO()`` implements the SMTP ``HELO`` command.  Subclasses can
      override these, or add new command methods to implement custom
      extensions to the SMTP protocol.  *arg* is the rest of the SMTP command
      given by the client, or None if nothing but the command was given.


.. _tls:

Enabling STARTTLS
=================

To enable `RFC 3207`_ ``STARTTLS``, you must supply the *tls_context* argument
to the :class:`SMTP` class.  *tls_context* is created with the
:meth:`ssl.create_default_context()` call from the ssl_ module, as follows::

    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)

The context must be initialized with a server certificate, private key, and/or
intermediate CA certificate chain with the
:meth:`ssl.SSLContext.load_cert_chain()` method.  This can be done with
separate files, or an all in one file.  Files must be in PEM format.

For example, if you wanted to use a self-signed certification for localhost,
which is easy to create but doesn't provide much security, you could use the
``openssl(1)`` command like so::

    $ openssl req -x509 -newkey rsa:4096 -keyout key.pem -out cert.pem -days 365 -nodes -subj '/CN=localhost'

and then in Python::

    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain('cert.pem', 'key.pem')

Now pass the ``context`` object to the *tls_context* argument in the ``SMTP``
constructor.

Note that a number of exceptions can be generated by these methods, and by SSL
connections, which you must be prepared to handle.  Additional documentation
is available in Python's ssl_ module, and should be reviewed before use; in
particular if client authentication and/or advanced error handling is desired.

If *require_starttls* is ``True``, a TLS session must be initiated for the
server to respond to any commands other than ``EHLO``/``LHLO``, ``NOOP``,
``QUIT``, and ``STARTTLS``.

If *require_starttls* is ``False`` (the default), use of TLS is not required;
the client *may* upgrade the connection to TLS, or may use any supported
command over an insecure connection.
=======
.. _hooks:

Handler hooks
=============

Handlers can implement hooks that get called during the SMTP dialog, or in
exceptional cases.  These *handler hooks* are all called asynchronously and
they *must* return a status string, such as ``'250 OK'``.  All handler hooks
are optional and certain default behaviors are carried out by the ``SMTP``
class when a hook is omitted, but when handler hooks are defined, they may
have additional responsibilities, as described below.

All handler hooks take at least three arguments, the ``SMTP`` server instance,
:ref:`a session instance, and an envelope instance <sessions_and_envelopes>`.
Some methods take additional arguments.

The following hooks are currently defined:

``handle_HELO(server, session, envelope, hostname)``
    Called during ``HELO``.  The ``hostname`` argument is the host name given
    by the client in the ``HELO`` command.  If implemented, this hook must
    also set the ``session.host_name`` attribute.

``handle_EHLO(server, session, envelope, hostname)``
    Called during ``EHLO``.  The ``hostname`` argument is the host name given
    by the client in the ``EHLO`` command.  If implemented, this hook must
    also set the ``session.host_name`` attribute.  This hook may push
    additional ``250-<command>`` responses to the client by yielding from
    ``server.push(status)`` before returning ``250 OK`` as the final response.

``handle_NOOP(server, session, envelope)``
    Called during ``NOOP``.

``handle_QUIT(server, session, envelope)``
    Called during ``QUIT``.

``handle_VRFY(server, session, envelope, address)``
    Called during ``VRFY``.  The ``address`` argument is the parsed email
    address given by the client in the ``VRFY`` command.

``handle_MAIL(server, session, envelope, address, mail_options)``
    Called during ``MAIL FROM``.  The ``address`` argument is the parsed email
    address given by the client in the ``MAIL FROM`` command, and
    ``mail_options`` are any additional ESMTP mail options providing by the
    client.  If implemented, this hook must also set the
    ``envelope.mail_from`` attribute and it may extend
    ``envelope.mail_options`` (which is always a Python list).

``handle_RCPT(server, session, envelope, address, rcpt_options)``
    Called during ``RCPT TO``.  The ``address`` argument is the parsed email
    address given by the client in the ``RCPT TO`` command, and
    ``rcpt_options`` are any additional ESMTP recipient options providing by
    the client.  If implemented, this hook should append the address to
    ``envelope.rcpt_tos`` and may extend ``envelope.rcpt_options`` (both of
    which are always Python lists).

``handle_RSET(server, session, envelope)``
    Called during ``RSET``.

``handle_DATA(server, session, envelope)``
    Called during ``DATA`` after the entire message (`"SMTP content"
    <https://tools.ietf.org/html/rfc5321#section-2.3.9>`_ as described in
    RFC 5321) has been received.  The content is available on the ``envelope``
    object, but the values are dependent on whether the ``SMTP`` class was
    instantiated with ``decode_data=False`` (the default) or
    ``decode_data=True``.  In the former case, both ``envelope.content`` and
    ``envelope.original_content`` will be the content bytes (normalized
    according to the transparency rules in `RFC 5321, $4.5.2
    <https://tools.ietf.org/html/rfc5321#section-4.5.2>`_).  In the latter
    case, ``envelope.original_content`` will be the normalized bytes, but
    ``envelope.content`` will be the UTF-8 decoded string of the original
    content.

In addition to the SMTP command hooks, the following hooks can also be
implemented by handlers.  These have a different APIs, and are called
synchronously.

``handle_STARTTLS(server, session, envelope)``
    If implemented, and if SSL is supported, this method gets called
    during the TLS handshake phase of ``connection_made()``.  It should return
    True if the handshake succeeded, and False otherwise.

``handle_AUTH(server, session, envelope, args)``
    Called during ``AUTH``. The ``args`` argument is a list of the client's
    provided arguments to the ``AUTH`` command (the first element being the
    ``AUTH`` method). For now the ``SMTP`` class advertise only ``PLAIN`` method
    as ``AUTH`` supported method (in ``HELP`` command) so this handler hook 
    *should* return a ``500`` error if ``handle_help`` is not implemented in
    handler.

``handle_exception(error)``
    If implemented, this method is called when any error occurs during the
    handling of a connection (e.g. if an ``smtp_<command>()`` method raises an
    exception).  The exception object is passed in.  This method *must* return
    a status string, such as ``'542 Internal server error'``.  If the method
    returns None or raises an exception, an exception will be logged, and a 500
    code will be returned to the client.


.. _sessions_and_envelopes:

Sessions and envelopes
======================

To make current and future hooks easier to write, two helper classes are
defined which provide attributes that can be of use to the
``handle_COMMAND()`` methods on the handler.  You can actually override the
use of these two classes by subclassing ``SMTP`` and defining the
``_create_session()`` and ``_create_envelope()`` methods.  Both of these
return the appropriate instance that will be used for the remainder of the
connection.  New session instances are created when new connections are made,
and new envelope instances are created at the beginning of an ``SMTP`` dialog,
or whenver a ``RSET`` command is issued.


Session
-------

``Session`` instances have the following attributes:

``peer``
    Defaulting to None, this attribute will contain the transport's socket's
    peername_ value.

``ssl``
    Defaulting to None, this attribute will contain some extra information,
    as a dictionary, from the ``asyncio.sslproto.SSLProtocol`` instance, which
    can be used to pull additional information out about the connection.  This
    attribute contains implementation-specific information so its contents may
    change, but it should roughly correspond to the information available
    `through this method`_.

``host_name``
    Defaulting to None, this attribute will contain the host name argument as
    seen by the ``HELO`` or ``EHLO`` command.

``extended_smtp``
    Defaulting to False, this flag will be True when the ``EHLO`` greeting
    was seen, indicating ESMTP_.

``loop``
    This is the asyncio event loop instance.


Envelope
--------

``Envelope`` instances have the following attributes:

``mail_from``
    Defaulting to None, this attribute holds the email address given in the
    ``MAIL FROM`` command.

``mail_options``
    Defaulting to None, this attribute contains a list of any ESMTP mail
    options provided by the client, such as those passed in by `the smtplib
    client`_.

``content``
    Defaulting to None, this attribute will contain the contents of the
    message as provided by the ``DATA`` command.  If the ``decode_data``
    parameter to the ``SMTP`` constructor was True (it defaults to False),
    then this attribute will contain the UTF-8 decoded string, otherwise it
    will contain the raw bytes.

``original_content``
    Defaulting to None, this attribute will contain the contents of the
    message as provided by the ``DATA`` command.  Unlike the ``content``
    attribute, this attribute will always contain the raw bytes.

``rcpt_tos``
    Defaulting to the empty list, this attribute will contain a list of the
    email addresses provided in the ``RCPT TO`` command.

``rcpt_options``
    Defaulting to the empty list, this attribute will contain the list of any
    recipient options provided by the client, such as those passed in by `the
    smtplib client`_.

If *tls_context* is not supplied, the ``STARTTLS`` option will not be
advertised, and the ``STARTTLS`` command will not be accepted.
*require_starttls* is meaningless in this case, and should be set to
``False``.

.. _StreamReaderProtocol: https://docs.python.org/3/library/asyncio-stream.html#streamreaderprotocol
.. _`RFC 3207`: http://www.faqs.org/rfcs/rfc3207.html
.. _`RFC 2821`: https://www.ietf.org/rfc/rfc2821.txt
.. _`asyncio transport`: https://docs.python.org/3/library/asyncio-protocol.html#asyncio-transport
.. _ssl: https://docs.python.org/3/library/ssl.html
