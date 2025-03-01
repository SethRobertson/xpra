#!/usr/bin/env python3
# This file is part of Xpra.
# Copyright (C) 2011 Serviware (Arthur Huillet, <ahuillet@serviware.com>)
# Copyright (C) 2010-2022 Antoine Martin <antoine@xpra.org>
# Copyright (C) 2008 Nathaniel Smith <njs@pobox.com>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import sys
import os.path
import stat
import socket
import time
import logging
from time import monotonic
from subprocess import Popen, PIPE, TimeoutExpired
import signal
import shlex
import traceback

from xpra import __version__ as XPRA_VERSION
from xpra.platform.dotxpra import DotXpra
from xpra.util import (
    csv, envbool, envint, nonl, pver, engs,
    noerr, sorted_nicely, typedict,
    DEFAULT_PORTS,
    )
from xpra.exit_codes import (
    EXIT_STR,
    EXIT_OK, EXIT_FAILURE, EXIT_UNSUPPORTED, EXIT_CONNECTION_FAILED,
    EXIT_NO_DISPLAY,
    EXIT_CONNECTION_LOST, EXIT_REMOTE_ERROR,
    EXIT_INTERNAL_ERROR, EXIT_FILE_TOO_BIG,
    RETRY_EXIT_CODES,
    )
from xpra.os_util import (
    get_util_logger, getuid, getgid, get_username_for_uid,
    bytestostr, use_tty, osexpand,
    OSEnvContext,
    set_proc_title,
    is_systemd_pid1, is_socket,
    WIN32, OSX, POSIX, SIGNAMES, is_Ubuntu,
    )
from xpra.scripts.parsing import (
    info, warn, error,
    parse_display_name, parse_env,
    fixup_defaults, validated_encodings, validate_encryption, do_parse_cmdline, show_sound_codec_help,
    MODE_ALIAS,
    )
from xpra.scripts.config import (
    OPTION_TYPES, TRUE_OPTIONS, FALSE_OPTIONS, OFF_OPTIONS,
    NON_COMMAND_LINE_OPTIONS, CLIENT_ONLY_OPTIONS, CLIENT_OPTIONS,
    START_COMMAND_OPTIONS, BIND_OPTIONS, PROXY_START_OVERRIDABLE_OPTIONS, OPTIONS_ADDED_SINCE_V3, OPTIONS_COMPAT_NAMES,
    InitException, InitInfo, InitExit,
    fixup_options, dict_to_validated_config, get_xpra_defaults_dirs, get_defaults, read_xpra_conf,
    make_defaults_struct, parse_bool, has_sound_support, name_to_field,
    )
from xpra.log import is_debug_enabled, Logger, get_debug_args
assert callable(error), "used by modules importing this function from here"

NO_ROOT_WARNING = envbool("XPRA_NO_ROOT_WARNING", False)
CLIPBOARD_CLASS = os.environ.get("XPRA_CLIPBOARD_CLASS")
WAIT_SERVER_TIMEOUT = envint("WAIT_SERVER_TIMEOUT", 90)
CONNECT_TIMEOUT = envint("XPRA_CONNECT_TIMEOUT", 20)
OPENGL_PROBE_TIMEOUT = envint("XPRA_OPENGL_PROBE_TIMEOUT", 5)
SYSTEMD_RUN = envbool("XPRA_SYSTEMD_RUN", True)
VERIFY_X11_SOCKET_TIMEOUT = envint("XPRA_VERIFY_X11_SOCKET_TIMEOUT", 1)
LIST_REPROBE_TIMEOUT = envint("XPRA_LIST_REPROBE_TIMEOUT", 10)

#pylint: disable=import-outside-toplevel

def nox():
    DISPLAY = os.environ.get("DISPLAY")
    if DISPLAY is not None:
        del os.environ["DISPLAY"]
    # This is an error on Fedora/RH, so make it an error everywhere so it will
    # be noticed:
    import warnings
    warnings.filterwarnings("error", "could not open display")
    return DISPLAY

def werr(*msg):
    for x in msg:
        noerr(sys.stderr.write, "%s\n" % (x,))
    noerr(sys.stderr.flush)

def add_process(*args, **kwargs):
    from xpra.child_reaper import getChildReaper
    return getChildReaper().add_process(*args, **kwargs)


def main(script_file, cmdline):
    ml = envint("XPRA_MEM_USAGE_LOGGER")
    if ml>0:
        from xpra.util import start_mem_watcher
        start_mem_watcher(ml)

    if sys.flags.optimize>0:    # pragma: no cover
        sys.stderr.write("************************************************************\n")
        sys.stderr.write(f"Warning: the python optimize flag is set to {sys.flags.optimize}\n")
        sys.stderr.write(" xpra is very likely to crash\n")
        sys.stderr.write("************************************************************\n")
        time.sleep(5)

    from xpra.platform import clean as platform_clean, command_error, command_info
    if len(cmdline)==1:
        cmdline.append("gui")

    #turn off gdk scaling to make sure we get the actual window geometry:
    os.environ["GDK_SCALE"]="1"
    os.environ["GDK_DPI_SCALE"] = "1"
    if WIN32 and os.environ.get("GDK_WIN32_DISABLE_HIDPI") is None:
        os.environ["GDK_WIN32_DISABLE_HIDPI"] = 1
    #client side decorations break window geometry,
    #disable this "feature" unless explicitly enabled:
    if os.environ.get("GTK_CSD") is None:
        os.environ["GTK_CSD"] = "0"
    if POSIX and not OSX and os.environ.get("XDG_SESSION_TYPE", "x11")=="x11" and not os.environ.get("GDK_BACKEND"):
        os.environ["GDK_BACKEND"] = "x11"

    if envbool("XPRA_NOMD5", False):
        import hashlib
        def nomd5(*_args):
            raise ValueError("md5 support is disabled")
        hashlib.algorithms_available.remove("md5")
        hashlib.md5 = nomd5

    def debug_exc(msg="run_mode error"):
        get_util_logger().debug(msg, exc_info=True)

    try:
        defaults = make_defaults_struct()
        fixup_defaults(defaults)
        options, args = do_parse_cmdline(cmdline, defaults)
        #set_proc_title is here so we can override the cmdline later
        #(don't ask me why this works)
        set_proc_title(" ".join(cmdline))
        if not args:
            raise InitExit(-1, "xpra: need a mode")
        mode = args.pop(0)
        mode = MODE_ALIAS.get(mode, mode)
        def err(*args):
            raise InitException(*args)
        return run_mode(script_file, cmdline, err, options, args, mode, defaults)
    except SystemExit:
        debug_exc()
        raise
    except InitExit as e:
        debug_exc()
        if str(e) and e.args and (e.args[0] or len(e.args)>1):
            command_info(str(e))
        return e.status
    except InitInfo as e:
        debug_exc()
        command_info(str(e))
        return 0
    except InitException as e:
        debug_exc()
        command_error(f"xpra initialization error:\n {e}")
        return 1
    except AssertionError as e:
        debug_exc()
        command_error(f"xpra initialization error:\n {e}")
        traceback.print_tb(sys.exc_info()[2])
        return 1
    except Exception:
        debug_exc()
        command_error("xpra main error:\n%s" % traceback.format_exc())
        return 1
    finally:
        platform_clean()
        def closestd(std):
            if std:
                try:
                    std.close()
                except OSError: # pragma: no cover
                    pass
        closestd(sys.stdout)
        closestd(sys.stderr)


def configure_logging(options, mode):
    if mode in (
        "attach", "listen", "launcher",
        "sessions", "mdns-gui",
        "bug-report", "session-info", "docs", "documentation",
        "recover",
        "splash", "qrcode",
        "opengl-test",
        "desktop-greeter",
        "show-menu", "show-about", "show-session-info"
        "webcam",
        "showconfig",
        ):
        to = sys.stdout
    else:
        to = sys.stderr
    #a bit naughty here, but it's easier to let xpra.log initialize
    #the logging system every time, and just undo things here..
    from xpra.log import (
        setloghandler, enable_color, enable_format,
        LOG_FORMAT, NOPREFIX_FORMAT,
        SIGPIPEStreamHandler,
        )
    setloghandler(SIGPIPEStreamHandler(to))
    if mode in (
        "seamless", "desktop", "monitor", "expand", "shadow",
        "upgrade", "upgrade-seamless", "upgrade-desktop", "upgrade-monitor",
        "recover",
        "attach", "listen", "proxy",
        "_sound_record", "_sound_play",
        "stop", "print", "showconfig",
        "request-start", "request-start-desktop", "request-shadow",
        "_dialog", "_pass",
        "pinentry",
        "example",
        ):
        if "help" in options.speaker_codec or "help" in options.microphone_codec:
            server_mode = mode not in ("attach", "listen")
            codec_help = show_sound_codec_help(server_mode, options.speaker_codec, options.microphone_codec)
            raise InitInfo("\n".join(codec_help))
        fmt = LOG_FORMAT
        if mode in ("stop", "showconfig"):
            fmt = NOPREFIX_FORMAT
        if envbool("XPRA_COLOR_LOG", hasattr(to, "fileno") and os.isatty(to.fileno())):
            enable_color(to, fmt)
        else:
            enable_format(fmt)

    from xpra.log import add_debug_category, add_disabled_category, enable_debug_for, disable_debug_for
    if options.debug:
        categories = options.debug.split(",")
        for cat in categories:
            if not cat:
                continue
            if cat[0]=="-":
                add_disabled_category(cat[1:])
                disable_debug_for(cat[1:])
            else:
                add_debug_category(cat)
                enable_debug_for(cat)

    #always log debug level, we just use it selectively (see above)
    logging.root.setLevel(logging.INFO)


def configure_network(options):
    from xpra.net import compression, packet_encoding
    compression.init_compressors(*(list(options.compressors)+["none"]))
    ecs = compression.get_enabled_compressors()
    if not ecs:
        #force compression level to zero since we have no compressors available:
        options.compression_level = 0
    packet_encoding.init_encoders(*list(options.packet_encoders)+["none"])
    ees = set(packet_encoding.get_enabled_encoders())
    try:
        ees.remove("none")
    except KeyError:
        pass
    #verify that at least one real encoder is available:
    if not ees:
        raise InitException("at least one valid packet encoder must be enabled")

def configure_env(env_str):
    if env_str:
        env = parse_env(env_str)
        if POSIX and getuid()==0:
            #running as root!
            #sanitize: only allow "safe" environment variables
            #as these may have been specified by a non-root user
            env = dict((k,v) for k,v in env.items() if k.startswith("XPRA_"))
        os.environ.update(env)


def systemd_run_command(mode, systemd_run_args=None, user=True):
    cmd = ["systemd-run", "--description" , "xpra-%s" % mode, "--scope"]
    if user:
        cmd.append("--user")
    LOG_SYSTEMD_WRAP = envbool("XPRA_LOG_SYSTEMD_WRAP", True)
    if not LOG_SYSTEMD_WRAP:
        cmd.append("--quiet")
    if systemd_run_args:
        cmd += shlex.split(systemd_run_args)
    return cmd

def systemd_run_wrap(mode, args, systemd_run_args=None, **kwargs):
    cmd = systemd_run_command(mode, systemd_run_args)
    cmd += args
    cmd.append("--systemd-run=no")
    stderr = sys.stderr
    LOG_SYSTEMD_WRAP = envbool("XPRA_LOG_SYSTEMD_WRAP", True)
    if LOG_SYSTEMD_WRAP:
        noerr(stderr.write, f"using systemd-run to wrap {mode!r} server command\n")
    LOG_SYSTEMD_WRAP_COMMAND = envbool("XPRA_LOG_SYSTEMD_WRAP_COMMAND", False)
    if LOG_SYSTEMD_WRAP_COMMAND:
        noerr(stderr.write, "%s\n" % " ".join(["'%s'" % x for x in cmd]))
    try:
        with Popen(cmd, **kwargs) as p:
            return p.wait()
    except KeyboardInterrupt:
        return 128+signal.SIGINT


def isdisplaytype(args, *dtypes) -> bool:
    if not args:
        return False
    d = args[0]
    return any((d.startswith(f"{dtype}/") or d.startswith(f"{dtype}:") for dtype in dtypes))

def check_gtk():
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk
    assert Gtk
    r = Gtk.init_check(None)
    if not r[0]:
        raise InitExit(EXIT_NO_DISPLAY, "failed to initialize Gtk, no display?")
    check_display()

def check_display():
    from xpra.platform.gui import can_access_display
    if not can_access_display():    # pragma: no cover
        raise InitExit(EXIT_NO_DISPLAY, "cannot access display")

def use_systemd_run(s):
    if not SYSTEMD_RUN or not POSIX or OSX:
        return False    # pragma: no cover
    systemd_run = parse_bool("systemd-run", s)
    if systemd_run in (True, False):
        return systemd_run
    #detect if we should use it:
    if is_Ubuntu() and (os.environ.get("SSH_TTY") or os.environ.get("SSH_CLIENT")): # pragma: no cover
        #would fail
        return False
    if not is_systemd_pid1():
        return False    # pragma: no cover
    #test it:
    cmd = ["systemd-run", "--quiet", "--user", "--scope", "--", "true"]
    proc = Popen(cmd, stdout=PIPE, stderr=PIPE, shell=False)
    try:
        proc.communicate(timeout=2)
        r = proc.returncode
    except TimeoutExpired:  # pragma: no cover
        r = None
    if r is None:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.communicate(timeout=1)
        except TimeoutExpired:  # pragma: no cover
            r = None
    return r==0


def run_mode(script_file, cmdline, error_cb, options, args, mode, defaults):
    #configure default logging handler:
    if POSIX and getuid()==0 and options.uid==0 and mode not in ("proxy", "autostart", "showconfig") and not NO_ROOT_WARNING:
        warn("\nWarning: running as root")

    mode = MODE_ALIAS.get(mode, mode)
    display_is_remote = isdisplaytype(args, "ssh", "tcp", "ssl", "vsock")
    if mode=="shadow" and WIN32 and not envbool("XPRA_PAEXEC_WRAP", False):
        #are we started from a non-interactive context?
        from xpra.platform.win32.gui import get_desktop_name
        if get_desktop_name() is None:
            argv = list(cmdline)
            exe = argv[0]
            if argv[0].endswith("Xpra_cmd.exe"):
                #we have to use the "interactive" version:
                argv[0] = exe.split("Xpra_cmd.exe", 1)[0]+"Xpra.exe"
            cmd = ["paexec", "-i" , "1", "-s"] + argv
            try:
                with Popen(cmd) as p:
                    return p.wait()
            except KeyboardInterrupt:
                return 128+signal.SIGINT
    if mode in (
        "seamless", "desktop", "shadow", "expand",
        "upgrade", "upgrade-seamless", "upgrade-desktop",
        ) and not display_is_remote and use_systemd_run(options.systemd_run):
        #make sure we run via the same interpreter,
        #inject it into the command line if we have to:
        argv = list(cmdline)
        if argv[0].find("python")<0:
            argv.insert(0, "python%i.%i" % (sys.version_info.major, sys.version_info.minor))
        return systemd_run_wrap(mode, argv, options.systemd_run_args)
    configure_env(options.env)
    configure_logging(options, mode)
    if mode not in (
        "showconfig", "splash",
        "list", "list-windows", "list-mdns", "mdns-gui",
        "list-sessions", "sessions", "displays",
        "clean-displays", "clean-sockets", "clean",
        "xwait", "wminfo", "wmname",
        "desktop-greeter", "gui", "start-gui",
        "docs", "documentation", "html5",
        "pinentry", "input_pass", "_dialog", "_pass",
        "opengl", "opengl-probe", "opengl-test",
        "autostart",
        "encoding", "video",
        "nvinfo", "webcam",
        "keyboard", "gtk-info", "gui-info", "network-info",
        "compression", "packet-encoding", "path-info",
        "printing-info", "version-info", "toolbox",
        "initenv",
        "auth", "showconfig", "showsetting",
        "applications-menu", "sessions-menu",
        "_proxy",
        ):
        configure_network(options)

    if mode not in ("showconfig", "splash") and POSIX and not OSX and os.environ.get("XDG_RUNTIME_DIR") is None and getuid()>0:
        xrd = "/run/user/%i" % getuid()
        if os.path.exists(xrd):
            warn(f"Warning: using {xrd!r} as XDG_RUNTIME_DIR")
            os.environ["XDG_RUNTIME_DIR"] = xrd
        else:
            warn("Warning: XDG_RUNTIME_DIR is not defined")
            warn(f" and {xrd!r} does not exist")
            if os.path.exists("/tmp") and os.path.isdir("/tmp"):
                xrd = "/tmp"
                warn(f" using {xrd!r}")
                os.environ["XDG_RUNTIME_DIR"] = xrd

    if not mode.startswith("_sound_"):
        #only the sound subcommands should ever actually import GStreamer:
        if "gst" in sys.modules or "gi.repository.Gst" in sys.modules:
            raise Exception("cannot prevent the import of the GStreamer bindings, already loaded")
        sys.modules["gst"] = None
        sys.modules["gi.repository.Gst"]= None
        #sound commands don't want to set the name
        #(they do it later to prevent glib import conflicts)
        #"attach" does it when it received the session name from the server
        if mode not in (
            "attach", "listen",
            "seamless", "desktop", "shadow", "expand",
            "upgrade", "upgrade-seamless", "upgrade-desktop",
            "proxy",
            ):
            from xpra.platform import set_name
            set_name("Xpra", "Xpra %s" % mode.strip("_"))

    if mode in (
        "seamless", "desktop", "shadow", "expand",
        "attach", "listen",
        "upgrade", "upgrade-seamless", "upgrade-desktop",
        "recover",
        "request-start", "request-start-desktop", "request-shadow",
        ):
        options.encodings = validated_encodings(options.encodings)
    try:
        return do_run_mode(script_file, cmdline, error_cb, options, args, mode, defaults)
    except KeyboardInterrupt as e:
        info(f"\ncaught {e!r}, exiting")
        return 128+signal.SIGINT


