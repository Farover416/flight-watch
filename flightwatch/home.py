"""The price checks that run on your laptop, every 2 hours while it is on.

Why they exist: Google shows GitHub's servers only the airlines' own fares.
Measured on 23 Sep 2026 with the same search a few minutes apart, every
browser tried on GitHub - Chrome and Edge, hidden and with a window, UTC and
Singapore time, watched for three minutes - got S$749, while the laptop and
a phone in Singapore got S$497 plus the agency and two-ticket fares. The
difference is the connection, and a laptop at home has the right one.

So this is the same watcher, run from here. It files its prices beside
GitHub's (data/home, data/taipei/home) and labels its messages, so both
sides can save at the same moment without a clash and you can always tell
which one you are reading. GitHub keeps checking round the clock; these add
the real prices whenever the laptop is on.

    python -m flightwatch.home setup    once: the token file, then the task
    pythonw -m flightwatch.home run     what the scheduled task starts

Everything lives in one folder - projects\\fw-home: this clone, its Python,
telegram-token.txt and home-log.txt. Delete the task and that folder and
nothing is left behind.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from xml.sax.saxutils import escape

REPO = Path(__file__).resolve().parent.parent
BASE = REPO.parent
LOG = BASE / "home-log.txt"
TOKEN = BASE / "telegram-token.txt"
LOCK = BASE / "home-run.lock"
TASK = "Flight Watch laptop check"

# What the scheduled GitHub runs check when nobody asked for anything else -
# ONLY in .github/workflows/check.yml. Change both together.
DECEMBER_ONLY = "Beijing,Qingdao"
# GitHub checks Taipei four times a day, and nothing two months out moves
# faster than that; checking it every laptop run would only cost time.
TAIPEI_EVERY = dt.timedelta(hours=6)
# The only paths ever committed from here. GitHub never writes them, and this
# never writes anything else, which is what keeps the two from colliding.
OWN = ("data/home", "data/taipei/home")

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# December now opens every search page (flightwatch/survey.py): about forty
# minutes on its own, and Taipei can follow it in the same check.
WATCH_TIMEOUT = 100 * 60


def _now() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _git_exe() -> str:
    found = shutil.which("git")
    if found:
        return found
    for guess in (r"C:\Program Files\Git\cmd\git.exe",
                  r"C:\Program Files (x86)\Git\cmd\git.exe"):
        if Path(guess).exists():
            return guess
    return "git"


def _python() -> str:
    """The console python beside whichever one is running this.

    The task starts pythonw, which has no console to write to; the watcher
    itself runs under python.exe, hidden, with its output going to the log.
    """
    here = Path(sys.executable)
    beside = here.with_name("python.exe")
    return str(beside) if beside.exists() else str(here)


def _token() -> str:
    try:
        text = TOKEN.read_text(encoding="utf-8-sig")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip().strip('"').strip("'")
        if "=" in line:                      # TELEGRAM_BOT_TOKEN=123:abc
            line = line.split("=", 1)[1].strip().strip('"').strip("'")
        if ":" in line and " " not in line:  # the shape of a bot token
            return line
    return ""


def _trim_log(limit: int = 2_000_000, keep: int = 500_000) -> None:
    try:
        if LOG.stat().st_size > limit:
            with LOG.open("rb") as handle:
                handle.seek(-keep, os.SEEK_END)
                tail = handle.read()
            LOG.write_bytes(b"...(older lines trimmed)\n" + tail)
    except OSError:
        pass


def _lock() -> bool:
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - LOCK.stat().st_mtime
        except OSError:
            return False
        if age > 90 * 60:                    # a check that died mid-way
            LOCK.unlink(missing_ok=True)
            return _lock()
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


class _Run:
    """One check: catch up, look, save - with everything said in the log."""

    def __init__(self, out):
        self.out = out
        self.env = dict(os.environ,
                        FLIGHTWATCH_SOURCE="home",
                        PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                        # Never wait on a password box nobody can see.
                        GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never")
        self.git_exe = _git_exe()

    def say(self, message: str) -> None:
        self.out.write(f"{_now()} {message}\n")
        self.out.flush()

    def _call(self, args, timeout) -> int:
        self.out.flush()
        try:
            return subprocess.run(args, cwd=REPO, env=self.env, stdout=self.out,
                                  stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                  creationflags=NO_WINDOW, timeout=timeout).returncode
        except subprocess.TimeoutExpired:
            self.say(f"gave up after {timeout // 60} min: {' '.join(args[:4])}")
            return 1
        except OSError as exc:
            self.say(f"could not start {args[0]}: {exc}")
            return 1

    def git(self, *args) -> int:
        return self._call([self.git_exe, *args], timeout=180)

    def watch(self, *args) -> int:
        self.say("checking " + " ".join(args))
        code = self._call([_python(), "-m", "flightwatch", *args],
                          timeout=WATCH_TIMEOUT)
        self.say(f"finished ({'ok' if code == 0 else f'exit {code}'})")
        return code

    def ahead(self) -> int:
        """How many saved-but-not-yet-uploaded commits this clone holds."""
        self.out.flush()
        try:
            done = subprocess.run(
                [self.git_exe, "rev-list", "--count", "origin/main..HEAD"],
                cwd=REPO, env=self.env, capture_output=True, text=True,
                creationflags=NO_WINDOW, timeout=60)
            return int(done.stdout.strip() or 0)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 0

    def catch_up(self) -> bool:
        if self.git("pull", "-q", "--rebase", "--autostash", "origin", "main") == 0:
            return True
        self.git("rebase", "--abort")
        return False

    def save(self) -> bool:
        own = [p for p in OWN if (REPO / p).exists()]
        if own:
            self.git("add", "--", *own)
        if own and self.git("diff", "--cached", "--quiet") != 0:
            stamp = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
            self.git("-c", "user.name=flight-watch",
                     "-c", "user.email=flight-watch@users.noreply.github.com",
                     "commit", "-q", "-m", f"prices (laptop): {stamp}")
        if not self.ahead():
            self.say("nothing new to save")
            return True
        for attempt in range(3):
            if self.catch_up() and self.git("push", "-q", "origin", "HEAD:main") == 0:
                self.say("saved to GitHub")
                return True
            time.sleep(15 * (attempt + 1))
        self.say("could not save to GitHub - these prices stay here and go up "
                 "with the next check")
        return False


def _taipei_due() -> bool:
    history = REPO / "data" / "taipei" / "home" / "history.jsonl"
    try:
        lines = history.read_text(encoding="utf-8").strip().splitlines()
        last = json.loads(lines[-1])["ts"]
        when = dt.datetime.fromisoformat(last.rstrip("Z"))
    except (OSError, IndexError, KeyError, ValueError):
        return True
    return dt.datetime.utcnow() - when >= TAIPEI_EVERY


def run() -> int:
    """What the scheduled task starts. Never asks anything; says it all in the log."""
    BASE.mkdir(parents=True, exist_ok=True)
    _trim_log()
    with LOG.open("a", encoding="utf-8", errors="replace") as out:
        job = _Run(out)
        if not _lock():
            job.say("another check is still running - skipped")
            return 0
        try:
            job.say("=== laptop check ===")
            token = _token()
            if token:
                job.env["TELEGRAM_BOT_TOKEN"] = token
            else:
                job.env.pop("TELEGRAM_BOT_TOKEN", None)
                job.say(f"no Telegram token in {TOKEN.name} yet - prices are "
                        "saved to the app but not sent")
            if not job.catch_up():
                job.say("could not catch up with GitHub (offline?) - trying "
                        "again next time")
                return 1
            job.watch("--only", DECEMBER_ONLY)
            if _taipei_due():
                job.watch("--trip", "taipei")
            else:
                job.say("Taipei was checked from here in the last "
                        f"{int(TAIPEI_EVERY.total_seconds() // 3600)} h - skipped")
            return 0 if job.save() else 1
        except Exception as exc:  # the log is the only place anyone will look
            job.say(f"FAILED: {exc!r}")
            return 1
        finally:
            LOCK.unlink(missing_ok=True)


def task_xml(command: str, arguments: str, workdir: str, user: str | None,
             start: dt.datetime) -> str:
    """The scheduled task: every 2 hours, and soon after waking if one was missed.

    Runs as you, only while you are signed in - so no password is stored and
    nothing runs on a locked-away account. Allowed on battery, skipped with
    no network, never wakes the laptop, and stopped if one ever runs past
    110 minutes (a normal check takes 40 to 60, opening every search page).
    """
    who = f"\n      <UserId>{escape(user)}</UserId>" if user else ""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Flight Watch: checks flight prices from this laptop every 2 hours while it is on, and saves them to the app. Log: {escape(str(LOG))}</Description>
  </RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition>
        <Interval>PT2H</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start.strftime("%Y-%m-%dT%H:%M:%S")}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">{who}
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT110M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(command)}</Command>
      <Arguments>{escape(arguments)}</Arguments>
      <WorkingDirectory>{escape(workdir)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def setup() -> int:
    """Once: make the token file (opened for pasting), then the scheduled task."""
    BASE.mkdir(parents=True, exist_ok=True)
    if not TOKEN.exists():
        TOKEN.write_text("", encoding="utf-8")
    if _token():
        print("Telegram token: found")
    else:
        print(f"Telegram token: not yet - opening {TOKEN} for you to paste it into")
        if os.name == "nt":
            subprocess.Popen(["notepad.exe", str(TOKEN)])

    if os.name != "nt":
        print("not Windows - no scheduled task made")
        return 0

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.exists():
        print(f"no pythonw.exe beside {sys.executable} - cannot schedule")
        return 1
    try:
        user = subprocess.run(["whoami"], capture_output=True, text=True,
                              creationflags=NO_WINDOW, timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        user = ""
    # Ten minutes' grace, so the first check finds the token already pasted.
    start = (dt.datetime.now() + dt.timedelta(minutes=10)).replace(second=0,
                                                                   microsecond=0)
    xml = task_xml(str(pythonw), "-m flightwatch.home run", str(REPO),
                   user or None, start)
    spec = BASE / "home-task.xml"
    spec.write_text(xml, encoding="utf-16")
    try:
        made = subprocess.run(["schtasks", "/create", "/tn", TASK, "/xml",
                               str(spec), "/f"], capture_output=True, text=True,
                              creationflags=NO_WINDOW, timeout=60)
    finally:
        spec.unlink(missing_ok=True)
    print((made.stdout or "").strip())
    print((made.stderr or "").strip())
    if made.returncode != 0:
        print("COULD NOT CREATE THE SCHEDULED TASK")
        return 1
    print(f'scheduled task "{TASK}": every 2 hours from {start:%H:%M}, '
          f"while you are signed in; log in {LOG}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "run"
    if command == "run":
        return run()
    if command == "setup":
        return setup()
    print("usage: python -m flightwatch.home [run|setup]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
