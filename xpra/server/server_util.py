# This file is part of Xpra.
# Copyright (C) 2017-2021 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

import sys
import json
import shlex
import os.path
from subprocess import Popen, PIPE

from xpra.util import envbool
from xpra.os_util import (
    OSX, POSIX,
    which,
    shellsub,
    get_util_logger,
    osexpand, umask_context,
    )
from xpra.log import Logger
from xpra.platform.dotxpra import norm_makepath
from xpra.platform.paths import get_python_exec_command
from xpra.scripts.config import InitException, FALSE_OPTIONS

UINPUT_UUID_LEN = 12


def source_env(source=()) -> dict:
    log = get_util_logger()
    log("source_env(%s)", source)
    env = {}
    for f in source:
        if not f or f.lower() in FALSE_OPTIONS:
            continue
        try:
            e = env_from_sourcing(f)
        except Exception as e:
            log(f"env_from_sourcing({f})", exc_info=True)
            log.error(f"Error sourcing {f!r}: {e}")
        else:
            log("source_env %s=%s", f, e)
            env.update(e)
    log("source_env(%s)=%s", source, env)
    return env


def decode_dict(out):
    env = {}
    for line in out.splitlines():
        parts = line.split("=", 1)
        if len(parts)==2:
            env[parts[0]] = parts[1]
    return env

def decode_json(out):
    return json.loads(out)


# credit: https://stackoverflow.com/a/47080959/428751
# returns a dictionary of the environment variables resulting from sourcing a file
def env_from_sourcing(file_to_source_path, include_unexported_variables=False):
    log = Logger("exec")
    cmd = shlex.split(file_to_source_path)
    filename = cmd[0]
    if not os.path.isabs(filename):
        filename = which(filename)
        if not filename:
            log.error("Error: cannot find command '%s' to execute", cmd[0])
            log.error(" for sourcing '%s'", file_to_source_path)
            return {}
        if not os.path.isabs(filename):
            filename = os.path.abspath(filename)
    cmd[0] = filename
    #figure out if this is a script to source,
    #or if we're meant to execute it directly
    try:
        with open(filename, "rb") as f:
            first_line = f.readline()
    except OSError as e:
        log.error(f"Error: failed to read from {filename!r}")
        log.estr(e)
        first_line = b""
    else:
        log(f"first line of {filename!r}: {first_line!r}")
    if first_line.startswith(b"\x7fELF") or b"\x00" in first_line:
        decode = decode_dict
    else:
        source = '%s. %s' % ("set -a && " if include_unexported_variables else "", filename)
        #ie: this is "python3.9 -c" on Posix
        #(but our 'Python_exec_cmd.exe' wrapper on MS Windows):
        python_cmd = " ".join(get_python_exec_command())
        dump = '%s "import os, json;print(json.dumps(dict(os.environ)))"' % (python_cmd)
        sh = which("bash") or "/bin/sh"
        cmd = [sh, '-c', '%s 1>&2 && %s' % (source, dump)]
        decode = decode_json
    out = err = ""
    try:
        log("env_from_sourcing%s cmd=%s", (filename, include_unexported_variables), cmd)
        proc = Popen(cmd, stdout=PIPE, stderr=PIPE)
        out, err = proc.communicate()
        if proc.returncode!=0:
            log.error(f"Error {proc.returncode} running source script {filename!r}")
    except OSError as e:
        log("env_from_sourcing%s", (filename, include_unexported_variables), exc_info=True)
        log(f" stdout={out} ({type(out)})")
        log(f" stderr={err} ({type(err)})")
        log.error(f"Error {proc.returncode} running source script {file_to_source_path!r}")
        log.error(f" {e}")
        return {}
    log(f"stdout({filename})={out}")
    log(f"stderr({filename})={err}")
    def proc_str(b, fdname="stdout"):
        try:
            return (b or b"").decode()
        except UnicodeDecodeError:
            log.error(f"Error decoding {fdname} from {filename!r}", exc_info=True)
        return ""
    env = {}
    env.update(decode(proc_str(out, "stdout")))
    env.update(decode_dict(proc_str(err, "stderr")))
    log("env_from_sourcing%s=%s", (file_to_source_path, include_unexported_variables), env)
    return env


def sh_quotemeta(s):
    return b"'" + s.replace(b"'", b"'\\''") + b"'"

