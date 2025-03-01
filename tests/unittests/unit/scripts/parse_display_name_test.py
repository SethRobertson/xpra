#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# This file is part of Xpra.
# Copyright (C) 2020-2022 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import tempfile
import unittest

from xpra.os_util import WIN32, POSIX
from xpra.util import AdHocStruct
from xpra.scripts.parsing import parse_display_name


def _get_test_socket_dir():
    return tempfile.gettempdir()

class TestMain(unittest.TestCase):

    def _test_opts(self):
        socket_dir = _get_test_socket_dir()
        opts = AdHocStruct()
        opts.socket_dirs = [socket_dir]
        opts.socket_dir = socket_dir
        opts.exit_ssh = False
        opts.ssh = "ssh -v "
        opts.remote_xpra = "run-xpra"
        opts.password_file = None
        opts.ssl_server_hostname = ""
        opts.ssl_cert = ""
        opts.ssl_key = ""
        opts.ssl_ca_certs = "default"
        opts.ssl_ca_data = ""
        opts.ssl_ciphers = "DEFAULT"
        opts.ssl_protocol = "TLSv1_2"
        opts.ssl_client_verify_mode = "optional"
        opts.ssl_server_verify_mode = "required"
        opts.ssl_verify_flags = "X509_STRICT"
        opts.ssl_check_hostname = True
        opts.ssl_server_hostname = ""
        opts.ssl_options = "ALL,NO_COMPRESSION"
        return opts

    def _test_parse_display_name(self, s, e=None):
        opts = self._test_opts()
        def err(*args):
            raise Exception(*args)
        r = parse_display_name(err, opts, s)
        if e:
            for k,v in e.items():
                actual = r.get(k)
                assert actual==v, f"expected {v!r} but got {actual!r} from parse_display_name({s})={r}, expected {e}"
        return r

    def test_parse_display_name(self):
        socket_dir = _get_test_socket_dir()
        if WIN32:
            fd = self._test_parse_display_name("named-pipe://FOO")["named-pipe"]
            sd = self._test_parse_display_name("FOO")["named-pipe"]
        else:
            fd = self._test_parse_display_name("socket:///FOO")
            sd = self._test_parse_display_name("/FOO")
        assert sd==fd, f"expected {fd} but got {sd}"
        t = self._test_parse_display_name
        def e(s):
            try:
                t(s)
            except Exception:
                pass
            else:
                raise Exception(f"parse_display_name should fail for {s}")
        if POSIX:
            e("ZZZZZZ")
            t("10", {"display_name" : "10", "local" : True, "type" : "unix-domain"})
            t(socket_dir+"/thesocket", {"display_name" : "socket://"+socket_dir+"/thesocket"})
            t("socket:"+socket_dir+"/thesocket", {"display_name" : "socket:"+socket_dir+"/thesocket"})
        e("tcp://host:NOTANUMBER/")
        e("tcp://host:0/")
        e("tcp://host:65536/")
        t("tcp://username@host/", {"username" : "username", "password" : None})
        for socktype in ("tcp", "ws", "wss", "ssl", "ssh"):
            #e(socktype+"://a/b/c/d")
            t(socktype+"://username:password@host:10000/DISPLAY?key1=value1", {
                "type"      : socktype,
                "display"   : "DISPLAY",
                "key1"      : "value1",
                "username"  : "username",
                "password"  : "password",
                "port"      : 10000,
                "host"      : "host",
                "local"     : False,
                })
        t("tcp://fe80::c1:ac45:7351:ea69:14500", {"host" : "fe80::c1:ac45:7351:ea69", "port" : 14500})
        t("tcp://fe80::c1:ac45:7351:ea69%eth1:14500", {"host" : "fe80::c1:ac45:7351:ea69%eth1", "port" : 14500})
        t("tcp://[fe80::c1:ac45:7351:ea69]:14500", {"host" : "fe80::c1:ac45:7351:ea69", "port" : 14500})
        t("tcp://host/100,key1=value1", {"key1" : "value1"})
        t("tcp://host/key1=value1", {"key1" : "value1"})
        try:
            from xpra.net.vsock import CID_ANY, PORT_ANY    #@UnresolvedImport pylint: disable=import-outside-toplevel
        except ImportError:
            pass
        else:
            t("vsock://any:any/", {"vsock" : (CID_ANY, PORT_ANY)})
            t("vsock://10:2000/", {"vsock" : (10, 2000)})
        t("vnc+ssh://host/0")


def main():
    unittest.main()

if __name__ == '__main__':
    main()
