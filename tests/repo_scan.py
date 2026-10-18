"""Repository hygiene scan (SPEC-G section 6): nothing host- or person-specific and no secret may be published.

Run: /usr/bin/python3.12 tests/repo_scan.py [REPO_ROOT]   (exit 0: clean, 1: findings, one `path:line: rule: text`)

The scanned files are what would be published: `git ls-files` (tracked and staged) in a git checkout, else every
file under the root. tests/test_repo_scan.py runs it on this repository and on planted samples.
"""

import ipaddress
import re
import socket
import subprocess
import sys
from pathlib import Path

# Written in pieces so that this file does not match itself.
BANNED_STRINGS = ("t1-" + "naver", "223" + ".130.", "haamsuk" + "@")

# The account homes the documentation names (README, docs/CONFIG.md): the interactive user's home is the only one
# under /home; agwork, agplan and agbot live under /var/lib.
DOCUMENTED_HOMES = frozenset({"agent"})

# The spec files quote BANNED_STRINGS to define the rule, so only that rule skips them.
SPEC_FILE_RE = re.compile(r"(^|/)SPEC(-[A-Z])?\.md$")
# Third-party folder prefixes credited in NOTICE: only the IP-address rule is skipped inside them.
VENDORED = ("guidance/user/skills/insane-search/",)
CHAT_ID_RE = re.compile(r"""\b(owner_id|chat_id)["']?\s*[:=]\s*-?\d{5,}""")
TELEGRAM_TOKEN_RE = re.compile(r"\d{8,10}:[A-Za-z0-9_-]{35}")
API_KEY_RES = (
    re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
IPV4_RE = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w.])")
# Addresses that identify no host: loopback, "any", the documentation ranges (RFC 5737), and link-local
# (169.254.0.0/16, the cloud metadata address that SSRF guards name).
ALLOWED_NETS = tuple(ipaddress.ip_network(n) for n in
                     ("127.0.0.0/8", "0.0.0.0/32", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
                      "169.254.0.0/16"))
HOME_RE = re.compile(r"(?<![\w.])/(home|Users)/([A-Za-z0-9_][A-Za-z0-9_.-]*)")
TEXT_SNIFF_BYTES = 8192


def host_names() -> tuple[str, ...]:
    """The machine's own host name and its first label, when they could name it (not "localhost")."""
    full = socket.gethostname().strip().lower()
    names = {full, full.split(".")[0]}
    return tuple(sorted(n for n in names if len(n) >= 4 and n not in ("localhost", "localhost.localdomain")))


def files_to_scan(root: Path) -> list[Path]:
    if (root / ".git").exists():
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached"],
                             capture_output=True, check=True).stdout
        names = [n for n in out.decode("utf-8", "surrogateescape").split("\0") if n]
        return [root / n for n in sorted(set(names)) if (root / n).is_file() and not (root / n).is_symlink()]
    skip = {".git", "__pycache__"}
    return sorted(p for p in root.rglob("*")
                  if p.is_file() and not p.is_symlink() and not skip.intersection(p.relative_to(root).parts))


def scan_text(rel: str, text: str, hosts: tuple[str, ...] = ()) -> list[str]:
    """Findings for one file's text, each `rel:line: rule: excerpt`."""
    findings = []
    spec_file = SPEC_FILE_RE.search(rel) is not None
    vendored = any(rel.startswith(prefix.rstrip("/") + "/") for prefix in VENDORED)
    for number, line in enumerate(text.splitlines(), 1):
        def hit(rule: str, excerpt: str) -> None:
            findings.append(f"{rel}:{number}: {rule}: {excerpt}")

        lowered = line.lower()
        if not spec_file:
            for banned in BANNED_STRINGS:
                if banned in lowered:
                    hit("banned string", banned)
        for host in hosts:
            if host in lowered:
                hit("host name", host)
        if not vendored:
            for match in IPV4_RE.finditer(line):
                try:
                    address = ipaddress.ip_address(match.group(1))
                except ValueError:
                    continue  # not an address (an octet over 255)
                if not any(address in net for net in ALLOWED_NETS):
                    hit("IP address", match.group(1))
        for match in TELEGRAM_TOKEN_RE.finditer(line):
            hit("Telegram token", match.group(0)[:12] + "…")
        for match in CHAT_ID_RE.finditer(line):
            hit("chat id", match.group(0))
        for pattern in API_KEY_RES:
            for match in pattern.finditer(line):
                hit("API key", match.group(0)[:12] + "…")
        for match in HOME_RE.finditer(line):
            if match.group(1) == "Users" or match.group(2) not in DOCUMENTED_HOMES:
                hit("personal home path", match.group(0))
    return findings


def scan_git_log(root: Path, hosts: tuple[str, ...] = ()) -> list[str]:
    """Findings for git commit authors (and distinct committers), each `commit <hash> <field>: <rule>: <excerpt>`."""
    if not (root / ".git").exists():
        return []
    out = subprocess.run(
        ["git", "-C", str(root), "log", "--all", "--format=%h%x00%an%x00%ae%x00%cn%x00%ce"],
        capture_output=True, check=True,
    ).stdout.decode("utf-8", "replace")
    findings = []
    for line in out.splitlines():
        if not line:
            continue
        commit, an, ae, cn, ce = line.split("\0")
        fields = [("author", f"{an}\0{ae}")]
        if (cn, ce) != (an, ae):
            fields.append(("committer", f"{cn}\0{ce}"))
        for role, value in fields:
            lowered = value.lower()
            for banned in BANNED_STRINGS:
                if banned in lowered:
                    findings.append(f"commit {commit} {role}: banned string: {banned}")
            for host in hosts:
                if host in lowered:
                    findings.append(f"commit {commit} {role}: host name: {host}")
    return findings


def scan(root: Path, hosts: tuple[str, ...] | None = None) -> list[str]:
    hosts = host_names() if hosts is None else hosts
    findings = []
    notice = root / "NOTICE"
    if notice.is_file():
        notice_text = notice.read_text(encoding="utf-8", errors="replace")
        for prefix in VENDORED:
            if prefix not in notice_text:
                findings.append(f"NOTICE:1: uncredited vendored path: {prefix}")
    files = files_to_scan(root)
    if (root / ".git").exists() and not files:
        findings.append(".git:0: empty repository: no tracked files to scan")
    for path in files:
        data = path.read_bytes()
        if b"\0" in data[:TEXT_SNIFF_BYTES]:
            continue  # binary
        findings.extend(scan_text(path.relative_to(root).as_posix(), data.decode("utf-8", "replace"), hosts))
    findings.extend(scan_git_log(root, hosts))
    return findings


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else Path(__file__).resolve().parent.parent)
    findings = scan(root)
    for line in findings:
        print(line)
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