def do_run_mode(script_file, cmdline, error_cb, options, args, mode, defaults):
    mode = MODE_ALIAS.get(mode, mode)
    display_is_remote = isdisplaytype(args, "ssh", "tcp", "ssl", "vsock")
    if mode in ("seamless", "desktop", "monitor", "expand", "shadow") and display_is_remote:
        #ie: "xpra start ssh://USER@HOST:SSHPORT/DISPLAY --start-child=xterm"
        return run_remote_server(script_file, cmdline, error_cb, options, args, mode, defaults)

    if mode in ("seamless", "desktop", "monitor") and args and parse_bool("attach", options.attach) is True:
        assert not display_is_remote
        #maybe the server is already running
        #and we don't need to bother trying to start it:
        try:
            display = pick_display(error_cb, options, args, cmdline)
        except Exception:
            pass
        else:
            dotxpra = DotXpra(options.socket_dir, options.socket_dirs)
            display_name = display.get("display_name")
            if display_name:
                state = dotxpra.get_display_state(display_name)
                if state==DotXpra.LIVE:
                    noerr(sys.stdout.write, "existing live display found, attaching")
                    return do_run_mode(script_file, cmdline, error_cb, options, args, "attach", defaults)

    if mode in (
        "seamless", "desktop", "monitor", "expand", "shadow",
        "upgrade", "upgrade-seamless", "upgrade-desktop",
        "proxy",
        ):
        return run_server(script_file, cmdline, error_cb, options, args, mode, defaults)
    if mode in (
        "attach", "listen", "detach",
        "screenshot", "version", "info", "id",
        "control", "_monitor", "shell", "print",
        "qrcode",
        "show-menu", "show-about", "show-session-info",
        "connect-test", "request-start", "request-start-desktop", "request-shadow",
        ):
        return run_client(script_file, cmdline, error_cb, options, args, mode)
    elif mode in ("stop", "exit"):
        no_gtk()
        return run_stopexit(mode, error_cb, options, args, cmdline)
    elif mode == "top":
        no_gtk()
        return run_top(error_cb, options, args, cmdline)
    elif mode == "list":
        no_gtk()
        return run_list(error_cb, options, args)
    elif mode == "list-windows":
        no_gtk()
        return run_list_windows(error_cb, options, args)
    elif mode == "list-mdns":
        no_gtk()
        return run_list_mdns(error_cb, args)
    elif mode == "mdns-gui":
        check_gtk()
        return run_mdns_gui(error_cb, options)
    elif mode == "list-sessions":
        no_gtk()
        return run_list_sessions(args, options)
    elif mode == "sessions":
        no_gtk()
        return run_sessions_gui(options)
    elif mode == "displays":
        no_gtk()
        return run_displays(args)
    elif mode == "clean-displays":
        no_gtk()
        return run_clean_displays(args)
    elif mode == "clean-sockets":
        no_gtk()
        return run_clean_sockets(options, args)
    elif mode == "clean":
        no_gtk()
        return run_clean(options, args)
    elif mode=="recover":
        return run_recover(script_file, cmdline, error_cb, options, args, defaults)
    elif mode == "xwait":
        no_gtk()
        return run_xwait(args)
    elif mode == "wminfo":
        no_gtk()
        return run_wminfo(args)
    elif mode == "wmname":
        no_gtk()
        return run_wmname(args)
    elif mode == "desktop-greeter":
        check_gtk()
        return run_desktop_greeter()
    elif mode == "launcher":
        check_gtk()
        from xpra.client.gtk_base.client_launcher import main as launcher_main
        return launcher_main(["xpra"]+args)
    elif mode == "gui":
        check_gtk()
        from xpra.gtk_common import gui
        return gui.main(cmdline)
    elif mode == "start-gui":
        check_gtk()
        from xpra.gtk_common import start_gui
        return start_gui.main(options)
    elif mode == "bug-report":
        check_gtk()
        from xpra.scripts import bug_report
        bug_report.main(["xpra"]+args)
    elif mode == "session-info":
        return run_session_info(error_cb, options, args, cmdline)
    elif mode in ("docs", "documentation"):
        return run_docs()
    elif mode == "html5":
        return run_html5()
    elif mode=="_proxy" or mode.startswith("_proxy_"):
        nox()
        return run_proxy(error_cb, options, script_file, cmdline, args, mode, defaults)
    elif mode in ("_sound_record", "_sound_play", "_sound_query"):
        if not has_sound_support():
            error_cb("no sound support!")
        from xpra.sound.wrapper import run_sound
        return run_sound(mode, error_cb, options, args)
    elif mode=="pinentry":
        check_gtk()
        from xpra.scripts.pinentry_wrapper import run_pinentry
        return run_pinentry(args)
    elif mode=="input_pass":
        check_gtk()
        from xpra.scripts.pinentry_wrapper import input_pass
        password = input_pass((args+["password"])[0])
        return len(password or "")>0
    elif mode=="_dialog":
        check_gtk()
        return run_dialog(args)
    elif mode=="_pass":
        check_gtk()
        return run_pass(args)
    elif mode=="send-file":
        check_gtk()
        return run_send_file(args)
    elif mode=="splash":
        check_gtk()
        return run_splash(args)
    elif mode=="opengl":
        check_gtk()
        return run_glcheck(options)
    elif mode=="opengl-probe":
        check_gtk()
        return run_glprobe(options)
    elif mode=="opengl-test":
        check_gtk()
        return run_glprobe(options, True)
    elif mode=="example":
        check_gtk()
        return run_example(args)
    elif mode=="autostart":
        return run_autostart(script_file, args)
    elif mode=="encoding":
        from xpra.codecs import loader
        return loader.main(args)
    elif mode in ("applications-menu", "sessions-menu"):
        from xpra.server.menu_provider import MenuProvider
        if mode=="applications-menu":
            data = MenuProvider().get_menu_data(remove_icons=True)
        else:
            data = MenuProvider().get_desktop_sessions(remove_icons=True)
        if not data:
            print("no menu data available")
        else:
            from xpra.util import print_nested_dict
            print_nested_dict(data)
    elif mode=="video":
        from xpra.codecs import video_helper
        return video_helper.main()
    elif mode=="nvinfo":
        from xpra.codecs import nv_util
        return nv_util.main()
    elif mode=="webcam":
        check_gtk()
        from xpra.scripts import show_webcam
        return show_webcam.main()
    elif mode=="keyboard":
        from xpra.platform import keyboard
        return keyboard.main()
    elif mode=="gtk-info":
        check_gtk()
        from xpra.scripts import gtk_info
        return gtk_info.main()
    elif mode=="gui-info":
        check_gtk()
        from xpra.platform import gui as platform_gui
        return platform_gui.main()
    elif mode=="network-info":
        from xpra.net import net_util
        return net_util.main()
    elif mode=="compression":
        from xpra.net import compression
        return compression.main()
    elif mode=="packet-encoding":
        from xpra.net import packet_encoding
        return packet_encoding.main()
    elif mode=="path-info":
        from xpra.platform import paths
        return paths.main()
    elif mode=="printing-info":
        from xpra.platform import printing
        return printing.main(args)
    elif mode=="version-info":
        from xpra.scripts import version
        return version.main()
    elif mode=="toolbox":
        check_gtk()
        from xpra.client.gtk_base import toolbox
        return toolbox.main()
    elif mode == "initenv":
        if not POSIX:
            raise InitExit(EXIT_UNSUPPORTED, "initenv is not supported on this OS")
        from xpra.server.server_util import xpra_runner_shell_script, write_runner_shell_scripts
        script = xpra_runner_shell_script(script_file, os.getcwd())
        write_runner_shell_scripts(script, False)
        return 0
    elif mode=="auth":
        return run_auth(options, args)
    elif mode == "showconfig":
        return run_showconfig(options, args)
    elif mode == "showsetting":
        return run_showsetting(args)
    else:
        from xpra.scripts.parsing import get_usage
        if mode!="help":
            print(f"Invalid subcommand {mode!r}")
        print("Usage:")
        if not POSIX or OSX:
            print("(this xpra installation does not support starting local servers)")
        cmd = os.path.basename(script_file)
        for x in get_usage():
            print(f"\t{cmd} {x}")
        print()
        print("see 'man xpra' or 'xpra --help' for more details")
        return 1


def find_session_by_name(opts, session_name):
    from xpra.platform.paths import get_nodock_command
    dotxpra = DotXpra(opts.socket_dir, opts.socket_dirs)
    socket_paths = dotxpra.socket_paths(check_uid=getuid(), matching_state=DotXpra.LIVE)
    if not socket_paths:
        return None
    id_sessions = {}
    for socket_path in socket_paths:
        cmd = get_nodock_command()+["id", f"socket://{socket_path}"]
        proc = Popen(cmd, stdout=PIPE, stderr=PIPE)
        id_sessions[socket_path] = proc
    now = monotonic()
    while any(proc.poll() is None for proc in id_sessions.values()) and monotonic()-now<10:
        time.sleep(0.5)
    session_uuid_to_path = {}
    for socket_path, proc in id_sessions.items():
        if proc.poll()==0:
            out, err = proc.communicate()
            d = {}
            for line in bytestostr(out or err).splitlines():
                try:
                    k,v = line.split("=", 1)
                    d[k] = v
                except ValueError:
                    continue
            name = d.get("session-name")
            uuid = d.get("uuid")
            if name==session_name and uuid:
                session_uuid_to_path[uuid] = socket_path
    if not session_uuid_to_path:
        return None
    if len(session_uuid_to_path)>1:
        raise InitException(f"more than one session found matching {session_name!r}")
    return "socket://%s" % tuple(session_uuid_to_path.values())[0]


def display_desc_to_uri(display_desc):
    dtype = display_desc.get("type")
    if not dtype:
        raise InitException("missing display type")
    uri = f"{dtype}://"
    username = display_desc.get("username")
    if username is not None:
        uri += username
    password = display_desc.get("password")
    if password is not None:
        uri += ":"+password
    if username is not None or password is not None:
        uri += "@"
    if dtype in ("ssh", "tcp", "ssl", "ws", "wss"):
        #TODO: re-add 'proxy_host' arguments here
        host = display_desc.get("host")
        if not host:
            raise InitException("missing host from display parameters")
        uri += host
        port = display_desc.get("port")
        if port and port!=DEFAULT_PORTS.get(dtype):
            uri += f":{port:d}"
    elif dtype=="vsock":
        cid, iport = display_desc["vsock"]
        uri += f"{cid}:{iport}"
    else:
        raise NotImplementedError("%s is not implemented yet" % dtype)
    uri += "/" + display_desc_to_display_path(display_desc)
    return uri

def display_desc_to_display_path(display_desc):
    uri = ""
    display = display_desc.get("display")
    if display:
        uri += display.lstrip(":")
    options_str = display_desc.get("options_str")
    if options_str:
        uri += f"?{options_str}"
    return uri


def pick_vnc_display(error_cb, opts, extra_args):
    if extra_args and len(extra_args)==1:
        try:
            display = extra_args[0].lstrip(":")
            display_no = int(display)
        except (ValueError, TypeError):
            pass
        else:
            return {
                "display"   : f":{display_no}",
                "display_name" : display,
                "host"      : "localhost",
                "port"      : 5900+display_no,
                "local"     : True,
                "type"      : "tcp",
                }

    error_cb("cannot find vnc displays yet")


def pick_display(error_cb, opts, extra_args, cmdline=()):
    if len(extra_args)==1 and extra_args[0].startswith("vnc"):
        #can't identify vnc displays with xpra sockets
        #try the first N standard vnc ports:
        #(we could use threads to port scan more quickly)
        N = 100
        from xpra.net.socket_util import socket_connect
        for i in range(N):
            if not os.path.exists(f"{X11_SOCKET_DIR}/X{i}"):
                #no X11 socket, assume no VNC server
                continue
            port = 5900+i
            sock = socket_connect("localhost", port, timeout=0.1)
            if sock:
                return {
                    "type"          : "vnc",
                    "local"         : True,
                    "host"          : "localhost",
                    "port"          : port,
                    "display"       : f":{i}",
                    "display_name"  : f":{i}",
                    }
        #if not, then fall through and hope that the xpra server supports vnc:
    dotxpra = DotXpra(opts.socket_dir, opts.socket_dirs)
    return do_pick_display(dotxpra, error_cb, opts, extra_args, cmdline)

def do_pick_display(dotxpra, error_cb, opts, extra_args, cmdline=()):
    if not extra_args:
        # Pick a default server
        dir_servers = dotxpra.socket_details(matching_state=DotXpra.LIVE)
        try:
            sockdir, display, sockpath = single_display_match(dir_servers, error_cb)
        except:
            if getuid()==0 and opts.system_proxy_socket:
                display = ":PROXY"
                sockdir = os.path.dirname(opts.system_proxy_socket)
                sockpath = opts.system_proxy_socket
            else:
                raise
        desc = {
            "local"             : True,
            "display"           : display,
            "display_name"      : display,
            }
        if WIN32:   # pragma: no cover
            desc.update({
                "type"              : "named-pipe",
                "named-pipe"        : sockpath,
                })
        else:
            desc.update({
                "type"          : "unix-domain",
                "socket_dir"    : sockdir,
                "socket_path"   : sockpath,
                })
        return desc
    if len(extra_args) == 1:
        return parse_display_name(error_cb, opts, extra_args[0], cmdline, find_session_by_name=find_session_by_name)
    error_cb("too many arguments (%i): %s" % (len(extra_args), extra_args))
    return None

def single_display_match(dir_servers, error_cb, nomatch="cannot find any live servers to connect to"):
    #ie: {"/tmp" : [LIVE, "desktop-10", "/tmp/desktop-10"]}
    #aggregate all the different locations:
    allservers = []
    noproxy = []
    for sockdir, servers in dir_servers.items():
        for state, display, path in servers:
            if state==DotXpra.LIVE:
                allservers.append((sockdir, display, path))
                if not display.startswith(":proxy-"):
                    noproxy.append((sockdir, display, path))
    if not allservers:
        error_cb(nomatch)
    if len(allservers)>1:
        #maybe the same server is available under multiple paths
        displays = set(v[1] for v in allservers)
        if len(displays)==1:
            #they all point to the same display, use the first one:
            allservers = allservers[:1]
    if len(allservers)>1 and noproxy:
        #try to ignore proxy instances:
        displays = set(v[1] for v in noproxy)
        if len(displays)==1:
            #they all point to the same display, use the first one:
            allservers = noproxy[:1]
    if len(allservers) > 1:
        error_cb("there are multiple servers running, please specify")
    assert len(allservers)==1
    sockdir, name, path = allservers[0]
    #ie: ("/tmp", "desktop-10", "/tmp/desktop-10")
    return sockdir, name, path


def connect_or_fail(display_desc, opts):
    from xpra.net.bytestreams import ConnectionClosedException
    try:
        return connect_to(display_desc, opts)
    except ConnectionClosedException as e:
        raise InitExit(EXIT_CONNECTION_FAILED, str(e)) from None
    except InitException:
        raise
    except InitExit:
        raise
    except InitInfo:
        raise
    except Exception as e:
        Logger("network").debug("failed to connect", exc_info=True)
        einfo = str(e) or type(e)
        raise InitException(f"connection failed: {einfo}") from None

def proxy_connect(options):
    #if is_debug_enabled("proxy"):
    #log = logging.getLogger(__name__)
    try:
        import socks
    except ImportError as e:
        raise ValueError(f"cannot connect via a proxy: {e}") from None
    to = typedict(options)
    ptype = to.strget("proxy-type")
    proxy_type = {
        "SOCKS5"    : socks.SOCKS5,
        "SOCKS4"    : socks.SOCKS4,
        }.get(ptype, socks.SOCKS5)
    if not proxy_type:
        raise InitExit(EXIT_UNSUPPORTED, f"unsupported proxy type {ptype!r}")
    host = to.strget("proxy-host")
    port = to.intget("proxy-port", 1080)
    rdns = to.boolget("proxy-rdns", True)
    username = options.get("proxy-username")
    password = options.get("proxy-password")
    timeout = options.get("timeout", 20)
    sock = socks.socksocket()
    sock.set_proxy(proxy_type, host, port, rdns, username, password)
    sock.settimeout(timeout)
    sock.connect((options["host"], options["port"]))
    return sock

def retry_socket_connect(options):
    host = options["host"]
    port = options["port"]
    if "proxy-host" in options:
        return proxy_connect(options)
    from xpra.net.socket_util import socket_connect
    start = monotonic()
    retry = 0
    timeout = options.get("timeout", 20)
    while True:
        sock = socket_connect(host, port, timeout=timeout)
        if sock:
            return sock
        if monotonic()-start>=timeout:
            break
        if retry==0:
            werr(f"failed to connect to {host}:{port}, retrying for {timeout} seconds")
        retry += 1
        time.sleep(1)
    dtype = options["type"]
    raise InitExit(EXIT_CONNECTION_FAILED, f"failed to connect to {dtype} socket {host}:{port}")

def get_host_target_string(display_desc, port_key="port", prefix=""):
    dtype = display_desc["type"]
    username = display_desc.get(prefix+"username")
    host = display_desc[prefix+"host"]
    try:
        port = int(display_desc.get(prefix+port_key))
        if not 0<port<2**16:
            port = 0
    except (ValueError, TypeError):
        port = 0
    display = display_desc.get(prefix+"display", "")
    return host_target_string(dtype, username, host, port, display)

