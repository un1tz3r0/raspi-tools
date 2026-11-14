"""Run a command as root for a front end that cannot signal it.

    pkexec env ... python -m piimg.frontend.supervise COMMAND...

A front end runs as the user and starts commands that need root through
pkexec. The root child then cannot be signalled by the user (kill() needs a
matching uid), so this small supervisor sits in between: it runs COMMAND in a
new session with stdout and stderr passed straight through, and takes orders
on its own stdin, one JSON object per line:

    {"data": "text"}     write text to COMMAND's stdin (a password, say)
    {"close": true}      close COMMAND's stdin
    {"signal": 2}        send that signal to COMMAND's process group

End of input (the front end went away) counts as {"signal": 2}. The exit
status is COMMAND's (128 + signal number if a signal ended it).
"""
import json
import os
import signal
import subprocess
import sys
import threading


def encode(**order):
    return (json.dumps(order) + "\n").encode()


def main(argv):
    if not argv:
        print("usage: python -m piimg.frontend.supervise COMMAND...", file=sys.stderr)
        return 2
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, start_new_session=True)

    def signal_child(sig):
        try:
            os.killpg(child.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    def orders():
        for line in sys.stdin.buffer:
            try:
                order = json.loads(line)
            except ValueError:
                continue
            if "data" in order and child.stdin:
                try:
                    child.stdin.write(str(order["data"]).encode())
                    child.stdin.flush()
                except (BrokenPipeError, ValueError):
                    pass
            if order.get("close") and child.stdin:
                try:
                    child.stdin.close()
                except BrokenPipeError:
                    pass
            if "signal" in order:
                signal_child(int(order["signal"]))
        if child.poll() is None:
            signal_child(signal.SIGINT)

    threading.Thread(target=orders, daemon=True).start()
    # the front end signals us only by an order; a terminal's Ctrl-C reaches the child itself
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    rc = child.wait()
    return 128 - rc if rc < 0 else rc


if __name__ == "__main__":
    status = main(sys.argv[1:])
    sys.stdout.flush()
    sys.stderr.flush()
    # not sys.exit: the order reader may still be blocked on stdin, and Python
    # aborts when a daemon thread holds the stdin lock at shutdown
    os._exit(status)
