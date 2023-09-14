"""Main module for note-python."""

##
# @mainpage Python Library for the Notecard
#
# @section intro_sec Introduction
# This module contains the core functionality for running the
# note-python library, including the main Notecard class, and
# Serial and I2C sub-classes.
#
# @section dependencies Dependencies
#
# This library requires a physical connection to a Notecard over I2C or
# Serial to be functional.
# @section author Author
#
# Written by Ray Ozzie and Brandon Satrom for Blues Inc.
# @section license License
#
# Copyright (c) 2019 Blues Inc. MIT License. Use of this source code is
# governed by licenses granted by the copyright holder including that found in
# the
# <a href="https://github.com/blues/note-python/blob/master/LICENSE">
#   LICENSE
# </a>
# file.

##
# @file notecard.py
#
# @brief Main module for note-python. Contains core library functionality.

import sys
import os
import json
import time
from .timeout import start_timeout, has_timed_out
from .transaction_manager import TransactionManager, NoOpTransactionManager

use_periphery = False
use_serial_lock = False

if sys.implementation.name == 'cpython' and (sys.platform == 'linux' or sys.platform == 'linux2'):

    use_periphery = True
    from periphery import I2C

    use_serial_lock = True
    from filelock import FileLock
    from filelock import Timeout as SerialLockTimeout
else:
    class SerialLockTimeout(Exception):
        """A null SerialLockTimeout for when use_serial_lock is False."""

        pass

NOTECARD_I2C_ADDRESS = 0x17

# The notecard is a real-time device that has a fixed size interrupt buffer.
# We can push data at it far, far faster than it can process it,
# therefore we push it in segments with a pause between each segment.
CARD_REQUEST_SEGMENT_MAX_LEN = 250
# "a 250ms delay is required to separate "segments", ~256 byte
# I2C transactions." See
# https://dev.blues.io/guides-and-tutorials/notecard-guides/serial-over-i2c-protocol/#data-write
CARD_REQUEST_SEGMENT_DELAY_MS = 250
# "A 20ms delay is commonly used to separate smaller I2C transactions known as
# 'chunks'". See the same document linked above.
I2C_CHUNK_DELAY_MS = 20


class NoOpContextManager:
    """A no-op context manager for use with NoOpSerialLock."""

    def __enter__(self):
        """No-op enter function. Required for context managers."""
        pass

    def __exit__(self, exc_type, exc_value, traceback):
        """No-op exit function. Required for context managers."""
        pass


class NoOpSerialLock():
    """A no-op serial lock class for when use_serial_lock is False."""

    def acquire(*args, **kwargs):
        """Acquire the no-op lock."""
        return NoOpContextManager()


def serial_lock(fn):
    """Attempt to get a lock on the serial channel used for Notecard comms."""

    def decorator(self, *args, **kwargs):
        try:
            with self.lock.acquire(timeout=5):
                return fn(self, *args, **kwargs)
        except SerialLockTimeout:
            raise Exception('Notecard in use')

    return decorator


def i2c_lock(fn):
    """Attempt to get a lock on the I2C bus used for Notecard comms."""

    def decorator(self, *args, **kwargs):
        retries = 5
        while retries != 0:
            if self.lock():
                break

            retries -= 1
            # Try again after 100 ms.
            time.sleep(.1)

        if retries == 0:
            raise Exception('Failed to acquire I2C lock.')

        try:
            ret = fn(self, *args, **kwargs)
        finally:
            self.unlock()

        return ret

    return decorator