def host_target_string(dtype, username, host, port, display):
    target = f"{dtype}://"
    if username:
        target += f"{username}@"
    target += host
    default_port = DEFAULT_PORTS.get(dtype, 0)
    if port and port!=default_port:
        target += f":{port}"
    if display and display.startswith(":"):
        display = display[1:]
    target += "/%s" % (display or "")
    return target


def connect_to(display_desc, opts=None, debug_cb=None, ssh_fail_cb=None):
    from xpra.net.bytestreams import SOCKET_TIMEOUT, VSOCK_TIMEOUT, SocketConnection
    display_name = display_desc["display_name"]
    dtype = display_desc["type"]
    if dtype in ("ssh", "vnc+ssh"):
        from xpra.net.ssh import ssh_paramiko_connect_to, ssh_exec_connect_to
        if display_desc.get("is_paramiko", False):
            conn = ssh_paramiko_connect_to(display_desc)
        else:
            conn = ssh_exec_connect_to(display_desc, opts, debug_cb, ssh_fail_cb)
        if dtype=="vnc+ssh":
            conn.socktype = "vnc"
            conn.socktype_wrapped = "ssh"
        return conn

    if dtype == "unix-domain":
        if not hasattr(socket, "AF_UNIX"):  # pragma: no cover
            raise InitExit(EXIT_UNSUPPORTED, "unix domain sockets are not available on this operating system")
        def sockpathfail_cb(msg):
            raise InitException(msg)
        sockpath = get_sockpath(display_desc, sockpathfail_cb)
        display_desc["socket_path"] = sockpath
        sock = socket.socket(socket.AF_UNIX)
        sock.settimeout(SOCKET_TIMEOUT)
        try:
            sock.connect(sockpath)
        except Exception as e:
            get_util_logger().debug(f"failed to connect using {sock.connect}({sockpath})", exc_info=True)
            noerr(sock.close)
            raise InitExit(EXIT_CONNECTION_FAILED, f"failed to connect to {sockpath!r}:\n {e}") from None
        sock.settimeout(None)
        conn = SocketConnection(sock, sock.getsockname(), sock.getpeername(), display_name, dtype)
        conn.timeout = SOCKET_TIMEOUT
        target = "socket://"
        username = display_desc.get("username")
        if username:
            target += f"{username}@"
        target += sockpath
        conn.target = target
        return conn

    if dtype == "named-pipe":   # pragma: no cover
        pipe_name = display_desc["named-pipe"]
        if not WIN32:
            raise InitException("named pipes are only supported on MS Windows")
        import errno
        from xpra.platform.win32.dotxpra import PIPE_PATH, PIPE_ROOT
        from xpra.platform.win32.namedpipes.connection import NamedPipeConnection, connect_to_namedpipe
        if pipe_name.startswith(PIPE_ROOT):
            #absolute pipe path already specified
            path = pipe_name
        else:
            path = PIPE_PATH+pipe_name
        try:
            pipe_handle = connect_to_namedpipe(path)
        except Exception as e:
            try:
                if e.args[0]==errno.ENOENT:
                    raise InitException(f"the named pipe {pipe_name!r} does not exist: {e}") from None
            except AttributeError:
                pass
            raise InitException(f"failed to connect to the named pipe {pipe_name!r}:\n {e}") from None
        conn = NamedPipeConnection(pipe_name, pipe_handle, {})
        conn.timeout = SOCKET_TIMEOUT
        conn.target = f"namedpipe://{pipe_name}/"
        return conn

    if dtype == "vsock":
        cid, iport = display_desc["vsock"]
        from xpra.net.vsock import (        #pylint: disable=no-name-in-module
            connect_vsocket,                #@UnresolvedImport
            CID_TYPES, CID_ANY, PORT_ANY,    #@UnresolvedImport
            )
        sock = connect_vsocket(cid=cid, port=iport)
        sock.timeout = VSOCK_TIMEOUT
        sock.settimeout(None)
        conn = SocketConnection(sock, "local", "host", (CID_TYPES.get(cid, cid), iport), dtype)
        conn.target = "vsock://%s:%s" % (
            "any" if cid==CID_ANY else cid,
            "any" if iport==PORT_ANY else iport,
            )
        return conn

    if dtype in ("tcp", "ssl", "ws", "wss", "vnc"):
        host = display_desc["host"]
        port = display_desc["port"]
        sock = retry_socket_connect(display_desc)
        sock.settimeout(None)
        conn = SocketConnection(sock, sock.getsockname(), sock.getpeername(), display_name,
                                dtype, socket_options=display_desc)

        if dtype in ("ssl", "wss"):
            from xpra.net.socket_util import ssl_wrap_socket, ssl_handshake
            #convert option names to function arguments:
            ssl_options = dict((k.replace("-", "_"), v) for k, v in display_desc.get("ssl-options", {}).items())
            sock = ssl_wrap_socket(sock, **ssl_options)
            sock = ssl_handshake(sock)
            assert sock, f"failed to wrap socket {sock}"
            conn._socket = sock
            conn.timeout = SOCKET_TIMEOUT

        #wrap in a websocket:
        if dtype in ("ws", "wss"):
            host = display_desc["host"]
            port = display_desc.get("port", 0)
            #do the websocket upgrade and switch to binary
            try:
                from xpra.net.websockets.common import client_upgrade
            except ImportError as e:    # pragma: no cover
                raise InitExit(EXIT_UNSUPPORTED, f"cannot handle websocket connection: {e}") from None
            else:
                display_path = display_desc_to_display_path(display_desc)
                client_upgrade(conn.read, conn.write, host, port, display_path)
        conn.target = get_host_target_string(display_desc)
        return conn
    raise InitException(f"unsupported display type: {dtype}")



def run_dialog(extra_args):
    from xpra.client.gtk_base.confirm_dialog import show_confirm_dialog
    return show_confirm_dialog(extra_args)

def run_pass(extra_args):
    from xpra.client.gtk_base.pass_dialog import show_pass_dialog
    return show_pass_dialog(extra_args)

def run_send_file(extra_args):
    sockpath = os.environ.get("XPRA_SERVER_SOCKET")
    if not sockpath:
        display = os.environ.get("DISPLAY")
        if display:
            uri = display
        else:
            raise InitException("cannot find xpra server to use")
    else:
        uri = f"socket://{sockpath}"
    if extra_args:
        files = extra_args
    else:
        from xpra.gtk_common.gtk_util import choose_files
        files = choose_files(None, "Select Files to Transfer", multiple=True)
        if not files:
            return
    filelog = Logger("file")
    from xpra.platform.paths import get_xpra_command
    xpra_cmd = get_xpra_command()
    errors = 0
    for f in files:
        filelog(f"run_send_file({extra_args}) sending {f!r}")
        if not os.path.isabs(f):
            f = os.path.abspath(f)
        #xpra control :10 send-file /path/to/the-file-to-send open CLIENT_UUID
        cmd = xpra_cmd + ["control", uri, "send-file", f]
        filelog(f"cmd={cmd}")
        proc = Popen(cmd, stdin=None, stdout=PIPE, stderr=PIPE)
        stdout, stderr = proc.communicate()
        if proc.returncode:
            filelog.error(f"Error: failed to send file {f!r}")
            def logfdoutput(v):
                if v:
                    try:
                        v = v.decode()
                    except UnicodeDecodeError:
                        pass
                    filelog.error(f" {v!r}")
            logfdoutput(stdout)
            logfdoutput(stderr)
            errors += 1
        else:
            filelog.info(f"sent {f!r}")
    if errors:
        return EXIT_FAILURE
    return 0

def get_sockpath(display_desc, error_cb, timeout=CONNECT_TIMEOUT):
    #if the path was specified, use that:
    sockpath = display_desc.get("socket_path")
    if not sockpath:
        #find the socket using the display:
        # if uid, gid or username are missing or not found on the local system,
        # use the uid, gid and username of the current user:
        uid = display_desc.get("uid", getuid())
        gid = display_desc.get("gid", getgid())
        username = display_desc.get("username", get_username_for_uid(uid))
        if not username:
            uid = getuid()
            gid = getgid()
            username = get_username_for_uid(uid)
        dotxpra = DotXpra(
            display_desc.get("socket_dir"),
            display_desc.get("socket_dirs"),
            username,
            uid,
            gid,
            )
        display = display_desc["display"]
        def socket_details(state=DotXpra.LIVE):
            return dotxpra.socket_details(matching_state=state, matching_display=display)
        dir_servers = socket_details()
        if display and not dir_servers:
            state = dotxpra.get_display_state(display)
            if state in (DotXpra.UNKNOWN, DotXpra.DEAD) and timeout>0:
                #found the socket for this specific display in UNKNOWN state,
                #or not found any sockets at all (DEAD),
                #this could be a server starting up,
                #so give it a bit of time:
                if state==DotXpra.UNKNOWN:
                    werr(f"server socket for display {display} is in {DotXpra.UNKNOWN} state")
                else:
                    werr(f"server socket for display {display} not found")
                werr(f" waiting up to {timeout} seconds")
                start = monotonic()
                log = Logger("network")
                while monotonic()-start<timeout:
                    state = dotxpra.get_display_state(display)
                    log(f"get_display_state({display})={state}")
                    if state in (dotxpra.LIVE, dotxpra.INACCESSIBLE):
                        #found a final state
                        break
                    time.sleep(0.1)
                dir_servers = socket_details()
        sockpath = single_display_match(dir_servers, error_cb,
                                        nomatch=f"cannot find live server for display {display}")[-1]
    return sockpath

def run_client(script_file, cmdline, error_cb, opts, extra_args, mode):
    if mode=="attach":
        check_gtk()
    else:
        opts.reconnect = False
    if mode in ("attach", "detach") and len(extra_args)==1 and extra_args[0]=="all":
        #run this command for each display:
        dotxpra = DotXpra(opts.socket_dir, opts.socket_dirs)
        displays = dotxpra.displays(check_uid=getuid(), matching_state=DotXpra.LIVE)
        if not displays:
            sys.stdout.write("No xpra sessions found\n")
            return 1
        #we have to locate the 'all' command line argument,
        #so we can replace it with each display we find,
        #but some other command line arguments can take a value of 'all',
        #so we have to make sure that the one we find does not belong to the argument before
        index = None
        for i, arg in enumerate(cmdline):
            if i==0 or arg!="all":
                continue
            prevarg = cmdline[i-1]
            if prevarg[0]=="-" and (prevarg.find("=")<0 or len(prevarg)==2):
                #ie: [.., "--csc-modules", "all"] or [.., "-d", "all"]
                continue
            index = i
            break
        if not index:
            raise InitException("'all' command line argument could not be located")
        cmd = cmdline[:index]+cmdline[index+1:]
        for display in displays:
            dcmd = cmd + [display] + ["--splash=no"]
            Popen(dcmd, stdin=PIPE, stdout=PIPE, stderr=PIPE, close_fds=not WIN32)
        return 0
    app = get_client_app(script_file, cmdline, error_cb, opts, extra_args, mode)
    r = do_run_client(app)
    if opts.reconnect is not False and r in RETRY_EXIT_CODES:
        warn("%s, reconnecting" % EXIT_STR.get(r, r))
        log = Logger("exec")
        if WIN32:
            #the cx_Freeze wrapper changes the cwd to the directory containing the exe,
            #so we have to re-construct the actual path to the exe:
            if not os.path.isabs(script_file):
                script_file = os.path.join(os.getcwd(), os.path.basename(script_file))
            if not os.path.exists(script_file):
                #perhaps the extension is missing?
                if not os.path.splitext(script_file)[1]:
                    log(f"script file {script_file!r} not found, retrying with %PATHEXT%")
                    for ext in os.environ.get("PATHEXT", ".COM;.EXE").split(os.path.pathsep):
                        tmp = script_file+ext
                        if os.path.exists(tmp):
                            script_file = tmp
            cmdline[0] = script_file
            log(f"Popen(args={cmdline}, executable={script_file}")
            Popen(args=cmdline, executable=script_file)
            #we can't keep re-spawning ourselves without freeing memory,
            #so exit the current process with "no error":
            return EXIT_OK
        log("execv%s", (script_file, cmdline))
        os.execv(script_file, cmdline)
    return r


def connect_to_server(app, display_desc, opts):
    #on win32, we must run the main loop
    #before we can call connect()
    #because connect() may run a subprocess,
    #and Gdk locks up the system if the main loop is not running by then!
    from gi.repository import GLib
    log = Logger("network")
    def do_setup_connection():
        try:
            log("do_setup_connection() display_desc=%s", display_desc)
            conn = connect_or_fail(display_desc, opts)
            log("do_setup_connection() conn=%s", conn)
            #UGLY warning: connect_or_fail() will parse the display string,
            #which may change the username and password..
            app.username = opts.username
            app.password = opts.password
            app.display = opts.display
            app.display_desc = display_desc
            protocol = app.setup_connection(conn)
            protocol.start()
        except InitInfo as e:
            log("do_setup_connection() display_desc=%s", display_desc, exc_info=True)
            werr("failed to connect:", " %s" % e)
            GLib.idle_add(app.quit, EXIT_OK)
        except InitExit as e:
            from xpra.net.socket_util import ssl_retry
            ssllog = Logger("ssl")
            mods = ssl_retry(e, opts.ssl_ca_certs)
            ssllog("do_setup_connection() ssl_retry(%s, %s)=%s", e, opts.ssl_ca_certs, mods)
            if mods:
                display_desc.setdefault("ssl-options", {}).update(mods)
                do_setup_connection()
                return
            ssllog("do_setup_connection() display_desc=%s", display_desc, exc_info=True)
            werr("Warning: failed to connect:", " %s" % e)
            GLib.idle_add(app.quit, e.status)
        except InitException as e:
            log("do_setup_connection() display_desc=%s", display_desc, exc_info=True)
            werr("Warning: failed to connect:", " %s" % e)
            GLib.idle_add(app.quit, EXIT_CONNECTION_FAILED)
        except Exception as e:
            log.error("do_setup_connection() display_desc=%s", display_desc, exc_info=True)
            werr("Error: failed to connect:", " %s" % e)
            GLib.idle_add(app.quit, EXIT_CONNECTION_FAILED)
    def setup_connection():
        log("setup_connection() starting setup-connection thread")
        from xpra.make_thread import start_thread
        start_thread(do_setup_connection, "setup-connection", True)
    GLib.idle_add(setup_connection)


def get_client_app(script_file, cmdline, error_cb, opts, extra_args, mode):
    validate_encryption(opts)
    if mode=="screenshot":
        if not extra_args:
            error_cb("invalid number of arguments for screenshot mode")
        screenshot_filename = extra_args[0]
        extra_args = extra_args[1:]

    request_mode = None
    if mode in ("request-start", "request-start-desktop", "request-shadow"):
        request_mode = mode.replace("request-", "")

    try:
        from xpra import client
        assert client
    except ImportError:
        error_cb("Xpra client is not installed")

    if opts.compression_level < 0 or opts.compression_level > 9:
        error_cb("Compression level must be between 0 and 9 inclusive.")
    if opts.quality!=-1 and (opts.quality < 0 or opts.quality > 100):
        error_cb("Quality must be between 0 and 100 inclusive. (or -1 to disable)")

    socket_dirs = opts.socket_dirs
    if mode in (
        "info", "id", "connect-test", "control", "version", "detach",
        "show-menu", "show-about", "show-session-info",
        ) and extra_args:
        socket_dirs += opts.client_socket_dirs or []
    dotxpra = DotXpra(opts.socket_dir, socket_dirs)
    if mode=="screenshot":
        from xpra.client.gobject_client_base import ScreenshotXpraClient
        app = ScreenshotXpraClient(opts, screenshot_filename)
    elif mode=="info":
        from xpra.client.gobject_client_base import InfoXpraClient
        app = InfoXpraClient(opts)
    elif mode=="id":
        from xpra.client.gobject_client_base import IDXpraClient
        app = IDXpraClient(opts)
    elif mode in ("show-menu", "show-about", "show-session-info"):
        from xpra.client.gobject_client_base import RequestXpraClient
        app = RequestXpraClient(request=mode, opts=opts)
    elif mode=="connect-test":
        from xpra.client.gobject_client_base import ConnectTestXpraClient
        app = ConnectTestXpraClient(opts)
    elif mode=="_monitor":
        from xpra.client.gobject_client_base import MonitorXpraClient
        app = MonitorXpraClient(opts)
    elif mode=="shell":
        from xpra.client.gobject_client_base import ShellXpraClient
        app = ShellXpraClient(opts)
    elif mode=="control":
        from xpra.client.gobject_client_base import ControlXpraClient
        if len(extra_args)<=1:
            error_cb("not enough arguments for 'control' mode, try 'help'")
        args = extra_args[1:]
        extra_args = extra_args[:1]
        app = ControlXpraClient(opts)
        app.set_command_args(args)
    elif mode=="print":
        from xpra.client.gobject_client_base import PrintClient
        if len(extra_args)<=1:
            error_cb("not enough arguments for 'print' mode")
        args = extra_args[1:]
        extra_args = extra_args[:1]
        app = PrintClient(opts)
        app.set_command_args(args)
    elif mode=="qrcode":
        check_gtk()
        from xpra.client.gtk3.qrcode_client import QRCodeClient
        app = QRCodeClient(opts)
    elif mode=="version":
        from xpra.client.gobject_client_base import VersionXpraClient
        app = VersionXpraClient(opts)
    elif mode=="detach":
        from xpra.client.gobject_client_base import DetachXpraClient
        app = DetachXpraClient(opts)
    elif request_mode and opts.attach is not True:
        from xpra.client.gobject_client_base import RequestStartClient
        sns = get_start_new_session_dict(opts, request_mode, extra_args)
        extra_args = [f"socket:{opts.system_proxy_socket}"]
        app = RequestStartClient(opts)
        app.hello_extra = {"connect" : False}
        app.start_new_session = sns
    else:
        app = get_client_gui_app(error_cb, opts, request_mode, extra_args, mode)
    try:
        if mode!="listen":
            app.show_progress(60, "connecting to server")
        if mode!="attach" and not extra_args:
            #try to guess the server intended:
            server_socket = os.environ.get("XPRA_SERVER_SOCKET")
            if server_socket:
                extra_args = [f"socket://{server_socket}"]
        display_desc = do_pick_display(dotxpra, error_cb, opts, extra_args, cmdline)
        if len(extra_args)==1 and opts.password:
            uri = extra_args[0]
            if uri in cmdline and opts.password in uri:
                #hide the password from the URI:
                i = cmdline.index(uri)
                #cmdline[i] = uri.replace(opts.password, "*"*len(opts.password))
                cmdline[i] = uri.replace(opts.password, "********")
                set_proc_title(" ".join(cmdline))
        connect_to_server(app, display_desc, opts)
    except Exception:
        app.cleanup()
        raise
    return app