def xpra_env_shell_script(socket_dir, env):
    script = [b"#!/bin/sh", b""]
    for var, value in env.items():
        # these aren't used by xpra, and some should not be exposed
        # as they are either irrelevant or simply do not match
        # the new environment used by xpra
        # TODO: use a whitelist
        if var in (b"XDG_SESSION_COOKIE", b"LS_COLORS", b"DISPLAY"):
            continue
        #XPRA_SOCKET_DIR is a special case, it is handled below
        if var==b"XPRA_SOCKET_DIR":
            continue
        if var.startswith(b"BASH_FUNC"):
            #some versions of bash will apparently generate functions
            #that cannot be reloaded using this script
            continue
        # :-separated envvars that people might change while their server is
        # going:
        if var in (b"PATH", b"LD_LIBRARY_PATH", b"PYTHONPATH"):
            #prevent those paths from accumulating the same values multiple times,
            #only keep the first one:
            pathsep = os.pathsep.encode()
            pval = value.split(pathsep)      #ie: ["/usr/bin", "/usr/local/bin", "/usr/bin"]
            seen = set()
            value = pathsep.join(x for x in pval if not (x in seen or seen.add(x)))
            script.append(b"%s=%s:\"$%s\"; export %s"
                          % (var, sh_quotemeta(value), var, var))
        else:
            script.append(b"%s=%s; export %s"
                          % (var, sh_quotemeta(value), var))
    #XPRA_SOCKET_DIR is a special case, we want to honour it
    #when it is specified, but the client may override it:
    if socket_dir:
        script.append(b'if [ -z "${XPRA_SOCKET_DIR}" ]; then')
        script.append(b'    XPRA_SOCKET_DIR="%s"; export XPRA_SOCKET_DIR' %
                      sh_quotemeta(os.path.expanduser(socket_dir).encode()))
        script.append(b'fi')
    script.append(b"")
    return b"\n".join(script)

def xpra_runner_shell_script(xpra_file, starting_dir):
    script = []
    # We ignore failures in cd'ing, b/c it's entirely possible that we were
    # started from some temporary directory and all paths are absolute.
    script.append(b"cd %s" % sh_quotemeta(starting_dir.encode()))
    if OSX:
        #OSX contortions:
        #The executable is the python interpreter,
        #which is execed by a shell script, which we have to find..
        sexec = sys.executable
        bini = sexec.rfind("Resources/bin/")
        if bini>0:
            sexec = os.path.join(sexec[:bini], "Resources", "MacOS", "Xpra")
        script.append(b"_XPRA_SCRIPT=%s\n" % (sh_quotemeta(sexec.encode()),))
        script.append(b"""
if command -v "$_XPRA_SCRIPT" > /dev/null; then
    # Happypath:
    exec "$_XPRA_SCRIPT" "$@"
else
    # Hope for the best:
    exec Xpra "$@"
fi
""")
    else:
        script.append(b"_XPRA_PYTHON=%s" % (sh_quotemeta(sys.executable.encode()),))
        script.append(b"_XPRA_SCRIPT=%s" % (sh_quotemeta(xpra_file.encode()),))
        script.append(b"""
if command -v "$_XPRA_PYTHON" > /dev/null && [ -e "$_XPRA_SCRIPT" ]; then
    # Happypath:
    exec "$_XPRA_PYTHON" "$_XPRA_SCRIPT" "$@"
else
    cat >&2 <<END
    Could not find one or both of '$_XPRA_PYTHON' and '$_XPRA_SCRIPT'
    Perhaps your environment has changed since the xpra server was started?
    I'll just try executing 'xpra' with current PATH, and hope...
END
    exec xpra "$@"
fi
""")
    return b"\n".join(script)

def write_runner_shell_scripts(contents, overwrite=True):
    assert POSIX
    # This used to be given a display-specific name, but now we give it a
    # single fixed name and if multiple servers are started then the last one
    # will clobber the rest.  This isn't great, but the tradeoff is that it
    # makes it possible to use bare 'ssh:hostname' display names and
    # autodiscover the proper numeric display name when only one xpra server
    # is running on the remote host.  Might need to revisit this later if
    # people run into problems or autodiscovery turns out to be less useful
    # than expected.
    log = get_util_logger()
    MODE = 0o700
    from xpra.platform.paths import get_script_bin_dirs
    for d in get_script_bin_dirs():
        scriptdir = osexpand(d)
        if not os.path.exists(scriptdir):
            try:
                os.mkdir(scriptdir, MODE)
            except Exception as e:
                log("os.mkdir(%s, %s)", scriptdir, oct(MODE), exc_info=True)
                log.warn("Warning: failed to create script directory '%s':", scriptdir)
                log.warn(" %s", e)
                if scriptdir.startswith("/var/run/user") or scriptdir.startswith("/run/user"):
                    log.warn(" ($XDG_RUNTIME_DIR has not been created?)")
                continue
        scriptpath = os.path.join(scriptdir, "run-xpra")
        if os.path.exists(scriptpath) and not overwrite:
            continue
        # Write out a shell-script so that we can start our proxy in a clean
        # environment:
        try:
            with umask_context(0o022):
                h = os.open(scriptpath, os.O_WRONLY|os.O_CREAT|os.O_TRUNC, MODE)
                try:
                    os.write(h, contents)
                finally:
                    os.close(h)
        except Exception as e:
            log("writing to %s", scriptpath, exc_info=True)
            log.error("Error: failed to write script file '%s':", scriptpath)
            log.error(" %s\n", e)