class Notecard:
    """Base Notecard class."""

    def __init__(self, debug=False):
        """Configure user agent."""
        self._user_agent_app = None
        self._user_agent_sent = False
        self._user_agent = {
            'agent': 'note-python',
            'os_name': sys.implementation.name,
            'os_platform': sys.platform,
            'os_version': sys.version
        }
        if sys.implementation.name == 'cpython':
            self._user_agent['os_family'] = os.name
        else:
            self._user_agent['os_family'] = os.uname().machine
        self._transaction_manager = NoOpTransactionManager()
        self._debug = debug

    def _prepare_request(self, req):
        """Prepare a request for transmission to the Notecard."""
        # Inspect the request for hub.set and add the User Agent.
        if 'hub.set' in req.values():
            # Merge the User Agent to send along with the hub.set request.
            req = req.copy()
            req.update({'body': self.GetUserAgent()})

            self._user_agent_sent = True

        # Serialize the JSON request to a string.
        req_string = json.dumps(req)
        if self._debug:
            print(req_string)

        req_string += "\n"

        # Encode the request string as UTF-8 bytes.
        return req_string.encode('utf-8')

    def Command(self, req):
        """Send a command to the Notecard. The Notecard response is ignored."""
        if 'cmd' not in req:
            raise Exception("Please use 'cmd' instead of 'req'")

        req_bytes = self._prepare_request(req)
        self._transact(req_bytes, False)

    def Transaction(self, req):
        """Perform a Notecard transaction and return the result."""
        req_bytes = self._prepare_request(req)
        rsp_bytes = self._transact(req_bytes, True)
        rsp_json = json.loads(rsp_bytes)
        if self._debug:
            print(rsp_json)

        return rsp_json

    def GetUserAgent(self):
        """Return the User Agent String for the host for debug purposes."""
        ua_copy = self._user_agent.copy()
        ua_copy.update(self._user_agent_app or {})
        return ua_copy

    def SetAppUserAgent(self, app_user_agent):
        """Set the User Agent info for the app."""
        self._user_agent_app = app_user_agent

    def UserAgentSent(self):
        """Return true if the User Agent has been sent to the Notecard."""
        return self._user_agent_sent

    def SetTransactionPins(self, rtx_pin, ctx_pin):
        """Set the pins used for RTX and CTX."""
        self._transaction_manager = TransactionManager(rtx_pin, ctx_pin)


class OpenSerial(Notecard):
    """Notecard class for Serial communication."""

    @serial_lock
    def _transact(self, req, rsp_expected):
        """Perform a low-level transaction with the Notecard."""
        rsp = None

        try:
            transaction_timeout_secs = 30
            self._transaction_manager.start(transaction_timeout_secs)

            seg_off = 0
            seg_left = len(req)
            while seg_left > 0:
                seg_len = seg_left
                if seg_len > CARD_REQUEST_SEGMENT_MAX_LEN:
                    seg_len = CARD_REQUEST_SEGMENT_MAX_LEN

                self.uart.write(req[seg_off:seg_off + seg_len])
                seg_off += seg_len
                seg_left -= seg_len
                time.sleep(CARD_REQUEST_SEGMENT_DELAY_MS / 1000)

            if rsp_expected:
                rsp = self.uart.readline()
        finally:
            self._transaction_manager.stop()

        return rsp

    def _read_byte_micropython(self):
        """Read a single byte from the Notecard (MicroPython)."""
        if not self.uart.any():
            return None
        return self.uart.read(1)

    def _read_byte_cpython(self):
        """Read a single byte from the Notecard (CPython)."""
        if self.uart.in_waiting == 0:
            return None
        return self.uart.read(1)

    def _read_byte_circuitpython(self):
        """Read a single byte from the Notecard (CircuitPython)."""
        return self.uart.read(1)

    @serial_lock
    def Reset(self):
        """Reset the Notecard."""
        notecard_ready = False
        for i in range(10):
            try:
                # Send a newline to the Notecard to terminate any partial
                # request that might be sitting in its input buffer.
                self.uart.write(b'\n')
            except:
                # Wait 500 ms and before trying to send the newline again.
                time.sleep(.5)
                continue

            something_found = False
            non_control_char_found = False
            # Drain serial for 500 ms.
            start = start_timeout()
            while not has_timed_out(start, 0.5):
                data = self._read_byte()
                # If data was read from the Notecard, inspect what we received.
                # If it isn't a \n or \r, the host and the Notecard aren't
                # synced up yet, and we'll need to retransmit the \n and try
                # again.
                while data is not None and data != b'':
                    something_found = True
                    if data[0] != ord('\n') and data[0] != ord('\r'):
                        non_control_char_found = True

                    data = self._read_byte()

                # If there was no data read from the Notecard, wait 1 ms and try
                # again. Keep doing this for 500 ms.
                time.sleep(.001)

            # If we received anything other than newlines from the Notecard, we
            # aren't in sync, yet.
            if something_found and not non_control_char_found:
                notecard_ready = True
                break

            # Wait 500 ms before trying again.
            time.sleep(.5)

        if not notecard_ready:
            raise Exception('Failed to reset Notecard.')

    def __init__(self, uart_id, debug=False):
        """Initialize the Notecard before a reset."""
        super().__init__(debug)
        self._user_agent['req_interface'] = 'serial'
        self._user_agent['req_port'] = str(uart_id)

        self.uart = uart_id

        if use_serial_lock:
            self.lock = FileLock('serial.lock')
        else:
            self.lock = NoOpSerialLock()

        if sys.implementation.name == 'micropython':
            self._read_byte = self._read_byte_micropython
        elif sys.implementation.name == 'cpython':
            self._read_byte = self._read_byte_cpython
        elif sys.implementation.name == 'circuitpython':
            self._read_byte = self._read_byte_circuitpython
        else:
            raise NotImplementedError(f'Unsupported platform: {sys.implementation.name}')

        self.Reset()