def get_client_gui_app(error_cb, opts, request_mode, extra_args, mode):
    check_gtk()
    try:
        app = make_client(error_cb, opts)
    except RuntimeError as e:
        #exceptions at this point are still initialization exceptions
        raise InitException(e.args[0]) from None
    app.show_progress(30, "client configuration")
    try:
        app.init(opts)
        if opts.encoding=="auto":
            opts.encoding = ""
        if opts.encoding:
            err = opts.encoding and (opts.encoding not in app.get_encodings())
            einfo = ""
            if err and opts.encoding!="help":
                einfo = f"invalid encoding: {opts.encoding}\n"
            if opts.encoding=="help" or err:
                from xpra.codecs.loader import encodings_help
                encodings = ["auto"] + app.get_encodings()
                raise InitInfo(einfo+"%s xpra client supports the following encodings:\n * %s" %
                               (app.client_toolkit(), "\n * ".join(encodings_help(encodings))))
        def handshake_complete(*_args):
            app.show_progress(100, "connection established")
            log = get_util_logger()
            try:
                p = app._protocol
                conn = p._conn
                if conn:
                    log.info("Attached to %s server at %s", p.TYPE, conn.target)
                    log.info(" (press Control-C to detach)\n")
            except AttributeError:
                return
        if hasattr(app, "after_handshake"):
            app.after_handshake(handshake_complete)
        app.show_progress(40, "loading user interface")
        app.init_ui(opts)
        if request_mode:
            sns = get_start_new_session_dict(opts, request_mode, extra_args)
            extra_args = [f"socket:{opts.system_proxy_socket}"]
            app.hello_extra = {
                "start-new-session" : sns,
                "connect"           : True,
                }
            #we have consumed the start[-child] options
            app.start_child_new_commands = []
            app.start_new_commands = []

        if mode=="listen":
            if extra_args:
                raise InitException("cannot specify extra arguments with 'listen' mode")
            app.show_progress(80, "listening for incoming connections")
            from xpra.platform.info import get_username
            from xpra.net.socket_util import (
                get_network_logger, setup_local_sockets, peek_connection,
                create_sockets, add_listen_socket, accept_connection,
                )
            sockets = create_sockets(opts, error_cb)
            #we don't have a display,
            #so we can't automatically create sockets:
            if "auto" in opts.bind:
                opts.bind.remove("auto")
            local_sockets = setup_local_sockets(opts.bind,
                                                opts.socket_dir, opts.socket_dirs,
                                                None, False,
                                                opts.mmap_group, opts.socket_permissions,
                                                get_username(), getuid, getgid)
            sockets.update(local_sockets)
            listen_cleanup = []
            socket_cleanup = []
            def new_connection(socktype, sock, handle=0):
                from xpra.make_thread import start_thread
                netlog = get_network_logger()
                netlog("new_connection%s", (socktype, sock, handle))
                conn = accept_connection(socktype, sock)
                #start a thread so we can sleep in peek_connection:
                start_thread(handle_new_connection, f"handle new connection: {conn}", daemon=True, args=(conn, ))
                return True
            def handle_new_connection(conn):
                #see if this is a redirection:
                netlog = get_network_logger()
                line1 = peek_connection(conn)[1]
                netlog(f"handle_new_connection({conn}) line1={line1!r}")
                if line1:
                    from xpra.net.common import SOCKET_TYPES
                    uri = bytestostr(line1)
                    for socktype in SOCKET_TYPES:
                        if uri.startswith(f"{socktype}://"):
                            run_socket_cleanups()
                            netlog.info(f"connecting to {uri}")
                            extra_args[:] = [uri, ]
                            display_desc = pick_display(error_cb, opts, [uri, ])
                            connect_to_server(app, display_desc, opts)
                            #app._protocol.start()
                            return
                app.idle_add(do_handle_connection, conn)
            def do_handle_connection(conn):
                protocol = app.setup_connection(conn)
                protocol.start()
                #stop listening for new connections:
                run_socket_cleanups()
            def run_socket_cleanups():
                for cleanup in listen_cleanup:
                    cleanup()
                listen_cleanup[:] = []
                #close the sockets:
                for cleanup in socket_cleanup:
                    cleanup()
                socket_cleanup[:] = []
            for socktype, sock, sinfo, cleanup_socket in sockets:
                socket_cleanup.append(cleanup_socket)
                cleanup = add_listen_socket(socktype, sock, sinfo, new_connection)
                if cleanup:
                    listen_cleanup.append(cleanup)
            #listen mode is special,
            #don't fall through to connect_to_server!
            app.show_progress(90, "ready")
            return app
    except Exception as e:
        app.show_progress(100, f"failure: {e}")
        may_notify = getattr(app, "may_notify", None)
        if callable(may_notify):
            from xpra.util import XPRA_FAILURE_NOTIFICATION_ID
            body = str(e)
            if body.startswith("failed to connect to"):
                lines = body.split("\n")
                summary = "Xpra client %s" % lines[0]
                body = "\n".join(lines[1:])
            else:
                summary = "Xpra client failed to connect"
            may_notify(XPRA_FAILURE_NOTIFICATION_ID, summary, body, icon_name="disconnected")  #pylint: disable=not-callable
        app.cleanup()
        raise
    return app


def make_progress_process(title="Xpra"):
    #start the splash subprocess
    from xpra.platform.paths import get_nodock_command
    cmd = get_nodock_command()+["splash"]
    try:
        progress_process = Popen(cmd, stdin=PIPE)
    except OSError as e:
        werr("Error launching 'splash' subprocess", " %s" % e)
        return None
    #always close stdin when terminating the splash screen process:
    saved_terminate = progress_process.terminate
    def terminate():
        noerr(progress_process.stdin.close)
        progress_process.stdin = None
        saved_terminate()
    progress_process.terminate = terminate
    def progress(pct, text):
        if progress_process.poll():
            return
        stdin = progress_process.stdin
        if stdin:
            stdin.write(("%i:%s\n" % (pct, text)).encode("latin1"))
            stdin.flush()
    add_process(progress_process, "splash", cmd, ignore=True, forget=True)
    progress(0, title)
    progress(10, "initializing")
    return progress_process


def run_opengl_probe():
    from xpra.platform.paths import get_nodock_command
    log = Logger("opengl")
    cmd = get_nodock_command()+["opengl"]
    env = os.environ.copy()
    if is_debug_enabled("opengl"):
        cmd += ["-d", "opengl"]
    else:
        env["NOTTY"] = "1"
    env["XPRA_HIDE_DOCK"] = "1"
    env["XPRA_REDIRECT_OUTPUT"] = "0"
    start = monotonic()
    try:
        proc = Popen(cmd, stdout=PIPE, stderr=PIPE, env=env)
    except Exception as e:
        log.warn("Warning: failed to execute OpenGL probe command")
        log.warn(" %s", e)
        return "failed", {"message" : str(e).replace("\n", " ")}
    try:
        stdout, stderr = proc.communicate(timeout=OPENGL_PROBE_TIMEOUT)
        r = proc.returncode
    except TimeoutExpired:
        log("opengl probe command timed out")
        proc.kill()
        stdout, stderr = proc.communicate()
        r = None
    log("xpra opengl stdout:")
    for line in stdout.decode().splitlines():
        log(" %s", line)
    log("xpra opengl stderr:")
    for line in stderr.decode().splitlines():
        log(" %s", line)
    log("OpenGL probe command returned %s for command=%s", r, cmd)
    end = monotonic()
    log("probe took %ims", 1000*(end-start))
    props = {}
    for line in stdout.decode().splitlines():
        parts = line.split("=", 1)
        if len(parts)==2:
            props[parts[0]] = parts[1]
    log("parsed OpenGL properties=%s", props)
    def probe_message():
        err = props.get("error", "")
        msg = props.get("message", "")
        if err:
            return f"error:{err}"
        if r==1:
            return "crash"
        if r is None:
            return "timeout"
        if r>128:
            return "failed:%s" % SIGNAMES.get(r-128)
        if r!=0:
            return "failed:%s" % SIGNAMES.get(0-r, 0-r)
        if props.get("success", "False").lower() in FALSE_OPTIONS:
            from xpra.scripts.config import is_VirtualBox
            if is_VirtualBox():
                return "error:incomplete OpenGL support in VirtualBox"
            return "error:%s" % (err or msg)
        if props.get("safe", "False").lower() in FALSE_OPTIONS:
            return "warning:%s" % (err or msg)
        return "success"
    #log.warn("Warning: OpenGL probe failed: %s", msg)
    return probe_message(), props

def make_client(error_cb, opts):
    progress_process = None
    if opts.splash is not False:
        progress_process = make_progress_process("Xpra Client v%s" % XPRA_VERSION)

    try:
        from xpra.platform.gui import init as gui_init
        gui_init()

        def b(v):
            return str(v).lower() not in FALSE_OPTIONS
        def bo(v):
            return str(v).lower() not in FALSE_OPTIONS or str(v).lower() in OFF_OPTIONS
        impwarned = []
        def impcheck(*modules):
            for mod in modules:
                try:
                    __import__("xpra.%s" % mod, {}, {}, [])
                except ImportError:
                    if mod not in impwarned:
                        impwarned.append(mod)
                        log = get_util_logger()
                        log("impcheck%s", modules, exc_info=True)
                        log.warn("Warning: missing %s module", mod)
                    return False
            return True
        from xpra.client import mixin_features
        mixin_features.display          = opts.windows
        mixin_features.windows          = opts.windows
        mixin_features.audio            = (bo(opts.speaker) or bo(opts.microphone)) and impcheck("sound")
        mixin_features.webcam           = bo(opts.webcam) and impcheck("codecs")
        mixin_features.clipboard        = b(opts.clipboard) and impcheck("clipboard")
        mixin_features.notifications    = opts.notifications and impcheck("notifications")
        mixin_features.dbus             = opts.dbus_proxy and impcheck("dbus")
        mixin_features.mmap             = b(opts.mmap)
        mixin_features.logging          = b(opts.remote_logging)
        mixin_features.tray             = b(opts.tray)
        mixin_features.network_state    = True
        mixin_features.network_listener = envbool("XPRA_CLIENT_BIND_SOCKETS", True)
        mixin_features.encoding         = opts.windows
        from xpra.client.gtk3.client import XpraClient
        app = XpraClient()
        app.progress_process = progress_process

        if opts.opengl in ("probe", "nowarn"):
            if os.environ.get("XDG_SESSION_TYPE")=="wayland":
                Logger("opengl").debug("wayland session detected, OpenGL disabled")
                opts.opengl = "no"
            else:
                app.show_progress(20, "validating OpenGL configuration")
                probe, glinfo = run_opengl_probe()
                if opts.opengl=="nowarn":
                    #just on or off from here on:
                    safe = glinfo.get("safe", "False").lower() in TRUE_OPTIONS
                    opts.opengl = ["off", "on"][safe]
                else:
                    opts.opengl = f"probe-{probe}"
                r = probe   #ie: "success"
                if info:
                    renderer = glinfo.get("renderer")
                    if renderer:
                        #ie: "AMD Radeon RX 570 Series (polaris10, LLVM 14.0.0, DRM 3.47, 5.19.10-200.fc36.x86_64)"
                        parts = renderer.split("(")
                        if len(parts)>1 and len(parts[0])>10:
                            renderer = parts[0].strip()
                        r += f" ({renderer})"
                app.show_progress(20, f"validating OpenGL: {r}")
                if probe=="error":
                    message = glinfo.get("message")
                    if message:
                        app.show_progress(21, f" {message}")
    except Exception:
        if progress_process:
            try:
                progress_process.terminate()
            except Exception:
                pass
        raise
    return app


def do_run_client(app):
    try:
        return app.run()
    except KeyboardInterrupt:
        return -signal.SIGINT
    finally:
        app.cleanup()


def get_start_new_session_dict(opts, mode, extra_args) -> dict:
    sns = {
           "mode"           : mode,     #ie: "start-desktop"
           }
    if len(extra_args)==1:
        sns["display"] = extra_args[0]
    from xpra.scripts.config import dict_to_config
    defaults = dict_to_config(get_defaults())
    fixup_options(defaults)
    for x in PROXY_START_OVERRIDABLE_OPTIONS:
        fn = x.replace("-", "_")
        v = getattr(opts, fn)
        dv = getattr(defaults, fn, None)
        if v and v!=dv:
            sns[x] = v
    #make sure the server will start in the same path we were called from:
    #(despite being started by a root owned process from a different directory)
    if not opts.chdir:
        sns["chdir"] = os.getcwd()
    return sns

def shellquote(s : str) -> str:
    return '"' + s.replace('"', '\\"') + '"'

def strip_defaults_start_child(start_child, defaults_start_child):
    if start_child and defaults_start_child:
        #ensure we don't pass start / start-child commands
        #which came from defaults (the configuration files)
        #only the ones specified on the command line:
        #(and only remove them once so the command line can re-add the same ones!)
        for x in defaults_start_child:
            if x in start_child:
                start_child.remove(x)
    return start_child


def run_server(script_file, cmdline, error_cb, options, args, mode, defaults):
    mode = MODE_ALIAS.get(mode, mode)
    if mode in (
        "seamless", "desktop", "monitor", "expand",
        "upgrade", "upgrade-seamless", "upgrade-desktop", "upgrade-monitor",
        ) and (OSX or WIN32):
        raise InitException(f"{mode} is not supported on this platform")
    display = None
    display_is_remote = isdisplaytype(args, "ssh", "tcp", "ssl", "ws", "wss", "vsock")
    if mode in (
        "seamless",
        "desktop",
        "monitor",
        "expand",
        ) and parse_bool("attach", options.attach) is True:
        if args and not display_is_remote:
            #maybe the server is already running for the display specified
            #then we don't even need to bother trying to start it:
            try:
                display = pick_display(error_cb, options, args, cmdline)
            except Exception:
                pass
            else:
                dotxpra = DotXpra(options.socket_dir, options.socket_dirs)
                display_name = display.get("display_name")
                if display_name:
                    state = dotxpra.get_display_state(display_name)
                    if state==DotXpra.LIVE:
                        noerr(sys.stdout.write, "existing live display found, attaching")
                        return do_run_mode(script_file, cmdline, error_cb, options, args, "attach", defaults)
        #we can't load gtk on posix if the server is local,
        #(as we would need to unload the initial display to attach to the new one)
        if options.resize_display.lower() in TRUE_OPTIONS and (display_is_remote or OSX or not POSIX):
            check_gtk()
            bypass_no_gtk()
            #we can tell the server what size to resize to:
            from xpra.gtk_common.gtk_util import get_root_size
            root_w, root_h = get_root_size()
            from xpra.client.scaling_parser import parse_scaling
            scaling = parse_scaling(options.desktop_scaling, root_w, root_h)
            #but don't bother if scaling is involved:
            if scaling==(1, 1):
                options.resize_display = f"{root_w}x{root_h}"

    r = start_server_via_proxy(script_file, cmdline, error_cb, options, args, mode)
    if isinstance(r, int):
        return r

    try:
        from xpra import server
        assert server
        from xpra.scripts.server import do_run_server
    except ImportError:
        error_cb("Xpra server is not installed")
    return do_run_server(script_file, cmdline, error_cb, options, args, mode, display, defaults)

def start_server_via_proxy(script_file, cmdline, error_cb, options, args, mode):
    start_via_proxy = parse_bool("start-via-proxy", options.start_via_proxy)
    if start_via_proxy is False:
        return
    if not options.daemon:
        if start_via_proxy is True:
            error_cb("cannot start-via-proxy without daemonizing")
        return
    if POSIX and getuid()==0:
        error_cb("cannot start via proxy for root")
        return
    try:
        from xpra import client  #pylint: disable=import-outside-toplevel
        assert client
    except ImportError:
        if start_via_proxy is True:
            error_cb("cannot start-via-proxy: xpra client is not installed")
        return
    ################################################################################
    err = None
    try:
        #this will use the client "start-new-session" feature,
        #to start a new session and connect to it at the same time:
        if not args:
            from xpra.platform.features import SYSTEM_PROXY_SOCKET
            args = [SYSTEM_PROXY_SOCKET]
        app = get_client_app(script_file, cmdline, error_cb, options, args, "request-%s" % mode)
        r = do_run_client(app)
        #OK or got a signal:
        NO_RETRY = [EXIT_OK] + list(range(128, 128+16))
        #TODO: honour "--attach=yes"
        if app.completed_startup:
            #if we had connected to the session,
            #we can ignore more error codes:
            NO_RETRY += [
                EXIT_CONNECTION_LOST,
                EXIT_REMOTE_ERROR,
                EXIT_INTERNAL_ERROR,
                EXIT_FILE_TOO_BIG,
                ]
        if r in NO_RETRY:
            return r
        if r==EXIT_FAILURE:
            err = "unknown general failure"
        else:
            err = EXIT_STR.get(r, r)
    except Exception as e:
        log = Logger("proxy")
        log("failed to start via proxy", exc_info=True)
        err = str(e)
    if start_via_proxy is True:
        error_cb(f"failed to start-via-proxy: {err}")
        return
    #warn and fall through to regular server start:
    warn(f"Warning: cannot use the system proxy for {mode!r} subcommand,")
    warn(f" {err}")
    warn(" more information may be available in your system log")


