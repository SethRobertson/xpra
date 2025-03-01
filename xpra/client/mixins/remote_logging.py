# This file is part of Xpra.
# Copyright (C) 2010-2021 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import sys
import logging
import traceback
from time import monotonic
from threading import Lock

from xpra.util import csv, typedict, repr_ellipsized, net_utf8
from xpra.client.mixins.stub_client_mixin import StubClientMixin
from xpra.log import Logger, set_global_logging_handler

log = Logger("client")


class RemoteLogging(StubClientMixin):
    """
    Mixin for remote logging support,
    either sending local logging events to the server,
    or receiving logging events from the server.
    """

    def __init__(self):
        super().__init__()
        self.remote_logging = False
        self.in_remote_logging = False
        self.local_logging = None
        self.logging_lock = Lock()
        self.log_both = False
        self.request_server_log = False
        self.monotonic_start_time = monotonic()

    def init(self, opts):
        self.remote_logging = opts.remote_logging
        self.log_both = (opts.remote_logging or "").lower()=="both"


    def cleanup(self):
        ll = self.local_logging
        log("cleanup() local_logging=%s", ll)
        if ll:
            self.local_logging = None
            set_global_logging_handler(ll)


    def parse_server_capabilities(self, c : typedict) -> bool:
        if self.remote_logging.lower() in ("send", "both", "yes", "true", "on") and (
            #'remote-logging.receive' was only added in v4.1 so check both:
            c.boolget("remote-logging") or c.boolget("remote-logging.receive")):
            #check for debug:
            from xpra.log import is_debug_enabled
            conflict = tuple(v for v in ("network", "crypto", "websocket") if is_debug_enabled(v))
            if conflict:
                log.warn("Warning: cannot enable remote logging")
                log.warn(" because debug logging is enabled for: %s", csv(conflict))
                return True
            log.info("enabled remote logging")
            if not self.log_both:
                log.info(" see server log file for further output")
            self.local_logging = set_global_logging_handler(self.remote_logging_handler)
        elif self.remote_logging.lower()=="receive":
            self.request_server_log = c.boolget("remote-logging.send")
            if not self.request_server_log:
                log.warn("Warning: cannot receive log output from the server")
                log.warn(" the feature is not enabled or not supported by the server")
            else:
                self.after_handshake(self.start_receiving_logging)  #pylint: disable=no-member
        return True

    def start_receiving_logging(self):
        self.add_packet_handler("logging", self._process_logging, False)
        self.send("logging-control", "start")

    #def stop_receiving_logging(self):
    #    self.send("logging-control", "stop")

    def _process_logging(self, packet):
        assert not self.local_logging, "cannot receive logging packets when forwarding logging!"
        level, msg = packet[1:3]
        prefix = "server: "
        if len(packet)>=4:
            dtime = packet[3]
            prefix += "@%02i.%03i " % ((dtime//1000)%60, dtime%1000)
        try:
            if isinstance(msg, (tuple, list)):
                dmsg = " ".join(net_utf8(x) for x in msg)
            else:
                dmsg = net_utf8(msg)
            for l in dmsg.splitlines():
                self.do_log(level, prefix+l)
        except Exception as e:
            log("log message decoding error", exc_info=True)
            log.error("Error: failed to parse logging message:")
            log.error(" %s", repr_ellipsized(msg))
            log.estr(e)

    def do_log(self, level, line):
        with self.logging_lock:
            log.log(level, line)


    def remote_logging_handler(self, logger_log, level, msg, *args, **kwargs):
        #prevent loops (if our send call ends up firing another logging call):
        if self.in_remote_logging:
            return
        self.in_remote_logging = True
        def local_warn(*args):
            self.local_logging(logger_log, logging.WARNING, *args)
        try:
            dtime = int(1000*(monotonic() - self.monotonic_start_time))
            if args:
                data = msg % args
            else:
                data = msg
            if len(data)>=32:
                try:
                    data = self.compressed_wrapper("text", data.encode("utf8"), level=1)
                except Exception:
                    pass
            self.send("logging", level, data, dtime)
            exc_info = kwargs.get("exc_info")
            if exc_info is True:
                exc_info = sys.exc_info()
            if exc_info and exc_info[0]:
                for x in traceback.format_tb(exc_info[2]):
                    self.send("logging", level, x, dtime)
                try:
                    etypeinfo = exc_info[0].__name__
                except AttributeError:
                    etypeinfo = str(exc_info[0])
                self.send("logging", level, "%s: %s" % (etypeinfo, exc_info[1]), dtime)
            if self.log_both:
                self.local_logging(logger_log, level, msg, *args, **kwargs)
        except Exception as e:
            if self.exit_code is not None:
                #errors can happen during exit, don't care
                return
            local_warn("Warning: failed to send logging packet:")
            local_warn(" %s" % e)
            local_warn(" original unformatted message: %s", msg)
            if args:
                local_warn(" %i arguments: %s", len(args), args)
            else:
                local_warn(" (no arguments)")
            try:
                self.local_logging(logger_log, level, msg, *args, **kwargs)
            except Exception:
                pass
            try:
                exc_info = sys.exc_info()
                for x in traceback.format_tb(exc_info[2]):
                    for v in x.splitlines():
                        local_warn(v)
            except Exception:
                pass
        finally:
            self.in_remote_logging = False