def open_log_file(logpath):
    """ renames the existing log file if it exists,
        then opens it for writing.
    """
    if os.path.exists(logpath):
        try:
            os.rename(logpath, logpath + ".old")
        except OSError:
            pass
    try:
        return os.open(logpath, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    except OSError as e:
        raise InitException(f"cannot open log file {logpath!r}: {e}") from None

def select_log_file(log_dir, log_file, display_name):
    """ returns the log file path we should be using given the parameters,
        this may return a temporary logpath if display_name is not available.
    """
    if log_file:
        if os.path.isabs(log_file):
            logpath = log_file
        else:
            logpath = os.path.join(log_dir, log_file)
        v = shellsub(logpath, {"DISPLAY" : display_name})
        if display_name or v==logpath:
            #we have 'display_name', or we just don't need it:
            return v
    if display_name:
        logpath = norm_makepath(log_dir, display_name) + ".log"
    else:
        logpath = os.path.join(log_dir, f"tmp_{os.getpid()}.log")
    return logpath

# Redirects stdin from /dev/null, and stdout and stderr to the file with the
# given file descriptor. Returns file objects pointing to the old stdout and
# stderr, which can be used to write a message about the redirection.
def redirect_std_to_log(logfd):
    # preserve old stdio in new filehandles for use (and subsequent closing)
    # by the caller
    old_fd_stdout = os.dup(1)
    old_fd_stderr = os.dup(2)
    stdout = os.fdopen(old_fd_stdout, "w", 1)
    stderr = os.fdopen(old_fd_stderr, "w", 1)

    # close the old stdio file handles
    os.close(0)
    os.close(1)
    os.close(2)

    # replace stdin with /dev/null
    fd0 = os.open("/dev/null", os.O_RDONLY)
    if fd0 != 0:
        os.dup2(fd0, 0)
        os.close(fd0)

    # replace standard stdout/stderr by the log file
    os.dup2(logfd, 1)
    os.dup2(logfd, 2)
    os.close(logfd)

    # Make these line-buffered:
    sys.stdout = os.fdopen(1, "w", 1)
    sys.stderr = os.fdopen(2, "w", 1)
    return stdout, stderr


def daemonize():
    os.chdir("/")
    if os.fork():
        os._exit(0)     #pylint: disable=protected-access
    os.setsid()
    if os.fork():
        os._exit(0)     #pylint: disable=protected-access


def write_pidfile(pidfile):
    log = get_util_logger()
    pidstr = str(os.getpid())
    inode = 0
    try:
        with open(pidfile, "w", encoding="latin1") as f:
            if POSIX:
                os.fchmod(f.fileno(), 0o640)
            f.write(f"{pidstr}\n")
            try:
                inode = os.fstat(f.fileno()).st_ino
            except OSError:
                inode = 0
        log.info(f"wrote pid {pidstr} to {pidfile!r}")
    except Exception as e:
        log(f"write_pidfile({pidfile})", exc_info=True)
        log.info(f"Error: failed to write pid {pidstr} to {pidfile!r}")
        log.error(f" {e}")
    return inode

def rm_pidfile(pidfile, inode):
    #verify this is the right file!
    log = get_util_logger()
    log("cleanuppidfile(%s, %s)", pidfile, inode)
    if inode>0:
        try:
            i = os.stat(pidfile).st_ino
            log("cleanuppidfile: current inode=%i", i)
            if i!=inode:
                return 0
        except OSError:
            pass
    try:
        os.unlink(pidfile)
    except OSError:
        log("rm_pidfile(%s, %s)", pidfile, inode, exc_info=True)
    return 0


def get_uinput_device_path(device):
    log = get_util_logger()
    try:
        log("get_uinput_device_path(%s)", device)
        fd = device._Device__uinput_fd
        log("fd(%s)=%s", device, fd)
        import fcntl        #@UnresolvedImport
        import ctypes
        l = 16
        buf = ctypes.create_string_buffer(l)
        #this magic value was calculated using the C macros:
        l = fcntl.ioctl(fd, 2148554028, buf)
        if 0<l<16:
            virt_dev_path = buf.raw[:l].rstrip(b"\0")
            log("UI_GET_SYSNAME(%s)=%s", fd, virt_dev_path)
            uevent_path = b"/sys/devices/virtual/input/%s" % virt_dev_path
            event_dirs = [x for x in os.listdir(uevent_path) if x.startswith(b"event")]
            log("event dirs(%s)=%s", uevent_path, event_dirs)
            for d in event_dirs:
                uevent_filename = os.path.join(uevent_path, d, b"uevent")
                uevent_conf = open(uevent_filename, "rb", encoding="latin1").read()
                for line in uevent_conf.splitlines():
                    if line.find(b"=")>0:
                        k,v = line.split(b"=", 1)
                        log("%s=%s", k, v)
                        if k==b"DEVNAME":
                            dev_path = b"/dev/%s" % v
                            log(f"found device path: {dev_path}")
                            return dev_path
    except Exception as e:
        log("get_uinput_device_path(%s)", device, exc_info=True)
        log.error("Error: cannot query uinput device path:")
        log.estr(e)
    return None

def has_uinput():
    if not envbool("XPRA_UINPUT", True):
        return False
    try:
        import uinput
        assert uinput
    except NameError as e:
        log = get_util_logger()
        log("has_uinput()", exc_info=True)
        log.warn("Warning: the system python uinput module looks broken:")
        log.warn(" %s", e)
        return False
    except ImportError as e:
        log = get_util_logger()
        log("has_uinput()", exc_info=True)
        log.info("no uinput module (not usually needed)")
        return False
    try:
        uinput.fdopen()         #@UndefinedVariable
    except Exception as e:
        log = get_util_logger()
        log("has_uinput()", exc_info=True)
        log.info("cannot use uinput for virtual devices,")
        log.info(" this is usually a permission issue:")
        log.info(" %s", e)
        return False
    return True

def create_uinput_device(uuid, uid, events, name):
    log = get_util_logger()
    import uinput  # @UnresolvedImport
    BUS_USB = 0x03
    #BUS_VIRTUAL = 0x06
    VENDOR = 0xffff
    PRODUCT = 0x1000
    #our 'udev_product_version' script will use the version attribute to set
    #the udev OWNER value
    VERSION = uid
    try:
        device = uinput.Device(events, name=name, bustype=BUS_USB, vendor=VENDOR, product=PRODUCT, version=VERSION)
    except OSError as e:
        log("uinput.Device creation failed", exc_info=True)
        if os.getuid()==0:
            #running as root, this should work!
            log.error("Error: cannot open uinput,")
            log.error(" make sure that the kernel module is loaded")
            log.error(" and that the /dev/uinput device exists:")
            log.estr(e)
        return None
    dev_path = get_uinput_device_path(device)
    if not dev_path:
        device.destroy()
        return None
    return name, device, dev_path

def create_uinput_pointer_device(uuid, uid):
    if not envbool("XPRA_UINPUT_POINTER", True):
        return None
    from uinput import (
        REL_X, REL_Y, REL_WHEEL,                    #@UnresolvedImport
        BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE,  #@UnresolvedImport
        BTN_EXTRA, BTN_FORWARD, BTN_BACK,           #@UnresolvedImport
        )
    events = (
        REL_X, REL_Y, REL_WHEEL,
        BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_SIDE,
        BTN_EXTRA, BTN_FORWARD, BTN_BACK,
        )
    #REL_HIRES_WHEEL = 0x10
    #uinput.REL_HWHEEL,
    name = "Xpra Virtual Pointer %s" % uuid
    return create_uinput_device(uuid, uid, events, name)

def create_uinput_touchpad_device(uuid, uid):
    if not envbool("XPRA_UINPUT_TOUCHPAD", False):
        return None
    from uinput import (
        BTN_TOUCH, ABS_X, ABS_Y, ABS_PRESSURE,      #@UnresolvedImport
        )
    events = (
        BTN_TOUCH,
        ABS_X + (0, 2**24-1, 0, 0),
        ABS_Y + (0, 2**24-1, 0, 0),
        ABS_PRESSURE + (0, 255, 0, 0),
        #BTN_TOOL_PEN,
        )
    name = "Xpra Virtual Touchpad %s" % uuid
    return create_uinput_device(uuid, uid, events, name)


def create_uinput_devices(uinput_uuid, uid):
    log = get_util_logger()
    try:
        import uinput  # @UnresolvedImport
        assert uinput
    except (ImportError, NameError) as e:
        log.error("Error: cannot access python uinput module:")
        log.estr(e)
        return {}
    pointer = create_uinput_pointer_device(uinput_uuid, uid)
    touchpad = create_uinput_touchpad_device(uinput_uuid, uid)
    if not pointer and not touchpad:
        return {}
    def i(device):
        if not device:
            return {}
        name, uinput_pointer, dev_path = device
        return {
            "name"      : name,
            "uinput"    : uinput_pointer,
            "device"    : dev_path,
            }
    return {
        "pointer"   : i(pointer),
        "touchpad"  : i(touchpad),
        }

def create_input_devices(uinput_uuid, uid):
    return create_uinput_devices(uinput_uuid, uid)