def run_remote_server(script_file, cmdline, error_cb, opts, args, mode, defaults):
    """ Uses the regular XpraClient with patched proxy arguments to tell run_proxy to start the server """
    display_name = args[0]
    params = parse_display_name(error_cb, opts, display_name, cmdline)
    hello_extra = {}
    #strip defaults, only keep extra ones:
    for x in START_COMMAND_OPTIONS:     # ["start", "start-child", etc]
        fn = x.replace("-", "_")
        v = strip_defaults_start_child(getattr(opts, fn), getattr(defaults, fn))
        setattr(opts, fn, v)
    if isdisplaytype(args, "ssh"):
        #add special flags to "display_as_args"
        proxy_args = params.get("display_as_args", [])
        if params.get("display") is not None:
            geometry = params.get("geometry")
            display = params["display"]
            try:
                pos = proxy_args.index(display)
            except ValueError:
                pos = -1
            if mode=="shadow" and geometry:
                display += f",{geometry}"
            if pos>=0:
                proxy_args[pos] = display
            else:
                proxy_args.append(display)
        for x in get_start_server_args(opts, compat=True, cmdline=cmdline):
            proxy_args.append(x)
        #we have consumed the start[-child] options
        opts.start_child = []
        opts.start = []
        params["display_as_args"] = proxy_args
        #and use a proxy subcommand to start the server:
        if mode=="seamless":
            #this should also be switched to the generic syntax below in v6:
            proxy_command = "_proxy_start"
        elif mode=="shadow":
            #this should also be switched to the generic syntax below in v6:
            proxy_command = "_proxy_shadow_start"
        else:
            #ie: "_proxy_start_desktop"
            proxy_command = f"_proxy_{mode.replace('-', '_')}"
        params["proxy_command"] = [proxy_command]
    else:
        #tcp, ssl or vsock:
        sns = {
               "mode"           : mode,
               "display"        : params.get("display", ""),
               }
        for x in START_COMMAND_OPTIONS:
            fn = x.replace("-", "_")
            v = getattr(opts, fn)
            if v:
                sns[x] = v
        hello_extra = {"start-new-session" : sns}

    app = None
    try:
        if opts.attach is False:
            from xpra.client.gobject_client_base import WaitForDisconnectXpraClient, RequestStartClient
            if isdisplaytype(args, "ssh"):
                #ssh will start the instance we requested,
                #then we just detach and we're done
                app = WaitForDisconnectXpraClient(opts)
            else:
                app = RequestStartClient(opts)
                app.start_new_session = sns
            app.hello_extra = {"connect" : False}
            opts.reconnect = False
        else:
            app = make_client(error_cb, opts)
            app.show_progress(30, "client configuration")
            app.init(opts)
            app.show_progress(40, "loading user interface")
            app.init_ui(opts)
            app.hello_extra = hello_extra
            def handshake_complete(*_args):
                app.show_progress(100, "connection established")
            app.after_handshake(handshake_complete)
        app.show_progress(60, "starting server")

        while True:
            try:
                conn = connect_or_fail(params, opts)
                app.setup_connection(conn)
                app.show_progress(80, "connecting to server")
                break
            except InitExit as e:
                from xpra.net.socket_util import ssl_retry
                mods = ssl_retry(e, opts.ssl_ca_certs)
                if mods:
                    for k, v in mods.items():
                        setattr(opts, f"ssl_{k}", v)
                    continue
                raise
    except Exception as e:
        if app:
            app.show_progress(100, f"failure: {e}")
        raise
    r = do_run_client(app)
    if opts.reconnect is not False and r in RETRY_EXIT_CODES:
        warn("%s, reconnecting" % EXIT_STR.get(r, r))
        args = list(cmdline)
        #modify the 'mode' in the command line:
        try:
            mode_pos = args.index(mode)
        except ValueError:
            raise InitException(f"mode {mode!r} not found in command line arguments") from None
        args[mode_pos] = "attach"
        if params.get("display") is None:
            #try to find which display was used,
            #so we can re-connect to this specific one:
            display = getattr(app, "_remote_display", None)
            if not display:
                raise InitException("cannot identify the remote display to reconnect to")
            #then re-generate a URI with this display name in it:
            new_params = params.copy()
            new_params["display"] = display
            uri = display_desc_to_uri(new_params)
            #and change it in the command line:
            try:
                uri_pos = args.index(display_name)
            except ValueError:
                raise InitException("URI not found in command line arguments") from None
            args[uri_pos] = uri
        #remove command line options consumed by 'start' that should not be used again:
        attach_args = []
        i = 0
        while i<len(args):
            arg = args[i]
            i += 1
            if arg.startswith("--"):
                pos = arg.find("=")
                if pos>0:
                    option = arg[2:pos]
                else:
                    option = arg[2:]
                if option.startswith("start") or option not in CLIENT_OPTIONS:
                    if pos<0 and i<len(args) and not args[i].startswith("--"):
                        i += 1
                    continue
            attach_args.append(arg)
        os.execv(script_file, attach_args)
    return r


X11_SOCKET_DIR = "/tmp/.X11-unix"

def find_X11_displays(max_display_no=None, match_uid=None, match_gid=None):
    displays = {}
    if os.path.exists(X11_SOCKET_DIR) and os.path.isdir(X11_SOCKET_DIR):
        for x in os.listdir(X11_SOCKET_DIR):
            if not x.startswith("X"):
                warn(f"path {x!r} does not look like an X11 socket")
                continue
            try:
                display_no = int(x[1:])
            except ValueError:
                warn(f"{x} does not parse as a display number")
                continue
            #arbitrary: only shadow automatically displays below 10..
            if max_display_no and display_no>max_display_no:
                #warn("display no %i too high (max %i)" % (v, max_display_no))
                continue
            stat = stat_X11_display(display_no)
            if not stat:
                continue
            uid = stat.get("uid", -1)
            gid = stat.get("gid", -1)
            if match_uid is not None and uid!=match_uid:
                #print("display socket %s does not match uid %i (uid=%i)" % (socket_path, match_uid, sstat.st_uid))
                continue
            if match_gid is not None and gid!=match_gid:
                #print("display socket %s does not match gid %i (gid=%i)" % (socket_path, match_gid, sstat.st_gid))
                continue
            displays[display_no] = (uid, gid, )
    return displays

def stat_X11_display(display_no, timeout=VERIFY_X11_SOCKET_TIMEOUT):
    socket_path = os.path.join(X11_SOCKET_DIR, f"X{display_no}")
    try:
        #check that this is a socket
        sstat = os.stat(socket_path)
        is_socket = stat.S_ISSOCK(sstat.st_mode)
        if not is_socket:
            warn(f"display path {socket_path!r} is not a socket!")
            return {}
        try:
            if timeout>0:
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(VERIFY_X11_SOCKET_TIMEOUT)
                sock.connect(socket_path)
                sock.close()
        except OSError:
            return {}
        else:
            return {
                "uid"   : sstat.st_uid,
                "gid"   : sstat.st_gid,
                }
    except FileNotFoundError:
        pass
    except Exception as e:
        warn(f"failure on {socket_path}: {e}")
    return {}


def guess_X11_display(dotxpra, current_display, uid=getuid(), gid=getgid()):
    displays = [f":{x}" for x in find_X11_displays(max_display_no=10, match_uid=uid, match_gid=gid)]
    if current_display and current_display not in displays:
        displays.append(current_display)
    if len(displays)!=1:
        #try without uid match:
        displays = [f":{x}" for x in find_X11_displays(max_display_no=10, match_gid=gid)]
        if len(displays)!=1:
            #try without gid match:
            displays = [f":{x}" for x in find_X11_displays(max_display_no=10)]
    if not displays:
        raise InitExit(1, "could not detect any live X11 displays")
    if len(displays)>1:
        #since we are here to shadow,
        #assume we want to shadow a real X11 server,
        #so remove xpra's own displays to narrow things down:
        results = dotxpra.sockets()
        xpra_displays = [display for _, display in results]
        displays = list(set(displays)-set(xpra_displays))
        if not displays:
            raise InitExit(1, "could not detect any live plain X11 displays,\n"
                           +" only multiple xpra displays: "+csv(xpra_displays))
    if current_display:
        return current_display
    if len(displays)!=1:
        raise InitExit(1, "too many live X11 displays to choose from: "+csv(sorted_nicely(displays)))
    return displays[0]


no_gtk_bypass = False
def bypass_no_gtk(v=True):
    global no_gtk_bypass
    no_gtk_bypass = v
def no_gtk():
    if no_gtk_bypass:
        return
    if OSX:
        #we can't verify on macos because importing GtkosxApplication
        #will import Gtk, and we need GtkosxApplication early to find the paths
        return
    Gtk = sys.modules.get("gi.repository.Gtk")
    if Gtk is None:
        #all good, not loaded
        return
    raise InitException("the Gtk module is already loaded: %s" % Gtk)


def run_example(args):
    all_examples = (
        "bell", "clicks",
        "colors-gradient", "colors-plain", "colors",
        "cursors",
        "file-chooser",
        "fontrendering",
        "grabs",
        "header-bar",
        "initiate-moveresize",
        "text-entry",
        "transparent-colors",
        "transparent-window",
        "tray",
        "window-focus", "window-geometry-hints",
        "window-opacity", "window-overrideredirect",
        "window-states", "window-title",
        "window-transient",
        )
    if not args or args[0] not in all_examples:
        raise InitInfo(f"usage: xpra example testname\nvalid names: {csv(all_examples)}")
    classname = args[0].replace("-", "_")
    try:
        ic =  __import__(f"xpra.client.gtk_base.example.{classname}", {}, {}, "main")
    except ImportError as e:
        raise InitException(f"failed to import example {classname}: {e}") from None
    return ic.main()

def run_autostart(script_file, args):
    def err(msg):
        print(msg)
        print(f"Usage: {script_file!r} enable|disable|status")
        return 1
    if len(args)!=1:
        return err("invalid number of arguments")
    arg = args[0].lower()
    if arg not in ("enable", "disable", "status"):
        return err(f"invalid argument {arg!r}")
    from xpra.platform.features import AUTOSTART
    if not AUTOSTART:
        print("autostart is not supported on this platform")
        return 1
    from xpra.platform.autostart import set_autostart, get_status
    if arg=="status":
        print(get_status())
    else:
        set_autostart(arg=="enable")
    return 0

def run_qrcode(args):
    from xpra.client.gtk3 import qrcode_client
    return qrcode_client.main(args)

def run_splash(args) -> int:
    from xpra.client.gtk3 import splash_screen
    return splash_screen.main(args)

def run_glprobe(opts, show=False) -> int:
    if show:
        from xpra.platform.gui import init, set_default_icon
        set_default_icon("opengl.png")
        init()
    props = do_run_glcheck(opts, show)
    if not props.get("success", False):
        return 3
    if not props.get("safe", False):
        return 2
    return 0

def do_run_glcheck(opts, show=False) -> dict:
    #suspend all logging:
    saved_level = None
    log = Logger("opengl")
    log(f"do_run_glcheck(.., {show})")
    if not is_debug_enabled("opengl") or not use_tty():
        saved_level = logging.root.getEffectiveLevel()
        logging.root.setLevel(logging.WARN)
    try:
        from xpra.client.gl.window_backend import (
            get_gl_client_window_module,
            test_gl_client_window,
            )
        opengl_str = (opts.opengl or "").lower()
        force_enable = opengl_str.split(":")[0] in TRUE_OPTIONS
        opengl_props, gl_client_window_module = get_gl_client_window_module(force_enable)
        log("do_run_glcheck() opengl_props=%s, gl_client_window_module=%s", opengl_props, gl_client_window_module)
        if gl_client_window_module and (opengl_props.get("safe", False) or force_enable):
            gl_client_window_class = gl_client_window_module.GLClientWindow
            pixel_depth = int(opts.pixel_depth)
            log("do_run_glcheck() gl_client_window_class=%s, pixel_depth=%s", gl_client_window_class, pixel_depth)
            if pixel_depth not in (0, 16, 24, 30) and pixel_depth<32:
                pixel_depth = 0
            draw_result = test_gl_client_window(gl_client_window_class, pixel_depth=pixel_depth, show=show)
            opengl_props.update(draw_result)
            if not draw_result.get("success", False):
                opengl_props["safe"] = False
        log("do_run_glcheck(.., %s)=%s", show, opengl_props)
        return opengl_props
    except Exception as e:
        if is_debug_enabled("opengl"):
            log("do_run_glcheck(..)", exc_info=True)
        if use_tty():
            noerr(sys.stderr.write, "error=%s\n" % nonl(e))
            noerr(sys.stderr.flush)
        return {
            "success"   : False,
            "message"   : str(e).replace("\n", " "),
            }
    finally:
        if saved_level is not None:
            logging.root.setLevel(saved_level)

def run_glcheck(opts):
    try:
        props = do_run_glcheck(opts)
    except Exception as e:
        noerr(sys.stdout.write, "error=%s\n" % str(e).replace("\n", " "))
        return 1
    log = Logger("opengl")
    log("run_glcheck(..) props=%s", props)
    for k in sorted(props.keys()):
        v = props[k]
        #skip not human readable:
        if k not in ("extensions", "glconfig", "GLU.extensions", ):
            vstr = str(v)
            try:
                if k.endswith("dims"):
                    vstr = csv(v)
                else:
                    vstr = pver(v)
            except ValueError:
                pass
            sys.stdout.write("%s=%s\n" % (k, vstr))
    sys.stdout.flush()
    return 0


def pick_shadow_display(dotxpra, args, uid=getuid(), gid=getgid()):
    if len(args)==1 and args[0]:
        if OSX or WIN32:
            return args[0]
        if args[0][0]==":":
            #display_name was provided:
            return args[0]
    if OSX or WIN32:
        #no need for a specific display
        return "Main"
    return guess_X11_display(dotxpra, None, uid, gid)


def start_macos_shadow(cmd, env, cwd):
    #launch the shadow server via launchctl so it will have GUI access:
    LAUNCH_AGENT = "org.xpra.Agent"
    LAUNCH_AGENT_FILE = f"/Library/LaunchAgents/{LAUNCH_AGENT}.plist"
    try:
        os.stat(LAUNCH_AGENT_FILE)
    except Exception as e:
        #ignore access denied error, launchctl runs as root
        import errno
        if e.args[0]!=errno.EACCES:
            warn("Error: shadow may not start,\n"
                 +f" the launch agent file {LAUNCH_AGENT_FILE!r} seems to be missing:{e}.\n")
    argfile = os.path.expanduser("~/.xpra/shadow-args")
    with open(argfile, "w", encoding="utf8") as f:
        f.write('["Xpra", "--no-daemon"')
        for x in cmd[1:]:
            f.write(f', "{x}"')
        f.write(']')
    launch_commands = [
                       ["launchctl", "unload", LAUNCH_AGENT_FILE],
                       ["launchctl", "load", "-S", "Aqua", LAUNCH_AGENT_FILE],
                       ["launchctl", "start", LAUNCH_AGENT],
                       ]
    log = get_util_logger()
    log("start_server_subprocess: launch_commands=%s", launch_commands)
    for x in launch_commands:
        proc = Popen(x, env=env, cwd=cwd)
        proc.wait()
    proc = None

def proxy_start_win32_shadow(script_file, args, opts, dotxpra, display_name):
    log = Logger("server")
    from xpra.platform.paths import get_app_dir
    app_dir = get_app_dir()
    cwd = app_dir
    env = os.environ.copy()
    exe = script_file
    cmd = []
    if envbool("XPRA_PAEXEC", True):
        #use paexec to access the GUI session:
        paexec = os.path.join(app_dir, "paexec.exe")
        if os.path.exists(paexec) and os.path.isfile(paexec):
            from xpra.platform.win32.wtsapi import find_session
            from xpra.platform.info import get_username
            username = get_username()
            info = find_session(username)
            if info:
                cmd = [
                    "paexec.exe",
                    "-i", str(info["SessionID"]), "-s",
                    ]
                exe = paexec
                #don't show a cmd window:
                script_file = script_file.replace("Xpra_cmd.exe", "Xpra.exe")
            else:
                log("session not found for user '%s', not using paexec", username)
    cmd += [script_file, "shadow"] + args
    cmd += get_start_server_args(opts)
    debug_args = os.environ.get("XPRA_SHADOW_DEBUG")
    if debug_args is None:
        debug_args = ",".join(get_debug_args())
    if debug_args:
        cmd.append(f"--debug={debug_args}")
    log(f"proxy shadow start command: {cmd}")
    proc = Popen(cmd, executable=exe, env=env, cwd=cwd)
    start = monotonic()
    elapsed = 0
    while elapsed<WAIT_SERVER_TIMEOUT:
        state = dotxpra.get_display_state(display_name)
        if state==DotXpra.LIVE:
            log("found live server '%s'", display_name)
            #give it a bit of time:
            #FIXME: poll until the server is ready instead
            time.sleep(1)
            return proc, f"named-pipe://{display_name}", display_name
        log(f"get_display_state({display_name})={state} (waiting)")
        if proc.poll() not in (None, 0):
            raise Exception(f"shadow subprocess command returned {proc.returncode}")
        time.sleep(0.10)
        elapsed = monotonic()-start
    proc.terminate()
    raise Exception(f"timeout: failed to identify the new shadow server {display_name!r}")

