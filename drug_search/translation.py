import hashlib
import json
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from subprocess import SubprocessError
from threading import Lock

from .transport import SourceError, fetch


RUSSIAN_LETTER = re.compile(r"[А-Яа-яЁё]")
ENGLISH_LETTER = re.compile(r"[A-Za-z]")
# Exact model outputs that need the spelling used in US drug labels.
US_DRUG_NAMES = {"paracetamol": "acetaminophen", "varfarin": "warfarin"}
CACHE_DIR = Path(__file__).resolve().parents[1] / ".bergamot"
WORKER = Path(__file__).with_name("bergamot_worker.cjs")
ENGINE_URL = (
    "https://raw.githubusercontent.com/mozilla/translate/"
    "14fdf57e323e532c3e11c75618e676c96d93df00/js"
)
MODEL_URL = (
    "https://storage.googleapis.com/moz-fx-translations-data--303e-prod-translations-data/"
    "models/ru-en/spring-2024_QrcdYgbwS7e7xbhtOSdoNQ/exported"
)
ASSETS = (
    (ENGINE_URL, "bergamot-translator-worker.js",
     "2cb354e739ba02cb04ee93da0c24f4b4d9de5e17c26fc41f1aeed4fb41811a5c"),
    (ENGINE_URL, "bergamot-translator-worker.wasm",
     "65cf5be754ea6d44db018a38e1f4a5c2fb2d7ef601d0e0067abde4e3d145386f"),
    (MODEL_URL, "model.ruen.intgemm.alphas.bin.gz",
     "4a8a7b9b07c9e06a167ec5bf2542528817321516db4edf614fda45011fa8e5d1"),
    (MODEL_URL, "lex.50.50.ruen.s2t.bin.gz",
     "6524f5c898f1fef52992bd2565a6d4acfafb6a4e8dcd6aef237bd888239418a0"),
    (MODEL_URL, "vocab.ruen.spm.gz",
     "cd70b828e99e4d0c79d48cd56d8579d656c87c1db20bf88883da3085dcbfef75"),
)
_TRANSLATION_LOCK = Lock()


class TranslationError(Exception):
    """The translator could not produce an English search query."""


def _ensure_asset(base: str, name: str, digest: str) -> None:
    destination = CACHE_DIR / name
    try:
        with destination.open("rb") as cached:
            if hashlib.file_digest(cached, "sha256").hexdigest() == digest:
                return
    except FileNotFoundError:
        pass

    content = fetch(f"{base}/{name}")
    if hashlib.sha256(content).hexdigest() != digest:
        raise TranslationError(f"Invalid checksum for {name}")

    with tempfile.NamedTemporaryFile(dir=CACHE_DIR, delete=False) as file:
        temporary = Path(file.name)
    try:
        temporary.write_bytes(content)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def translate_query(query: str) -> str:
    """Translate Cyrillic input locally. Other queries go straight to the search."""
    if not RUSSIAN_LETTER.search(query):
        return query

    try:
        from nodejs_wheel import node

        # Bound the number of model processes and avoid overlapping cold downloads.
        with _TRANSLATION_LOCK:
            CACHE_DIR.mkdir(exist_ok=True)
            with ThreadPoolExecutor(max_workers=len(ASSETS)) as executor:
                downloads = [executor.submit(_ensure_asset, *asset) for asset in ASSETS]
                for download in downloads:
                    download.result()

            result = node(
                [str(WORKER), str(CACHE_DIR)],
                return_completed_process=True,
                input=json.dumps(query),
                encoding="utf-8",
                capture_output=True,
                check=True,
                timeout=20,
            )
        translated = json.loads(result.stdout)
    except (ImportError, OSError, SubprocessError, SourceError, ValueError) as exc:
        raise TranslationError("Bergamot could not translate the query") from exc

    if not isinstance(translated, str):
        raise TranslationError("Bergamot returned an invalid result")
    translated = " ".join(translated.split()).strip().rstrip(".").strip()
    if not ENGLISH_LETTER.search(translated) or RUSSIAN_LETTER.search(translated):
        raise TranslationError("Bergamot did not return an English query")
    return US_DRUG_NAMES.get(translated.casefold(), translated)
