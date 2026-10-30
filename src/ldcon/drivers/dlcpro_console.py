"""Bounded DLC Telnet framing, without telnetlib or terminal emulation."""
import socket
import time
from ldcon.drivers.dlcpro_priority import PriorityLock

from ldcon.core.contracts import DeviceError


class UncertainDLCWrite(DeviceError):
    """A write may have reached the controller; never replay automatically."""


class Console:
    def __init__(self, stream, *, max_reply_bytes=262144, timeout_s=2.):
        self.stream = stream
        self.max_reply_bytes = max_reply_bytes
        self.timeout_s = timeout_s
        self.lock = PriorityLock()
        self.broken = False
        self._telnet_state = 'data'
        self._verb = None
        self.greeting = self._reply()

    @classmethod
    def connect(cls, host, *, port=1998, timeout_s=2.):
        stream = socket.create_connection((host, port), timeout=timeout_s)
        stream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            console = cls(stream, timeout_s=timeout_s)
            # FW 1.9 echoes each character despite rejecting Telnet ECHO.
            # This console-local setting avoids per-character reply overhead;
            # it changes no laser parameter and leaves other consoles alone.
            if console.command("(param-set! 'echo #f)") != '0':
                raise DeviceError('DLC console echo disable rejected')
            if console.command("(param-ref 'echo)") != '#f':
                raise DeviceError('DLC console echo disable not verified')
            return console
        except BaseException:
            stream.close()
            raise

    def _decode_telnet(self, raw):
        text = bytearray()
        for byte in raw:
            state = self._telnet_state
            if state == 'data':
                if byte == 255:
                    self._telnet_state = 'iac'
                else:
                    text.append(byte)
            elif state == 'iac':
                if byte == 255:
                    text.append(byte)
                    self._telnet_state = 'data'
                elif byte in (251, 252, 253, 254):
                    self._verb = byte
                    self._telnet_state = 'option'
                elif byte == 250:
                    self._telnet_state = 'subnegotiation'
                else:
                    self._telnet_state = 'data'
            elif state == 'option':
                # We need no terminal options. Reject WILL with DONT and DO with
                # WONT; acknowledgments are ignored to avoid negotiation loops.
                if self._verb in (251, 253):
                    self.stream.sendall(bytes((255, 254 if self._verb == 251 else 252, byte)))
                self._telnet_state = 'data'
            elif state == 'subnegotiation':
                if byte == 255:
                    self._telnet_state = 'subnegotiation_iac'
            elif state == 'subnegotiation_iac':
                self._telnet_state = 'data' if byte == 240 else 'subnegotiation'
        return text

    def _reply(self):
        result = bytearray()
        received = 0
        deadline = time.monotonic() + self.timeout_s
        while not ((result == b'> ' or result.endswith(b'\n> '))
                   and self._telnet_state == 'data'):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('DLC reply deadline exceeded')
            if hasattr(self.stream, 'settimeout'):
                self.stream.settimeout(remaining)
            block = self.stream.recv(4096)
            if not block:
                raise ConnectionError('DLC console disconnected before prompt')
            self._quick_ack()
            received += len(block)
            if received > self.max_reply_bytes:
                raise DeviceError('DLC reply exceeds framing bound')
            result.extend(self._decode_telnet(block))
        return result[:-2].replace(b'\r\x00', b'\r').decode('utf-8').strip()

    def _quick_ack(self):
        # Linux QUICKACK is transient: request it for each received fragment.
        # This can unblock a peer's small remaining reply/prompt under Nagle;
        # our TCP_NODELAY only controls requests sent from this socket. It is
        # an optional transport optimization, never a change to reply bounds.
        option = getattr(socket, 'TCP_QUICKACK', None)
        configure = getattr(self.stream, 'setsockopt', None)
        if option is not None and configure is not None:
            try:
                configure(socket.IPPROTO_TCP, option, 1)
            except OSError:
                pass  # Unsupported socket/platform keeps the original framing.

    def command(self, expression, *, writing=False):
        if not isinstance(expression, str) or any(c in expression for c in '\r\n\x00'):
            raise ValueError('DLC command must be one line')
        with self.lock:
            if self.broken:
                raise DeviceError('DLC console requires inspection/reconnection')
            try:
                if hasattr(self.stream, 'settimeout'):
                    self.stream.settimeout(self.timeout_s)
                self.stream.sendall((expression + '\n').encode('ascii'))
                reply = self._reply()
                lines = reply.splitlines()
                # Firmware 1.9 may echo even after ECHO negotiation is rejected.
                # Remove only the exact request on its own first line.
                if lines and lines[0] == expression:
                    reply = '\n'.join(lines[1:]).strip()
                return reply
            except Exception as error:
                self.broken = True
                if writing:
                    raise UncertainDLCWrite('DLC write outcome unknown; no replay') from error
                raise DeviceError('DLC read failed') from error

    def close(self):
        self.stream.close()