class OpenI2C(Notecard):
    """Notecard class for I2C communication."""

    def _write(self, data):
        write_length = bytearray(1)
        write_length[0] = len(data)

        # Send a message with the length of the incoming bytes followed
        # by the bytes themselves.
        self._platform_write(write_length, data)

    def _transmit(self, data):
        chunk_offset = 0
        data_left = len(data)
        sent_in_seg = 0

        while data_left > 0:
            chunk_len = min(data_left, self.max)
            write_data = data[chunk_offset:chunk_offset + chunk_len]

            self._write(write_data)

            chunk_offset += chunk_len
            data_left -= chunk_len
            sent_in_seg += chunk_len

            if sent_in_seg > CARD_REQUEST_SEGMENT_MAX_LEN:
                sent_in_seg -= CARD_REQUEST_SEGMENT_MAX_LEN
                time.sleep(CARD_REQUEST_SEGMENT_DELAY_MS / 1000)

            time.sleep(I2C_CHUNK_DELAY_MS / 1000)

    def _read(self, length):
        initiate_read = bytearray(2)
        # 0 indicates we are reading from the Notecard.
        initiate_read[0] = 0
        # This indicates how many bytes we are prepared to read.
        initiate_read[1] = length
        # read_buf is a buffer to store the data we're reading.
        # length accounts for the payload and the +2 is for the header. The
        # header sent by the Notecard has one byte to indicate the number of
        # bytes still available to read and a second byte to indicate the number
        # of bytes coming in the current chunk.
        read_buf = bytearray(length + 2)

        return self._platform_read(initiate_read, read_buf)

    def _receive(self, timeout_secs, chunk_delay_secs, wait_for_newline):
        chunk_len = 0
        received_newline = False
        start = start_timeout()
        read_data = bytearray()

        while True:
            read_buf = self._read(chunk_len)

            # The number of bytes still available to read.
            num_bytes_available = read_buf[0]
            # The number of bytes in this chunk.
            num_bytes_this_chunk = read_buf[1]
            if num_bytes_this_chunk > 0:
                read_data += read_buf[2:2 + num_bytes_this_chunk]
                received_newline = read_buf[-1] == ord('\n')

            chunk_len = min(num_bytes_available, self.max)
            # Keep going if there's still byte available to read, even if
            # we've received a newline.
            if chunk_len > 0:
                continue

            # Otherwise, if there's no bytes available to read and we either
            # 1) don't care about waiting for a newline or 2) do care and
            # received the newline, we're done.
            if not wait_for_newline or received_newline:
                break

            # Delay between reading chunks. Note that as long as bytes are
            # available to read (i.e. chunk_len > 0), we don't delay here, nor
            # do we check the timeout below. This is intentional and mimics the
            # behavior of other SDKs (e.g. note-c).
            time.sleep(chunk_delay_secs)

            if timeout_secs != 0 and has_timed_out(start, timeout_secs):
                raise Exception("Timed out while reading data from the Notecard.")

        return read_data

    @i2c_lock
    def _transact(self, req, rsp_expected):
        """Perform a low-level transaction with the Notecard."""
        rsp = None

        try:
            transaction_timeout_secs = 30
            self._transaction_manager.start(transaction_timeout_secs)

            self._transmit(req)

            if rsp_expected:
                rsp = self._receive(30, 0.05, True)
        finally:
            self._transaction_manager.stop()

        return rsp

    @i2c_lock
    def Reset(self):
        """Reset the Notecard."""
        # Send a newline to the Notecard to terminate any partial request that
        # might be sitting in its input buffer.
        self._transmit(b'\n')

        time.sleep(CARD_REQUEST_SEGMENT_DELAY_MS / 1000)

        # Read from the Notecard until there's nothing left, retrying a max of 3
        # times.
        retries = 3
        while retries > 0:
            try:
                self._receive(0, .001, False)
            except:
                retries -= 1
            else:
                break

        if retries == 0:
            raise Exception('Failed to reset Notecard.')

    def _linux_write(self, length, data):
        msgs = [I2C.Message(length + data)]
        self.i2c.transfer(self.addr, msgs)

    def _non_linux_write(self, length, data):
        self.i2c.writeto(self.addr, length + data)

    def _linux_read(self, initiate_read_msg, read_buf):
        msgs = [I2C.Message(initiate_read_msg), I2C.Message(read_buf, read=True)]
        self.i2c.transfer(self.addr, msgs)
        read_buf = msgs[1].data

        return read_buf

    def _micropython_read(self, initiate_read_msg, read_buf):
        self.i2c.writeto(self.addr, initiate_read_msg, False)
        self.i2c.readfrom_into(self.addr, read_buf)

        return read_buf

    def _circuitpython_read(self, initiate_read_msg, read_buf):
        self.i2c.writeto_then_readfrom(self.addr, initiate_read_msg, read_buf)

        return read_buf

    def __init__(self, i2c, address, max_transfer, debug=False):
        """Initialize the Notecard before a reset."""
        super().__init__(debug)
        self._user_agent['req_interface'] = 'i2c'
        self._user_agent['req_port'] = address

        self.i2c = i2c

        def i2c_no_op_try_lock(*args, **kwargs):
            """No-op lock function."""
            return True

        def i2c_no_op_unlock(*args, **kwargs):
            """No-op unlock function."""
            pass

        use_i2c_lock = not use_periphery and sys.implementation.name != 'micropython'
        if use_i2c_lock:
            self.lock = self.i2c.try_lock
            self.unlock = self.i2c.unlock
        else:
            self.lock = i2c_no_op_try_lock
            self.unlock = i2c_no_op_unlock

        if address == 0:
            self.addr = NOTECARD_I2C_ADDRESS
        else:
            self.addr = address
        if max_transfer == 0:
            self.max = 255
        else:
            self.max = max_transfer

        if use_periphery:
            self._platform_write = self._linux_write
            self._platform_read = self._linux_read
        elif sys.implementation.name == 'micropython':
            self._platform_write = self._non_linux_write
            self._platform_read = self._micropython_read
        elif sys.implementation.name == 'circuitpython':
            self._platform_write = self._non_linux_write
            self._platform_read = self._circuitpython_read
        else:
            raise NotImplementedError(f'Unsupported platform: {sys.implementation.name}')

        self.Reset()