def start_server_subprocess(script_file, args, mode, opts,
                            username="", uid=getuid(), gid=getgid(), env=os.environ.copy(), cwd=None):
    log = Logger("server", "exec")
    log("start_server_subprocess%s", (script_file, args, mode, opts, uid, gid, env, cwd))
    dotxpra = DotXpra(opts.socket_dir, opts.socket_dirs, username, uid=uid, gid=gid)
    #we must use a subprocess to avoid messing things up - yuk
    mode = MODE_ALIAS.get(mode, mode)
    if mode not in ("seamless", "desktop", "monitor", "expand", "shadow"):
        raise ValueError(f"invalid mode {mode!r}")
    if len(args) not in (0, 1):
        raise InitException(f"{mode}: expected 0 or 1 arguments but got {len(args)}: {args}")
    if mode in ("seamless", "desktop", "monitor"):
        if len(args)==1:
            display_name = args[0]
        else:
            assert len(args)==0
            #let the server get one from Xorg via displayfd:
            display_name = 'S' + str(os.getpid())
    else:
        if mode not in ("expand", "shadow"):
            raise ValueError(f"invalid mode {mode!r}")
        display_name = pick_shadow_display(dotxpra, args, uid, gid)
        #we now know the display name, so add it:
        args = [display_name]
        opts.exit_with_client = True

    #get the list of existing sockets so we can spot the new ones:
    if display_name.startswith("S"):
        matching_display = None
    else:
        if display_name.startswith(":") and display_name.find(",")>0:
            #remove options from display name
            matching_display = display_name.split(",", 1)[0]
        else:
            matching_display = display_name
    if WIN32:
        if mode!="shadow":
            raise ValueError(f"invalid mode {mode!r} for MS Windows")
        assert display_name
        return proxy_start_win32_shadow(script_file, args, opts, dotxpra, display_name)

    existing_sockets = set(dotxpra.socket_paths(check_uid=uid,
                                            matching_state=dotxpra.LIVE,
                                            matching_display=matching_display))
    log(f"start_server_subprocess: existing_sockets={existing_sockets}")

    cmd = [script_file, mode] + args        #ie: ["/usr/bin/xpra", "start-desktop", ":100"]
    cmd += get_start_server_args(opts, uid, gid)      #ie: ["--exit-with-children", "--start-child=xterm"]
    debug_args = os.environ.get("XPRA_SUBPROCESS_DEBUG")
    if debug_args is None:
        debug_args = ",".join(get_debug_args())
    if debug_args:
        cmd.append(f"--debug={debug_args}")
    #when starting via the system proxy server,
    #we may already have a XPRA_PROXY_START_UUID,
    #specified by the proxy-start command:
    new_server_uuid = parse_env(opts.env or []).get("XPRA_PROXY_START_UUID")
    if not new_server_uuid:
        #generate one now:
        from xpra.os_util import get_hex_uuid
        new_server_uuid = get_hex_uuid()
        cmd.append(f"--env=XPRA_PROXY_START_UUID={new_server_uuid}")
    if mode=="shadow" and OSX:
        start_macos_shadow(cmd, env, cwd)
        proc = None
    else:
        #useful for testing failures that cause the whole XDG_RUNTIME_DIR to get nuked
        #(and the log file with it):
        #cmd.append("--log-file=/tmp/proxy.log")
        preexec_fn = None
        pass_fds = ()
        if POSIX:
            preexec_fn = os.setpgrp
            cmd.append("--daemon=yes")
            cmd.append("--systemd-run=no")
            if getuid()==0 and (uid!=0 or gid!=0):
                cmd.append(f"--uid={uid}")
                cmd.append(f"--gid={gid}")
            if not OSX and not matching_display:
                #use "--displayfd" switch to tell us which display was chosen:
                r_pipe, w_pipe = os.pipe()
                log("subprocess displayfd pipes: %s", (r_pipe, w_pipe))
                cmd.append(f"--displayfd={w_pipe}")
                pass_fds = (w_pipe, )
        log("start_server_subprocess: command="+csv(repr(x) for x in cmd))
        proc = Popen(cmd, env=env, cwd=cwd, preexec_fn=preexec_fn, pass_fds=pass_fds)
        log(f"proc={proc}")
        add_process(proc, "server", cmd, ignore=True, forget=True)
        if POSIX and not OSX and not matching_display:
            from xpra.platform.displayfd import read_displayfd, parse_displayfd  #pylint: disable=import-outside-toplevel
            buf = read_displayfd(r_pipe, proc=None) #proc deamonizes!
            noerr(os.close, r_pipe)
            noerr(os.close, w_pipe)
            def displayfd_err(msg):
                log.error("Error: displayfd failed")
                log.error(f" {msg}")
            n = parse_displayfd(buf, displayfd_err)
            if n is not None:
                matching_display = f":{n}"
                log(f"displayfd={matching_display}")
    socket_path, display = identify_new_socket(proc, dotxpra, existing_sockets,
                                               matching_display,
                                               new_server_uuid,
                                               display_name,
                                               uid)
    return proc, socket_path, display

def get_start_server_args(opts, uid=getuid(), gid=getgid(), compat=False, cmdline=()):
    defaults = make_defaults_struct(uid=uid, gid=gid)
    fdefaults = defaults.clone()
    fixup_options(fdefaults)
    args = []
    for x, ftype in OPTION_TYPES.items():
        if x in NON_COMMAND_LINE_OPTIONS or x in CLIENT_ONLY_OPTIONS:
            continue
        if compat and x in OPTIONS_ADDED_SINCE_V3:
            continue
        fn = x.replace("-", "_")
        ov = getattr(opts, fn)
        dv = getattr(defaults, fn)
        fv = getattr(fdefaults, fn)
        incmdline = (
            f"--{x}" in cmdline or f"--no-{x}" in cmdline or
            any(c.startswith(f"--{x}=") for c in cmdline)
            )
        if not incmdline:
            #we may skip this option if the value is the same as the default:
            if ftype==list:
                #compare lists using their csv representation:
                if csv(ov)==csv(dv) or csv(ov)==csv(fv):
                    continue
            if ov in (dv, fv):
                continue    #same as the default
        argname = f"--{x}="
        if compat:
            argname = OPTIONS_COMPAT_NAMES.get(argname, argname)
        #lists are special cased depending on how OptionParse will be parsing them:
        if ftype==list:
            #warn("%s: %s vs %s\n" % (x, ov, dv))
            if x in START_COMMAND_OPTIONS+BIND_OPTIONS+[
                     "pulseaudio-configure-commands",
                     "speaker-codec", "microphone-codec",
                     "key-shortcut", "start-env", "env",
                     "socket-dirs",
                     ]:
                #individual arguments (ie: "--start=xterm" "--start=gedit" ..)
                for e in ov:
                    args.append(f"{argname}{e}")
            else:
                #those can be specified as CSV: (ie: "--encodings=png,jpeg,rgb")
                args.append(f"{argname}"+",".join(str(v) for v in ov))
        elif ftype==bool:
            if compat and x in ("exit-with-children", "mmap-group"):
                #older servers don't take a bool value for those options,
                #it is disabled unless specified:
                if ov:
                    args.append(f"--{x}")
            else:
                args.append(f"{argname}" + ["no", "yes"][int(ov)])
        elif ftype in (int, float, str):
            args.append(f"{argname}{ov}")
        else:
            raise InitException(f"unknown option type {ftype!r} for {x!r}")
    return args


def identify_new_socket(proc, dotxpra, existing_sockets, matching_display, new_server_uuid, display_name, matching_uid=0):
    log = Logger("server", "network")
    log("identify_new_socket%s",
        (proc, dotxpra, existing_sockets, matching_display, new_server_uuid, display_name, matching_uid))
    #wait until the new socket appears:
    start = monotonic()
    UUID_PREFIX = "uuid="
    DISPLAY_PREFIX = "display="
    from xpra.platform.paths import get_nodock_command
    while monotonic()-start<WAIT_SERVER_TIMEOUT and (proc is None or proc.poll() in (None, 0)):
        sockets = set(dotxpra.socket_paths(check_uid=matching_uid,
                                           matching_state=dotxpra.LIVE,
                                           matching_display=matching_display))
        #sort because we prefer a socket in /run/* to one in /home/*:
        new_sockets = tuple(reversed(tuple(sockets-existing_sockets)))
        log(f"identify_new_socket new_sockets={new_sockets}")
        for socket_path in new_sockets:
            #verify that this is the right server:
            try:
                #we must use a subprocess to avoid messing things up - yuk
                cmd = get_nodock_command()+["id", f"socket://{socket_path}"]
                p = Popen(cmd, stdout=PIPE, stderr=PIPE)
                stdout, _ = p.communicate()
                if p.returncode==0:
                    try:
                        out = stdout.decode('utf-8')
                    except Exception:
                        try:
                            out = stdout.decode()
                        except Exception:
                            out = bytestostr(stdout)
                    lines = out.splitlines()
                    log(f"id({socket_path}): "+csv(lines))
                    found = False
                    display = matching_display
                    for line in lines:
                        if line.startswith(UUID_PREFIX):
                            this_uuid = line[len(UUID_PREFIX):]
                            if this_uuid==new_server_uuid:
                                found = True
                        elif line.startswith(DISPLAY_PREFIX):
                            display = line[len(DISPLAY_PREFIX):]
                            if display and display==matching_display:
                                found = True
                    if found:
                        assert display, "display value not found in id output"
                        log(f"identify_new_socket found match: path={socket_path!r}, display={display}")
                        return socket_path, display
            except Exception as e:
                warn(f"error during server process detection: {e}")
        time.sleep(0.10)
    raise InitException("failed to identify the new server display!")

def setup_proxy_ssh_socket(cmdline, auth_sock=os.environ.get("SSH_AUTH_SOCK")):
    sshlog = Logger("ssh")
    sshlog(f"setup_proxy_ssh_socket({cmdline}, {auth_sock!r}")
    #this is the socket path that the ssh client wants us to use:
    #ie: "SSH_AUTH_SOCK=/tmp/ssh-XXXX4KyFhe/agent.726992"
    if not auth_sock or not os.path.exists(auth_sock) or not is_socket(auth_sock):
        sshlog(f"setup_proxy_ssh_socket invalid SSH_AUTH_SOCK={auth_sock!r}")
        return None
    session_dir = os.environ.get("XPRA_SESSION_DIR")
    if not session_dir or not os.path.exists(session_dir):
        sshlog(f"setup_proxy_ssh_socket invalid XPRA_SESSION_DIR={session_dir!r}")
        return
    #locate the ssh agent uuid,
    #which is used to derive the agent path symlink
    #that the server will want to use for this connection,
    #newer clients pass it to the remote proxy command process using an env var:
    agent_uuid = None
    for x in cmdline:
        if x.startswith("--env=SSH_AGENT_UUID="):
            agent_uuid = x[len("--env=SSH_AGENT_UUID="):]
            break
    #prevent illegal paths:
    if not agent_uuid or agent_uuid.find("/")>=0 or agent_uuid.find(".")>=0:
        sshlog(f"setup_proxy_ssh_socket invalid SSH_AGENT_UUID={agent_uuid!r}")
        return None
    from xpra.scripts.server import get_ssh_agent_path
    #ie: "/run/user/$UID/xpra/$DISPLAY/ssh/$UUID
    agent_uuid_sockpath = get_ssh_agent_path(agent_uuid)
    if os.path.exists(agent_uuid_sockpath):
        if os.path.islink(agent_uuid_sockpath) and is_socket(agent_uuid_sockpath):
            sshlog(f"setup_proxy_ssh_socket keeping existing valid socket {agent_uuid_sockpath!r}")
            #keep the existing socket unchanged - somehow it still works?
            return
        sshlog(f"setup_proxy_ssh_socket removing invalid symlink / socket {agent_uuid_sockpath!r}")
        os.unlink(agent_uuid_sockpath)
    sshlog(f"setup_proxy_ssh_socket {agent_uuid_sockpath!r} -> {auth_sock!r}")
    os.symlink(auth_sock, agent_uuid_sockpath)
    return agent_uuid_sockpath

def run_proxy(error_cb, opts, script_file, cmdline, args, mode, defaults):
    no_gtk()
    display = None
    display_name = None
    server_mode = {
        "_proxy"                : "seamless",
        "_proxy_shadow_start"   : "shadow",
        }.get(mode, mode.replace("_proxy_", "").replace("_", "-"))
    server_mode = MODE_ALIAS.get(server_mode, server_mode)
    if mode!="_proxy" and server_mode in ("seamless", "desktop", "monitor", "shadow", "expand"):
        attach = parse_bool("attach", opts.attach)
        state = None
        if attach is not False:
            #maybe this server already exists?
            dotxpra = DotXpra(opts.socket_dir, opts.socket_dirs)
            if not args and server_mode in ("shadow", "expand"):
                try:
                    display_name = pick_shadow_display(dotxpra, args)
                    args = [display_name]
                except Exception:
                    #failed to guess!
                    pass
            elif args:
                display = pick_display(error_cb, opts, args, cmdline)
                display_name = display.get("display")
            if display_name:
                state = dotxpra.get_display_state(display_name)
                if state!=DotXpra.DEAD:
                    sys.stderr.write(f"found existing display {display_name} : {state}\n")
        if state!=DotXpra.LIVE:
            #strip defaults, only keep extra ones:
            for x in ("start", "start-child",
                      "start-after-connect", "start-child-after-connect",
                      "start-on-connect", "start-child-on-connect",
                      "start-on-last-client-exit", "start-child-on-last-client-exit",
                      ):
                fn = x.replace("-", "_")
                v = strip_defaults_start_child(getattr(opts, fn), getattr(defaults, fn))
                setattr(opts, fn, v)
            opts.splash = False
            proc, socket_path, display_name = start_server_subprocess(script_file, args, server_mode, opts)
            if not socket_path:
                #if we return non-zero, we will try the next run-xpra script in the list..
                return 0
            if WIN32:
                uri = f"named-pipe://{display_name}"
            else:
                uri = f"socket://{socket_path}"
            display = parse_display_name(error_cb, opts, uri, cmdline)
            if proc and proc.poll() is None:
                #start a thread just to reap server startup process (yuk)
                #(as the server process will exit as it daemonizes)
                from xpra.make_thread import start_thread
                start_thread(proc.wait, "server-startup-reaper")
    if not display:
        #use display specified on command line:
        display = pick_display(error_cb, opts, args, cmdline)
    if display and server_mode!="shadow":
        display_name = display_name or display.get("display") or display.get("display_name")
        try:
            from xpra.scripts.server import get_session_dir
            with OSEnvContext():
                #env var `XPRA_SESSION_DIR` should not be set in an ssh session,
                #and we use an OSEnvContext to avoid polluting the env with it
                #(we need it to use the server ssh agent path functions)
                os.environ["XPRA_SESSION_DIR"] = get_session_dir("attach", opts.sessions_dir, display_name, getuid())
                #ie: "/run/user/$UID/xpra/$DISPLAY/ssh/$UUID
                setup_proxy_ssh_socket(cmdline)
        except OSError:
            sshlog = Logger("ssh")
            sshlog = Logger("ssh")
            sshlog.error("Error setting up client ssh agent forwarding socket", exc_info=True)
    server_conn = connect_or_fail(display, opts)
    from xpra.scripts.fdproxy import XpraProxy
    from xpra.net.bytestreams import TwoFileConnection
    pipe = TwoFileConnection(sys.stdout, sys.stdin, socktype="stdin/stdout")
    app = XpraProxy("xpra-pipe-proxy", pipe, server_conn)
    app.run()
    return 0

def run_stopexit(mode, error_cb, opts, extra_args, cmdline):
    assert mode in ("stop", "exit")
    no_gtk()

    def show_final_state(display_desc):
        #this is for local sockets only!
        display = display_desc["display"]
        sockdir = display_desc.get("socket_dir", "")
        sockdirs = display_desc.get("socket_dirs", [])
        sockdir = DotXpra(sockdir, sockdirs)
        try:
            sockfile = get_sockpath(display_desc, error_cb, 0)
        except InitException:
            #on win32, we can't find the path when it is gone
            final_state = DotXpra.DEAD
        else:
            #first 5 seconds: just check if the socket still exists:
            #without connecting (avoid warnings and log messages on server)
            for _ in range(25):
                if not os.path.exists(sockfile):
                    break
                time.sleep(0.2)
            #next 5 seconds: actually try to connect
            for _ in range(5):
                final_state = sockdir.get_server_state(sockfile, 1)
                if final_state is DotXpra.DEAD:
                    break
                time.sleep(1)
        if final_state is DotXpra.DEAD:
            print(f"xpra at {display} has exited.")
            return 0
        if final_state is DotXpra.UNKNOWN:
            print(f"How odd... I'm not sure what's going on with xpra at {display}")
            return 1
        if final_state is DotXpra.LIVE:
            print(f"Failed to shutdown xpra at {display}")
            return 1
        raise Exception(f"invalid state: {final_state}")

    def multimode(displays):
        sys.stdout.write(f"Trying to {mode} {len(displays)} displays:\n")
        sys.stdout.write(" %s\n" % csv(displays))
        procs = []
        #["xpra", "stop", ..]
        from xpra.platform.paths import get_nodock_command
        cmd = get_nodock_command()+[mode, f"--socket-dir={opts.socket_dir}"]
        for x in opts.socket_dirs:
            if x:
                cmd.append(f"--socket-dirs={x}")
        #use a subprocess per display:
        for display in displays:
            dcmd = cmd + [display]
            proc = Popen(dcmd)
            procs.append(proc)
        start = monotonic()
        live = procs
        while monotonic()-start<10 and live:
            live = [x for x in procs if x.poll() is None]
        return 0

    if len(extra_args)==1 and extra_args[0]=="all":
        #stop or exit all
        dotxpra = DotXpra(opts.socket_dir, opts.socket_dirs)
        displays = dotxpra.displays(check_uid=getuid(), matching_state=DotXpra.LIVE)
        if not displays:
            sys.stdout.write("No xpra sessions found\n")
            return 1
        if len(displays)==1:
            #fall through, but use the display we found:
            extra_args = displays
        else:
            assert len(displays)>1
            return multimode(displays)
    elif len(extra_args)>1:
        return multimode(extra_args)

    display_desc = pick_display(error_cb, opts, extra_args, cmdline)
    app = None
    e = 1
    try:
        if mode=="stop":
            from xpra.client.gobject_client_base import StopXpraClient
            app = StopXpraClient(opts)
        else:
            assert mode=="exit"
            from xpra.client.gobject_client_base import ExitXpraClient
            app = ExitXpraClient(opts)
        app.display_desc = display_desc
        connect_to_server(app, display_desc, opts)
        e = app.run()
    finally:
        if app:
            app.cleanup()
    if e==0:
        if display_desc["local"] and display_desc.get("display"):
            show_final_state(display_desc)
        else:
            print(f"Sent {mode} command")
    return e


