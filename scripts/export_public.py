"""Собрать чистую копию проекта для открытого репозитория.

    python scripts/export_public.py ../jobhunter-kit            # собрать и проверить
    python scripts/export_public.py ../jobhunter-kit --check    # только проверить

Берёт файлы, которые отслеживает git в этой копии, кроме личных и служебных
(EXCLUDE), и накладывает поверх содержимое public/ — README, инструкцию, .gitignore.

Перед записью всё проверяется, и при любой находке копия не собирается:
  - следы владельца: имя, почта, телефон, Telegram, компании и вуз из ЕГО
    profile.yaml — список строится на лету и никуда не записывается;
  - значения секретов из ЕГО .env (токены, ключи, пароли, id);
  - типовые шаблоны ключей (Telegram, Google, OpenAI-подобные, GitHub…);
  - файлы секретов и данных по именам (jobhunter.repo_safety.private_path).
Папка назначения очищается, кроме .git: повторный экспорт обновляет ту же копию.
"""
from __future__ import annotations

import fnmatch
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
sys.path.insert(0, str(ROOT))
from jobhunter.repo_safety import private_path  # noqa: E402

# Личное и служебное владельца: в открытую копию не идёт.
EXCLUDE = [
    "profile.yaml", "README.md", "HANDOFF.md", "INTERVIEWS.md", "interviews.ics",
    "AUDIT_*.md", "jobhunter_*.html", "output/*", "tmp/*", "docs/*", "public/*",
    "demo_tailor.py", "scripts/export_public.py",
]

SECRET_PATTERNS = {
    "telegram_bot_token": r"\b\d{8,10}:AA[A-Za-z0-9_-]{30,40}\b",
    "google_api_key": r"AIza[0-9A-Za-z_-]{35}",
    "google_oauth_secret": r"GOCSPX-[0-9A-Za-z_-]{20,}",
    "google_refresh_token": r"1//0[0-9A-Za-z_-]{30,}",
    "google_access_token": r"ya29\.[0-9A-Za-z_-]{20,}",
    "sk_key": r"\bsk-(?:proj-|or-v1-|ant-)?[A-Za-z0-9_-]{24,}",
    "groq_key": r"\bgsk_[A-Za-z0-9]{30,}",
    "github_token": r"\b(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}",
    "tavily_key": r"\btvly-[A-Za-z0-9-]{20,}",
    "hf_token": r"\bhf_[A-Za-z0-9]{30,}",
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
}
SECRET_ENV_KEYS = re.compile(
    r"TOKEN|HASH|KEY|PASSWORD|SECRET|PHONE|API_ID|USER_IDS|SMTP_USER|EMAIL|SALT|"
    r"HEALTHCHECK_URL|PIN|CLIENT_ID", re.I)


def excluded(rel: str) -> bool:
    return any(fnmatch.fnmatchcase(rel, pat) for pat in EXCLUDE)


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, check=True)
    return [n for n in out.stdout.decode("utf-8").split("\0") if n and (ROOT / n).is_file()]


def owner_markers() -> list[tuple[str, re.Pattern]]:
    """Что указывает на владельца: берётся из его profile.yaml, не хранится нигде."""
    path = ROOT / "profile.yaml"
    if not path.exists():
        return []
    p = yaml.safe_load(path.read_text(encoding="utf-8"))
    ident = p.get("identity", {})
    words = set()
    for key in ("full_name_ru", "full_name_en"):
        words |= {w for w in str(ident.get(key, "")).split() if len(w) >= 4}
    email = str(ident.get("email", ""))
    if "@" in email:
        words.add(email.split("@")[0])
    tg = str(ident.get("telegram", "")).rstrip("/").split("/")[-1].lstrip("@")
    if len(tg) >= 4:
        words.add(tg)
    if ident.get("birth_date"):
        words.add(str(ident["birth_date"]))
    for e in p.get("experience", []):
        if e.get("is_own_project"):
            continue
        for key in ("company", "company_en"):
            name = str(e.get(key) or "").strip()
            if len(name) >= 4:
                words.add(name)
    for ed in p.get("education", []):
        # Только имена собственные («Воронежский»), не «институт» и «университет».
        words |= {w for w in str(ed.get("institution_ru", "")).split()
                  if len(w) >= 6 and w[:1].isupper()}
    markers = [(w, re.compile(re.escape(w), re.I)) for w in sorted(words)]
    digits = re.sub(r"\D", "", str(ident.get("phone", "")))[-10:]
    if len(digits) == 10:
        markers.append(("phone", re.compile(r"\D{0,3}".join(digits))))
    return markers


def env_secrets() -> list[tuple[str, str]]:
    path = ROOT / ".env"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip("'\"")
        if SECRET_ENV_KEYS.search(key) and len(val) >= 6:
            out.append((key.strip(), val))
    return out


def build(target: Path) -> dict[str, bytes]:
    files = {}
    for rel in tracked_files():
        if not excluded(rel):
            files[rel] = (ROOT / rel).read_bytes()
    for src in PUBLIC.rglob("*"):
        if src.is_file():
            files[src.relative_to(PUBLIC).as_posix()] = src.read_bytes()
    return files


def scan(files: dict[str, bytes]) -> list[str]:
    problems = []
    markers, secrets = owner_markers(), env_secrets()
    patterns = {k: re.compile(v) for k, v in SECRET_PATTERNS.items()}
    for rel, data in sorted(files.items()):
        if private_path(rel):
            problems.append("%s: файл секрета или данных по имени" % rel)
            continue
        text = data.decode("utf-8", "replace")
        for label, rx in markers:
            if rx.search(text):
                problems.append("%s: след владельца (%s…)" % (rel, label[:2]))
        for key, val in secrets:
            if val in text:
                problems.append("%s: значение %s из .env" % (rel, key))
        for kind, rx in patterns.items():
            if rx.search(text):
                problems.append("%s: похоже на секрет (%s)" % (rel, kind))
    return problems


def write(target: Path, files: dict[str, bytes]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in target.iterdir():
        if child.name == ".git":
            continue
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    for rel, data in files.items():
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    target = Path(sys.argv[1]).resolve()
    if target == ROOT or ROOT in target.parents:
        print("Папка назначения должна быть вне проекта.")
        return 2
    files = build(target)
    problems = scan(files)
    print("файлов: %d; следов владельца проверено: %d; секретов из .env: %d"
          % (len(files), len(owner_markers()), len(env_secrets())))
    if problems:
        print("НАХОДКИ — копия не собрана:")
        for line in problems:
            print("  " + line)
        return 1
    if "--check" not in sys.argv:
        write(target, files)
        print("собрано в", target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