def may_cleanup_socket(state, display, sockpath, clean_states=(DotXpra.DEAD,)):
    sys.stdout.write(f"\t{state} session at {display}")
    if state in clean_states:
        try:
            stat_info = os.stat(sockpath)
            if stat_info.st_uid==getuid():
                os.unlink(sockpath)
                sys.stdout.write(" (cleaned up)")
        except OSError as e:
            sys.stdout.write(f" (delete failed: {e})")
    sys.stdout.write("\n")


def run_top(error_cb, options, args, cmdline):
    from xpra.client.top_client import TopClient, TopSessionClient
    app = None
    if args:
        try:
            display_desc = pick_display(error_cb, options, args, cmdline)
        except Exception:
            pass
        else:
            #show the display we picked automatically:
            app = TopSessionClient(options)
            try:
                connect_to_server(app, display_desc, options)
            except Exception:
                app = None
    if not app:
        #show all sessions:
        app = TopClient(options)
    return app.run()

def run_session_info(error_cb, options, args, cmdline):
    check_gtk()
    display_desc = pick_display(error_cb, options, args, cmdline)
    from xpra.client.gtk_base.session_info import SessionInfoClient
    app = SessionInfoClient(options)
    connect_to_server(app, display_desc, options)
    return app.run()

def run_docs():
    from xpra.platform.paths import get_resources_dir, get_app_dir
    paths = []
    prefixes = {get_resources_dir(), get_app_dir()}
    if POSIX:
        prefixes.add("/usr/share")
        prefixes.add("/usr/local/share")
    for prefix in prefixes:
        for parts in (
            ("doc", "xpra", "index.html"),
            ("xpra", "doc", "index.html"),
            ("doc", "index.html"),
            ("doc", "index.html"),
            ):
            paths.append(os.path.join(prefix, *parts))
    return _browser_open("documentation", *paths)

def run_html5(url_options=None):
    from xpra.platform.paths import get_resources_dir, get_app_dir
    page = "connect.html"
    if url_options:
        from urllib.parse import urlencode
        page += "#"+urlencode(url_options)
    return _browser_open(
        "html5 client",
        os.path.join(get_resources_dir(), "html5", page),
        os.path.join(get_resources_dir(), "www", page),
        os.path.join(get_app_dir(), "www", page),
        )

def _browser_open(what, *path_options):
    for f in path_options:
        af = os.path.abspath(f)
        nohash = af.split("#", 1)[0]
        if os.path.exists(nohash) and os.path.isfile(nohash):
            import webbrowser
            webbrowser.open_new_tab("file://%s" % af)
            return 0
    raise InitExit(EXIT_FAILURE, "%s not found!" % what)


def run_desktop_greeter():
    from xpra.gtk_common.desktop_greeter import main
    main()

def run_sessions_gui(options):
    mdns = options.mdns
    try:
        from xpra.net import mdns
        assert mdns
    except ImportError:
        mdns = False
    if mdns:
        from xpra.net.mdns import get_listener_class
        listener = get_listener_class()
        if listener:
            from xpra.client.gtk_base import mdns_gui
            return mdns_gui.do_main(options)
        else:
            warn("Warning: no mDNS support")
            warn(" only local sessions will be shown")
    from xpra.client.gtk_base import sessions_gui
    return sessions_gui.do_main(options)

def run_mdns_gui(error_cb, options):
    from xpra.net.mdns import get_listener_class
    listener = get_listener_class()
    if not listener:
        error_cb("sorry, 'mdns-gui' is not supported on this platform yet")
    from xpra.client.gtk_base import mdns_gui
    return mdns_gui.do_main(options)

def run_list_mdns(error_cb, extra_args):
    no_gtk()
    if len(extra_args)<=1:
        try:
            MDNS_WAIT = int(extra_args[0])
        except (IndexError, ValueError):
            MDNS_WAIT = 5
    else:
        error_cb("too many arguments for mode")
    from xpra.net.mdns import XPRA_MDNS_TYPE
    try:
        from xpra.net.mdns.avahi_listener import AvahiListener
        listener_class = AvahiListener
    except ImportError:
        try:
            from xpra.net.mdns.zeroconf_listener import ZeroconfListener
            listener_class = ZeroconfListener
        except ImportError:
            error_cb("sorry, 'list-mdns' requires an mdns module")
    from xpra.net.net_util import if_indextoname
    from xpra.dbus.common import loop_init
    from gi.repository import GLib
    loop_init()
    found = {}
    shown = set()
    def show_new_found():
        new_found = [x for x in found.keys() if x not in shown]
        for uq in new_found:
            recs = found[uq]
            for i, rec in enumerate(recs):
                iface, _, _, host, address, port, text = rec
                uuid = text.strget("uuid")
                display = text.strget("display", "")
                mode = text.strget("mode", "")
                username = text.strget("username", "")
                session = text.strget("session")
                dtype = text.strget("type")
                if i==0:
                    print("* user '%s' on '%s'" % (username, host))
                    if session:
                        print(" %s session '%s', uuid=%s" % (dtype, session, uuid))
                    elif uuid:
                        print(" uuid=%s" % uuid)
                iinfo = ""
                if iface:
                    iinfo = ", interface %s" % iface
                print(" + %s endpoint on host %s, port %i%s" % (mode, address, port, iinfo))
                dstr = ""
                if display.startswith(":"):
                    dstr = display[1:]
                uri = "%s://%s@%s:%s/%s" % (mode, username, address, port, dstr)
                print("   \"%s\"" % uri)
            shown.add(uq)
    def mdns_add(interface, _protocol, name, _stype, domain, host, address, port, text):
        text = typedict(text or {})
        iface = interface
        if if_indextoname and iface is not None:
            iface = if_indextoname(interface)
        username = text.strget("username", "")
        uq = text.strget("uuid", len(found)), username, host
        found.setdefault(uq, []).append((iface or "", name, domain, host, address, port, text))
        GLib.timeout_add(1000, show_new_found)
    listener = listener_class(XPRA_MDNS_TYPE, mdns_add=mdns_add)
    print("Looking for xpra services via mdns")
    try:
        GLib.idle_add(listener.start)
        loop = GLib.MainLoop()
        GLib.timeout_add(MDNS_WAIT*1000, loop.quit)
        loop.run()
    finally:
        listener.stop()
    if not found:
        print("no services found")
    else:
        print(f"{len(found)} services found")


def run_clean(opts, args):
    no_gtk()
    try:
        uid = int(opts.uid)
    except (ValueError, TypeError):
        uid = getuid()
    from xpra.scripts.server import get_session_dir
    clean = {}
    if args:
        for display in args:
            session_dir = get_session_dir(None, opts.sessions_dir, display, uid)
            if not os.path.exists(session_dir) or not os.path.isdir(session_dir):
                print(f"session {display} not found")
            else:
                clean[display] = session_dir
        #the user specified the sessions to clean,
        #so we can also kill the display:
        kill_displays = True
    else:
        session_dir = osexpand(opts.sessions_dir)
        if not os.path.exists(session_dir):
            raise ValueError(f"cannot find sessions directory {opts.sessions_dir}")
        #try to find all the session directories:
        for x in os.listdir(session_dir):
            d = os.path.join(session_dir, x)
            if not os.path.isdir(d):
                continue
            try:
                display = int(x)
            except ValueError:
                continue
            else:
                clean["%s" % x] = d
        kill_displays = False
    def kill_pid(pid):
        if pid:
            try:
                if pid and pid>1 and pid!=os.getpid():
                    os.kill(pid, signal.SIGTERM)
            except OSError as e:
                error("Error sending SIGTERM signal to %r %i %s" % (pid_filename, pid, e))
    def load_pid(session_dir, pid_filename):
        pid_file = os.path.join(session_dir, pid_filename)  #ie: "/run/user/1000/xpra/7/dbus.pid"
        if not os.path.exists(pid_file):
            return 0
        try:
            with open(pid_file, "rb") as f:
                return int(f.read().rstrip(b"\n\r"))
        except (ValueError, OSError) as e:
            error(f"failed to read {pid_file!r}: {e}")
            return 0

    #also clean client sockets?
    dotxpra = DotXpra(opts.socket_dir, opts.socket_dirs)
    for display, session_dir in clean.items():
        if not os.path.exists(session_dir):
            print(f"session {display} not found")
            continue
        sockpath = os.path.join(session_dir, "socket")
        state = dotxpra.is_socket_match(sockpath, check_uid=uid)
        if state in (dotxpra.LIVE, dotxpra.INACCESSIBLE):
            #this session is still active
            #do not try to clean it!
            if args:
                print(f"session {display} is {state}")
                print(f" the session directory {session_dir} has not been removed")
            continue
        server_pid = load_pid(session_dir, "server.pid")
        if server_pid and POSIX and not OSX and os.path.exists(f"/proc/{server_pid}"):
            print(f"server process for session {display} is still running with pid {server_pid}")
            print(f" the session directory {session_dir!r} has not been removed")
            continue
        try:
            dno = int(display.lstrip(":"))
        except (ValueError, TypeError):
            dno = 0
        else:
            r = stat_X11_display(dno, timeout=VERIFY_X11_SOCKET_TIMEOUT)
            if r:
                #so the X11 server may still be running
                #x11_sockpath = X11_SOCKET_DIR+"/X%i" % dno
                xvfb_pid = load_pid(session_dir, "xvfb.pid")
                if xvfb_pid and kill_displays:
                    kill_pid(xvfb_pid)
                else:
                    print(f"X11 server :{dno} is still running ")
                    if xvfb_pid:
                        print(" run clean-displays to terminate it")
                    else:
                        print(" cowardly refusing to clean the session")
                    continue
        #print("session_dir: %s : %s" % (session_dir, state))
        for pid_filename in ("dbus.pid", "pulseaudio.pid", ):
            pid = load_pid(session_dir, pid_filename)  #ie: "/run/user/1000/xpra/7/dbus.pid"
            kill_pid(pid)
        try:
            session_files = os.listdir(session_dir)
        except OSError as e:
            error(f"Error listing session files in {session_dir}: {e}")
            continue
        #files we can remove safely:
        KNOWN_SERVER_FILES = [
            "cmdline", "config",
            "dbus.env", "dbus.pid",
            "server.env", "server.pid", "server.log",
            "socket", "xauthority", "Xorg.log", "xvfb.pid",
            "pulseaudio.pid",
            ]
        KNOWN_SERVER_DIRS = [
            "pulse",
            "ssh",
            ]
        ALL_KNOWN = KNOWN_SERVER_FILES + KNOWN_SERVER_DIRS
        unknown_files = [x for x in session_files if x not in ALL_KNOWN]
        if unknown_files:
            error("Error: found some unexpected session files:")
            error(" "+csv(unknown_files))
            error(f" the session directory {session_dir!r} has not been removed")
            continue
        for x in session_files:
            try:
                pathname = os.path.join(session_dir, x)
                if x in KNOWN_SERVER_FILES:
                    os.unlink(pathname)
                else:
                    assert x in KNOWN_SERVER_DIRS
                    import shutil
                    shutil.rmtree(pathname)
            except OSError as e:
                error(f"Error removing {pathname!r}: {e}")
        try:
            os.rmdir(session_dir)
        except OSError:
            error(f"Error session directory {session_dir!r}: {e}")
            continue
        #remove the other sockets:
        socket_paths = dotxpra.socket_paths(check_uid=uid, matching_display=display)
        for filename in socket_paths:
            try:
                os.unlink(filename)
            except OSError as e:
                error(f"Error removing socket {filename!r}: {e}")


def run_clean_sockets(opts, args):
    no_gtk()
    matching_display = None
    if args:
        if len(args)==1:
            matching_display = args[0]
        else:
            raise InitInfo("too many arguments for 'clean' mode")
    dotxpra = DotXpra(opts.socket_dir, opts.socket_dirs+opts.client_socket_dirs)
    results = dotxpra.socket_details(check_uid=getuid(),
                                     matching_state=DotXpra.UNKNOWN,
                                     matching_display=matching_display)
    if matching_display and not results:
        raise InitInfo(f"no UNKNOWN socket for display {matching_display!r}")
    return clean_sockets(dotxpra, results)


def run_recover(script_file, cmdline, error_cb, options, args, defaults):
    if not POSIX or OSX:
        raise InitExit(EXIT_UNSUPPORTED, "the 'xpra recover' subcommand is not supported on this platform")
    assert POSIX and not OSX
    no_gtk()
    display_descr = {}
    ALL = len(args)==1 and args[0].lower()=="all"
    if not ALL and len(args)==1:
        try:
            display_descr = pick_display(error_cb, options, args, cmdline)
            args = []
        except Exception:
            pass
    if display_descr:
        display = display_descr.get("display")
        #args are enough to identify the display,
        #get the display_info so we know the mode:
        descr = get_display_info(display)
    else:
        def recover_many(displays):
            from xpra.platform.paths import get_xpra_command  #pylint: disable=import-outside-toplevel
            for display in displays:
                cmd = get_xpra_command()+["recover", display]
                Popen(cmd)
            return 0
        if len(args)>1:
            return recover_many(args)
        displays = get_displays_info()
        #find the 'DEAD' ones:
        dead_displays = tuple(display for display, descr in displays.items() if descr.get("state")=="DEAD")
        if not dead_displays:
            print("No dead displays found, see 'xpra displays'")
            return EXIT_NO_DISPLAY
        if len(dead_displays)>1:
            if ALL:
                return recover_many(dead_displays)
            print("More than one 'DEAD' display found, see 'xpra displays'")
            print(" you can use 'xpra recover all',")
            print(" or specify a display")
            return EXIT_NO_DISPLAY
        display = dead_displays[0]
        descr = displays[display]
    args = [display]
    #figure out what mode was used:
    mode = descr.get("xpra-server-mode", "seamless")
    for m in ("seamless", "desktop", "proxy", "shadow"):
        if mode.find(m)>=0:
            mode = m
            break
    print("Recovering display '%s' as a %s server" % (display, mode))
    #use the existing display:
    options.use_display = "yes"
    no_gtk()
    return run_server(script_file, cmdline, error_cb, options, args, mode, defaults)

def run_displays(args):
    #dotxpra = DotXpra(opts.socket_dir, opts.socket_dirs+opts.client_socket_dirs)
    displays = get_displays_info(None, args)
    print("Found %i displays:" % len(displays))
    if args:
        print(" matching %s" % csv(args))
    SHOW = {
        "xpra-server-mode"  : "mode",
        "uid"               : "uid",
        "gid"               : "gid",
        }
    for display, descr in sorted_nicely(displays.items()):
        state = descr.pop("state", "LIVE")
        info_str = ""
        if "wmname" in descr:
            info_str += descr.get("wmname")+": "
        info_str += csv("%s=%s" % (v, descr.get(k)) for k,v in SHOW.items() if k in descr)
        print("%4s    %-8s    %s" % (display, state, info_str))

def run_clean_displays(args):
    if not POSIX or OSX:
        raise InitExit(EXIT_UNSUPPORTED, "clean-displays is not supported on this platform")
    displays = get_displays_info()
    dead_displays = tuple(display for display, descr in displays.items() if descr.get("state")=="DEAD")
    if not dead_displays:
        print("No dead displays found")
        if args:
            print(" matching %s" % csv(args))
        return 0
    inodes_display = {}
    for display in sorted_nicely(dead_displays):
        #find the X11 server PID
        inodes = []
        sockpath = os.path.join(X11_SOCKET_DIR, "X%s" % display.lstrip(":"))
        PROC_NET_UNIX = "/proc/net/unix"
        with open(PROC_NET_UNIX, "r", encoding="latin1") as f:
            for line in f:
                parts = line.rstrip("\n\r").split(" ")
                if not parts or len(parts)<8:
                    continue
                if parts[-1]==sockpath or parts[-1]=="@%s" % sockpath:
                    try:
                        inode = int(parts[-2])
                    except ValueError:
                        continue
                    else:
                        inodes.append(inode)
                        inodes_display[inode] = display
    #now find the processes that own these inodes
    display_pids = {}
    if inodes_display:
        for f in os.listdir("/proc"):
            try:
                pid = int(f)
            except ValueError:
                continue
            if pid==1:
                #pid 1 is our friend, don't try to kill it
                continue
            procpath = os.path.join("/proc", f)
            if not os.path.isdir(procpath):
                continue
            fddir = os.path.join(procpath, "fd")
            if not os.path.exists(fddir) or not os.path.isdir(fddir):
                continue
            try:
                fds = os.listdir(fddir)
            except PermissionError:
                continue
            for fd in fds:
                fdpath = os.path.join(fddir, fd)
                if not os.path.islink(fdpath):
                    continue
                try:
                    ref = os.readlink(fdpath)
                except PermissionError:
                    continue
                if not ref:
                    continue
                for inode, display in inodes_display.items():
                    if ref=="socket:[%i]" % inode:
                        cmd = ""
                        try:
                            cmdline = os.path.join(procpath, "cmdline")
                            cmd = open(cmdline, "r", encoding="utf8").read()
                            cmd = shlex.join(cmd.split("\0"))
                        except Exception:
                            pass
                        display_pids[display] = (pid, cmd)
    if not display_pids:
        print("No pids found for dead display%s %s" % (engs(dead_displays), csv(sorted_nicely(dead_displays)),))
        if args:
            print(" matching %s" % csv(args))
        return
    print("Found %i dead display pids:" % len(display_pids))
    if args:
        print(" matching %s" % csv(args))
    for display, (pid, cmd) in sorted_nicely(display_pids.items()):
        print("%4s    %-8s    %s" % (display, pid, cmd))
    print()
    WAIT = 5
    print("These displays will be forcibly terminated in %i seconds" % (WAIT,))
    print("Press Control-C to abort")
    for _ in range(WAIT):
        sys.stdout.write(".")
        sys.stdout.flush()
        time.sleep(1)
    for display, (pid, cmd) in display_pids.items():
        try:
            os.kill(pid, signal.SIGINT)
        except OSError as e:
            print("Unable to send SIGINT to %i: %s" % (pid, e))
    print("")
    print("Done")

def get_displays_info(dotxpra=None, display_names=None):
    displays = get_displays(dotxpra, display_names)
    displays_info = {}
    for display, descr in sorted_nicely(displays.items()):
        #descr already contains the uid, gid
        displays_info[display] = descr
        #add wminfo:
        descr.update(get_display_info(display))
    return displays_info

def get_display_info(display):
    log = Logger("util")
    display_info = {"state" : "LIVE"}
    if OSX or not POSIX:
        return display_info
    wminfo = exec_wminfo(display)
    if wminfo:
        log(f"wminfo({display})={wminfo}")
        display_info.update(wminfo)
        mode = wminfo.get("xpra-server-mode", "")
        #seamless servers and non-xpra servers should have a window manager:
        if mode.find("seamless")>=0 and not wminfo.get("_NET_SUPPORTING_WM_CHECK"):
            display_info["state"] = "DEAD"
        else:
            wmname = wminfo.get("wmname")
            if wmname and wmname.lower().find("xpra")>=0:
                #check if the xpra server process still exists:
                pid = wminfo.get("xpra-server-pid")
                if not pid or (os.path.exists("/proc") and not os.path.exists("/proc/%s" % pid)):
                    display_info["state"] = "DEAD"
    else:
        display_info.update({"state" : "UNKNOWN"})
    return display_info

def get_displays(dotxpra=None, display_names=None):
    if OSX or not POSIX:
        return {"Main" : {}}
    log = get_util_logger()
    #add ":" prefix to display name,
    #and remove xpra sessions
    xpra_sessions = {}
    if dotxpra:
        xpra_sessions = get_xpra_sessions(dotxpra)
    displays = {}
    for k, v in find_X11_displays().items():
        display = f":{k}"
        if display in xpra_sessions:
            continue
        if display_names and display not in display_names:
            continue
        uid, gid = v[:2]
        displays[display] = {"uid" : uid, "gid" : gid}
    log("get_displays%s=%s", (dotxpra, display_names), displays)
    return displays

def run_list_sessions(args, options):
    dotxpra = DotXpra(options.socket_dir, options.socket_dirs)
    if args:
        raise InitInfo("too many arguments for 'list-sessions' mode")
    sessions = get_xpra_sessions(dotxpra)
    print(f"Found {len(sessions)} xpra sessions:")
    for display, attrs in sessions.items():
        print("%4s    %-8s    %-12s    %-16s    %s" % (
            display,
            attrs.get("state"),
            attrs.get("session-type", ""),
            attrs.get("username") or attrs.get("uid") or "",
            attrs.get("session-name", "")))
    return 0

def display_wm_info(args):
    assert POSIX and not OSX, "wminfo is not supported on this platform"
    no_gtk()
    if len(args)==1:
        os.environ["DISPLAY"] = args[0]
    elif not args and os.environ.get("DISPLAY"):
        #just use the current one
        pass
    else:
        raise InitExit(EXIT_NO_DISPLAY, "you must specify a display")
    with OSEnvContext():
        os.environ["GDK_BACKEND"] = "x11"
        from xpra.x11.gtk_x11.gdk_display_source import init_gdk_display_source
        init_gdk_display_source()
        from xpra.x11.gtk_x11.wm_check import get_wm_info
        return get_wm_info()

def run_xwait(args):
    from xpra.x11.bindings.xwait import main as xwait_main
    xwait_main(args)

def run_wminfo(args):
    for k,v in display_wm_info(args).items():
        print(f"{k}={v}")
    return 0

def run_wmname(args):
    name = display_wm_info(args).get("wmname", "")
    if name:
        print(name)
    return 0

def exec_wminfo(display):
    log = Logger("util")
    #get the window manager info by executing the "wminfo" subcommand:
    try:
        from xpra.platform.paths import get_xpra_command  #pylint: disable=import-outside-toplevel
        cmd = get_xpra_command() + ["wminfo"]
        env = os.environ.copy()
        env["DISPLAY"] = display
        proc = Popen(cmd, stdout=PIPE, stderr=PIPE, env=env)
        out = proc.communicate(None, 5)[0]
    except Exception as e:
        log(f"exec_wminfo({display})", exc_info=True)
        log.error(f"Error querying wminfo for display {display!r}: {e}")
        return {}
    #parse wminfo output:
    if proc.returncode!=0 or not out:
        return {}
    wminfo = {}
    for line in out.decode().splitlines():
        parts = line.split("=", 1)
        if len(parts)==2:
            wminfo[parts[0]] = parts[1]
    return wminfo

def get_xpra_sessions(dotxpra, ignore_state=(DotXpra.UNKNOWN,), matching_display=None, query=True):
    results = dotxpra.socket_details(matching_display=matching_display)
    log = get_util_logger()
    log("get_xpra_sessions%s socket_details=%s", (dotxpra, ignore_state, matching_display), results)
    sessions = {}
    for socket_dir, values in results.items():
        for state, display, sockpath in values:
            if state in ignore_state:
                continue
            session = {
                "state"         : state,
                "socket-dir"    : socket_dir,
                "socket-path"   : sockpath,
                }
            try:
                s = os.stat(sockpath)
            except OSError as e:
                log("'%s' path cannot be accessed: %s", sockpath, e)
            else:
                session.update({
                    "uid"   : s.st_uid,
                    "gid"   : s.st_gid,
                    })
                username = get_username_for_uid(s.st_uid)
                if username:
                    session["username"] = username
            if query:
                try:
                    from xpra.platform.paths import get_xpra_command
                    cmd = get_xpra_command()+["id", sockpath]
                    proc = Popen(cmd, stdout=PIPE, stderr=PIPE)
                    out = proc.communicate(None, timeout=1)[0]
                    if proc.returncode==0:
                        for line in out.decode().splitlines():
                            parts = line.split("=", 1)
                            if len(parts)==2:
                                session[parts[0]] = parts[1]
                except (OSError, TimeoutExpired):
                    pass
            sessions[display] = session
    return sessions


def run_list(error_cb, opts, extra_args, clean=True):
    no_gtk()
    if extra_args:
        error_cb("too many arguments for mode")
    dotxpra = DotXpra(opts.socket_dir, opts.socket_dirs+opts.client_socket_dirs)
    results = dotxpra.socket_details()
    if not results:
        sys.stdout.write("No xpra sessions found\n")
        return 0
    sys.stdout.write("Found the following xpra sessions:\n")
    unknown = []
    for socket_dir, values in results.items():
        sys.stdout.write(f"{socket_dir}:\n")
        for state, display, sockpath in values:
            if clean:
                may_cleanup_socket(state, display, sockpath)
            if state is DotXpra.UNKNOWN:
                unknown.append((socket_dir, display, sockpath))
    if clean:
        #now, re-probe the "unknown" ones:
        clean_sockets(dotxpra, unknown)
    return 0

def clean_sockets(dotxpra, sockets, timeout=LIST_REPROBE_TIMEOUT):
    #only clean the ones we own:
    reprobe = []
    for x in sockets:
        try:
            stat_info = os.stat(x[2])
            if stat_info.st_uid==getuid():
                reprobe.append(x)
        except OSError:
            pass
    if not reprobe:
        return 0
    sys.stdout.write("Re-probing unknown sessions in: %s\n" % csv(list(set(x[0] for x in sockets))))
    counter = 0
    while reprobe and counter<timeout:
        time.sleep(1)
        counter += 1
        probe_list = list(reprobe)
        unknown = []
        for v in probe_list:
            socket_dir, display, sockpath = v
            state = dotxpra.get_server_state(sockpath, 1)
            if state is DotXpra.DEAD:
                may_cleanup_socket(state, display, sockpath)
            elif state is DotXpra.UNKNOWN:
                unknown.append(v)
            else:
                sys.stdout.write("\t%s session at %s (%s)\n" % (state, display, socket_dir))
        reprobe = unknown
        if reprobe and timeout==LIST_REPROBE_TIMEOUT:
            #if all the remaining sockets are old,
            #we don't need to poll for very long,
            #as they're very likely to be dead:
            newest = 0
            for x in reprobe:
                sockpath = x[2]
                try:
                    mtime = os.stat(sockpath).st_mtime
                except Exception:
                    pass
                else:
                    newest = max(mtime, newest)
            elapsed = time.time()-newest
            if elapsed>60*5:
                #wait maximum 3 seconds for old sockets
                timeout = min(LIST_REPROBE_TIMEOUT, 3)
    #now cleanup those still unknown:
    clean_states = [DotXpra.DEAD, DotXpra.UNKNOWN]
    for state, display, sockpath in unknown:
        state = dotxpra.get_server_state(sockpath)
        may_cleanup_socket(state, display, sockpath, clean_states=clean_states)
    return 0


def run_list_windows(error_cb, opts, extra_args):
    no_gtk()
    if extra_args:
        error_cb("too many arguments for mode")
    dotxpra = DotXpra(opts.socket_dir, opts.socket_dirs)
    displays = dotxpra.displays()
    if not displays:
        sys.stdout.write("No xpra sessions found\n")
        return 0
    import re
    def sort_human(l):
        convert = lambda text: float(text) if text.isdigit() else text
        alphanum = lambda key: [convert(c) for c in re.split(r'([-+]?[0-9]+\.?[0-9]*)', key)]
        l.sort(key=alphanum)
        return l
    def exec_and_parse(subcommand="id", display=""):
        from xpra.platform.paths import get_nodock_command
        cmd = get_nodock_command()+[subcommand, display]
        d = {}
        try:
            env = os.environ.copy()
            env["XPRA_SKIP_UI"] = "1"
            proc = Popen(cmd, stdout=PIPE, stderr=PIPE, env=env)
            out, err = proc.communicate()
            for line in bytestostr(out or err).splitlines():
                try:
                    k,v = line.split("=", 1)
                    d[k] = v
                except ValueError:
                    continue
        except Exception:
            pass
        return d

    sys.stdout.write("Display   Status    Name           Windows\n")
    for display in sort_human(displays):
        state = dotxpra.get_display_state(display)
        sys.stdout.write("%-10s%-10s" % (display, state))
        sys.stdout.flush()
        name = "?"
        if state==DotXpra.LIVE:
            name = exec_and_parse("id", display).get("session-name", "?")
            if len(name)>=15:
                name = name[:12]+".. "
        sys.stdout.write("%-15s" % (name, ))
        sys.stdout.flush()
        windows = "?"
        if state==DotXpra.LIVE:
            dinfo = exec_and_parse("info", display)
            if dinfo:
                #first, find all the window properties:
                winfo = {}
                for k,v in dinfo.items():
                    #ie: "windows.1.size-constraints.base-size" -> ["windows", "1", "size-constraints.base-size"]
                    parts = k.split(".", 2)
                    if parts[0]=="windows" and len(parts)==3:
                        winfo.setdefault(parts[1], {})[parts[2]] = v
                #print("winfo=%s" % (winfo,))
                #then find a property we can show for each:
                wstrs = []
                for props in winfo.values():
                    for prop in ("command", "class-instance", "title"):
                        wstr = props.get(prop, "?")
                        if wstr and prop=="class-instance":
                            wstr = wstr.split("',")[0][2:]
                        if wstr and wstr!="?":
                            break
                    wstrs.append(wstr)
                windows = csv(wstrs)
        sys.stdout.write(f"{windows}\n")
        sys.stdout.flush()

def run_auth(_options, args):
    if not args:
        raise InitException("missing module argument")
    auth_str = args[0]
    from xpra.server.auth.auth_helper import get_auth_module
    auth, auth_module = get_auth_module(auth_str)[:2]
    #see if the module has a "main" entry point:
    main_fn = getattr(auth_module, "main", None)
    if not main_fn:
        raise InitExit(EXIT_UNSUPPORTED, f"no command line utility for {auth!r} authentication module")
    argv = [auth_module.__file__]+args[1:]
    return main_fn(argv)


def run_showconfig(options, args):
    log = get_util_logger()
    d = dict_to_validated_config({})
    fixup_options(d, skip_encodings=True)
    #this one is normally only probed at build time:
    #(so probe it here again)
    if POSIX:
        try:
            from xpra.platform.pycups_printing import get_printer_definition
            for mimetype in ("pdf", "postscript"):
                pdef = get_printer_definition(mimetype)
                if pdef:
                    #ie: d.pdf_printer = "/usr/share/ppd/cupsfilters/Generic-PDF_Printer-PDF.ppd"
                    setattr(d, f"{mimetype}_printer", pdef)
        except Exception:
            pass
    VIRTUAL = ["mode"]       #no such option! (it's a virtual one for the launch by config files)
    #hide irrelevant options:
    HIDDEN = []
    if not "all" in args:
        #this logic probably belongs somewhere else:
        if OSX or WIN32:
            #these options don't make sense on win32 or osx:
            HIDDEN += ["socket-dirs", "socket-dir",
                       "wm-name", "pulseaudio-command", "pulseaudio", "xvfb", "input-method",
                       "socket-permissions", "fake-xinerama", "dbus-proxy", "xsettings",
                       "exit-with-children", "start-new-commands",
                       "start", "start-child",
                       "start-after-connect", "start-child-after-connect",
                       "start-on-connect", "start-child-on-connect",
                       "start-on-last-client-exit", "start-child-on-last-client-exit",
                       "use-display",
                       ]
        if WIN32:
            #"exit-ssh"?
            HIDDEN += ["lpadmin", "daemon", "mmap-group", "mdns"]
        if not OSX:
            HIDDEN += ["dock-icon", "swap-keys"]
    for opt, otype in sorted(OPTION_TYPES.items()):
        if opt in VIRTUAL:
            continue
        i = log.info
        w = log.warn
        if args:
            if ("all" not in args) and (opt not in args):
                continue
        elif opt in HIDDEN:
            i = log.debug
            w = log.debug
        k = name_to_field(opt)
        dv = getattr(d, k)
        cv = getattr(options, k, dv)
        cmpv = [dv]
        if isinstance(dv, tuple) and isinstance(cv, list):
            #defaults may have a tuple,
            #but command line parsing will create a list:
            cmpv.append(list(dv))
        if isinstance(dv, str) and dv.find("\n")>0:
            #newline is written with a "\" continuation character,
            #so we don't read the newline back when loading the config files
            import re
            cmpv.append(re.sub("\\\\\n *", " ", dv))
        if cv not in cmpv:
            w("%-20s  (used)   = %-32s  %s", opt, vstr(otype, cv), type(cv))
            w("%-20s (default) = %-32s  %s", opt, vstr(otype, dv), type(dv))
        else:
            i("%-20s           = %s", opt, vstr(otype, cv))

def vstr(otype, v) -> str:
    #just used to quote all string values
    if v is None:
        if otype==bool:
            return "auto"
        return ""
    if isinstance(v, str):
        return "'%s'" % nonl(v)
    if isinstance(v, (tuple, list)):
        return csv(vstr(otype, x) for x in v)
    return str(v)

def run_showsetting(args):
    if not args:
        raise InitException("specify a setting to display")

    log = get_util_logger()

    settings = []
    for arg in args:
        otype = OPTION_TYPES.get(arg)
        if not otype:
            log.warn(f"{arg!r} is not a valid setting")
        else:
            settings.append(arg)

    from xpra.platform.info import get_username
    dirs = get_xpra_defaults_dirs(username=get_username(), uid=getuid(), gid=getgid())

    #default config:
    config = get_defaults()
    def show_settings():
        for setting in settings:
            value = config.get(setting)
            log.info("%-20s: %-40s (%s)", setting, vstr(None, value), type(value))

    log.info("* default config:")
    show_settings()
    for d in dirs:
        config.clear()
        config.update(read_xpra_conf(d))
        log.info(f"* {d!r}:")
        show_settings()
    return 0


if __name__ == "__main__":  # pragma: no cover
    code = main("xpra.exe", sys.argv)
    if not code:
        code = 0
    sys.exit(code)
