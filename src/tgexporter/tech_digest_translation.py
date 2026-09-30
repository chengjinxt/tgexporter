from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

from .tech_digest import BilingualArticle, TechCandidate


class TranslationError(RuntimeError):
    pass


class _ArgosTranslationBackend:
    REQUIRED_PAIRS = frozenset({("en", "zh"), ("zh", "en")})

    def __init__(self, model_dir: Path) -> None:
        self.model_dir = model_dir
        self._runtime: tuple[Any, Any] | None = None
        self._translators: dict[tuple[str, str], tuple[Any, Any]] = {}

    def validate(self) -> None:
        packages = _bundled_model_packages(self.model_dir)
        if not self.REQUIRED_PAIRS.issubset(packages):
            raise TranslationError(
                "Missing bundled English and Chinese models under "
                f"{self.model_dir}. Rebuild the portable package with local translation models."
            )
        for pair in self.REQUIRED_PAIRS:
            package_dir = packages[pair]
            required_files = (
                package_dir / "model" / "config.json",
                package_dir / "model" / "model.bin",
                package_dir / "sentencepiece.model",
            )
            if not all(path.is_file() for path in required_files):
                raise TranslationError(
                    f"Bundled local translation model is incomplete for {pair[0]}->{pair[1]}: {package_dir}"
                )
        self._load_runtime()

    def translate(self, text: str, source: str, target: str) -> str:
        ctranslate2, sentencepiece = self._load_runtime()
        pair = (source, target)
        try:
            loaded = self._translators.get(pair)
            if loaded is None:
                package_dir = _bundled_model_packages(self.model_dir).get(pair)
                if package_dir is None:
                    raise TranslationError(f"Missing bundled local translation model for {source}->{target}.")
                processor = sentencepiece.SentencePieceProcessor(
                    model_file=str(package_dir / "sentencepiece.model")
                )
                translator = ctranslate2.Translator(str(package_dir / "model"), device="cpu")
                loaded = (translator, processor)
                self._translators[pair] = loaded
            translator, processor = loaded
            tokens = processor.encode(text, out_type=str)
            result = translator.translate_batch(
                [tokens],
                beam_size=4,
                replace_unknowns=True,
            )[0]
            return str(processor.decode_pieces(result.hypotheses[0])).replace("▁", " ").replace("_", " ").strip()
        except Exception as exc:
            if isinstance(exc, TranslationError):
                raise
            raise TranslationError(f"Local LibreTranslate failed for {source}->{target}: {exc}") from exc

    def _load_runtime(self) -> tuple[Any, Any]:
        if self._runtime is not None:
            return self._runtime
        try:
            import ctranslate2
            import sentencepiece
        except (ImportError, OSError) as exc:
            raise TranslationError(
                "Bundled LibreTranslate runtime is unavailable. Rebuild the portable package "
                "with the local-translation dependency."
            ) from exc
        self._runtime = (ctranslate2, sentencepiece)
        return self._runtime


class LibreTranslateTranslator:
    def __init__(self, *, model_dir: Path, backend: Any | None = None) -> None:
        self.model_dir = Path(model_dir)
        self.backend = backend or _ArgosTranslationBackend(self.model_dir)

    def validate(self) -> None:
        self.backend.validate()

    def translate(self, candidate: TechCandidate) -> BilingualArticle:
        source_summary = _source_summary(candidate)
        source_text = f"{candidate.title}\n{source_summary}"
        if _is_chinese_text(source_text):
            zh_title = candidate.title.strip()
            zh_summary = _trim_chinese_summary(source_summary)
            en_title = self.translate_text(candidate.title, "zh", "en")
            en_summary = _trim_english_summary(self.translate_paragraphs(source_summary, "zh", "en"))
        else:
            en_title = candidate.title.strip()
            en_summary = _trim_english_summary(source_summary)
            zh_title = self.translate_text(candidate.title, "en", "zh")
            zh_summary = _trim_chinese_summary(self.translate_paragraphs(source_summary, "en", "zh"))
        return BilingualArticle(
            candidate=candidate,
            zh_title=zh_title,
            en_title=en_title,
            zh_summary=zh_summary,
            en_summary=en_summary,
        )

    def translate_text(self, text: str, source: str, target: str) -> str:
        try:
            translated = str(self.backend.translate(text.strip(), source, target)).strip()
        except TranslationError:
            raise
        except Exception as exc:
            raise TranslationError(f"Local LibreTranslate failed for {source}->{target}: {exc}") from exc
        if not translated:
            raise TranslationError(f"Local LibreTranslate returned empty text for {source}->{target}.")
        return _restore_technical_terms(text, translated)

    def translate_paragraphs(self, text: str, source: str, target: str) -> str:
        paragraphs = _summary_paragraphs(text)
        return "\n\n".join(self.translate_text(paragraph, source, target) for paragraph in paragraphs)


class OpenAICompatibleTranslator:
    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key_env: str = "OPENAI_API_KEY",
        timeout_seconds: int = 90,
        opener: Any | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        self.api_key_env = api_key_env
        self.timeout_seconds = timeout_seconds
        self.opener = opener or urllib.request.build_opener()
        self.environ = environ if environ is not None else os.environ

    def validate(self) -> None:
        self._api_key()

    def translate(self, candidate: TechCandidate) -> BilingualArticle:
        api_key = self._api_key()

        prompt = _translation_prompt(candidate)
        payload = {
            "model": self.model,
            "messages": [{"role": "developer", "content": prompt}],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "tgexporter/0.1",
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            values = _parse_json_content(content)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise TranslationError(f"Translation API failed: HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise TranslationError(f"Translation API returned an invalid response: {exc}") from exc
        required = ("zh_title", "en_title", "zh_summary", "en_summary")
        missing = [key for key in required if not str(values.get(key, "")).strip()]
        if missing:
            raise TranslationError(f"Translation response is missing fields: {', '.join(missing)}")
        zh_summary = _trim_chinese_summary(str(values["zh_summary"]))
        en_summary = _trim_english_summary(str(values["en_summary"]))
        if len(_summary_paragraphs(zh_summary)) < 2 or len(_summary_paragraphs(en_summary)) < 2:
            raise TranslationError("Translation response summaries must each contain two paragraphs.")
        return BilingualArticle(
            candidate=candidate,
            zh_title=str(values["zh_title"]).strip(),
            en_title=str(values["en_title"]).strip(),
            zh_summary=zh_summary,
            en_summary=en_summary,
        )

    def _api_key(self) -> str:
        if not self.model:
            raise TranslationError("Missing tech_digest.translation.model in config.local.toml.")
        api_key = self.environ.get(self.api_key_env, "").strip()
        if not api_key:
            raise TranslationError(f"Missing translation API key environment variable: {self.api_key_env}")
        return api_key


def build_tech_digest_translator(config: Any):
    provider = str(config.provider).strip().lower()
    if provider in {"libretranslate", "local"}:
        return LibreTranslateTranslator(model_dir=Path(config.model_dir))
    if provider in {"openai", "openai-compatible"}:
        return OpenAICompatibleTranslator(
            endpoint=config.endpoint,
            model=config.model,
            api_key_env=config.api_key_env,
            timeout_seconds=config.timeout_seconds,
        )
    raise TranslationError(f"Unsupported tech digest translation provider: {config.provider}")


def _bundled_model_packages(model_dir: Path) -> dict[tuple[str, str], Path]:
    packages: dict[tuple[str, str], Path] = {}
    if not model_dir.is_dir():
        return packages
    for metadata_path in model_dir.glob("*/metadata.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            source = str(metadata.get("from_code", "")).strip()
            target = str(metadata.get("to_code", "")).strip()
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if source and target:
            packages[(source, target)] = metadata_path.parent
    return packages


def _source_summary(candidate: TechCandidate) -> str:
    return (candidate.summary or candidate.title).strip()


def _is_chinese_text(text: str) -> bool:
    chinese = sum(1 for char in text if "\u3400" <= char <= "\u9fff")
    latin = sum(1 for char in text if char.isascii() and char.isalpha())
    return chinese > 0 and chinese * 2 >= latin


def _summary_paragraphs(text: str) -> list[str]:
    paragraphs = [" ".join(part.split()) for part in re.split(r"\n\s*\n", text.strip())]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]
    if len(paragraphs) != 1:
        return paragraphs
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[。！？!?])\s*|(?<=\.)\s+(?=[A-Z0-9])", paragraphs[0])
        if sentence.strip()
    ]
    if len(sentences) < 2:
        return paragraphs
    split_at = max(1, len(sentences) // 2)
    return [" ".join(sentences[:split_at]), " ".join(sentences[split_at:])]


def _trim_chinese_summary(text: str, limit: int = 280) -> str:
    paragraphs = _summary_paragraphs(text)
    if not paragraphs:
        return ""
    if sum(len(paragraph) for paragraph in paragraphs) <= limit:
        return "\n\n".join(paragraphs)
    remaining = limit
    rendered: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        paragraphs_left = len(paragraphs) - index - 1
        reserved = min(90 * paragraphs_left, max(0, remaining - 1))
        allowance = max(1, remaining - reserved)
        if len(paragraph) > allowance:
            paragraph = _truncate_chinese_paragraph(paragraph, allowance)
        rendered.append(paragraph)
        remaining -= len(paragraph)
        if remaining <= 0:
            break
    return "\n\n".join(rendered)


def _truncate_chinese_paragraph(paragraph: str, allowance: int) -> str:
    if allowance <= 1:
        return "…"
    shortened = paragraph[:allowance]
    sentence_end = max(shortened.rfind(mark) for mark in "。！？!?")
    if sentence_end >= min(30, allowance // 2):
        return shortened[: sentence_end + 1].strip()
    return shortened[: allowance - 1].rstrip("，,；;：:。.!！?？ ") + "…"


def _restore_technical_terms(source: str, translated: str) -> str:
    terms = {
        token
        for token in re.findall(r"\b[A-Za-z][A-Za-z0-9.-]*\b", source)
        if any(char.isupper() for char in token[1:]) or any(char.isdigit() for char in token)
    }
    if terms:
        def restore(match: re.Match[str]) -> str:
            token = match.group(0)
            for term in terms:
                if token.casefold() == term.casefold():
                    return term
                if (
                    len(term) >= 5
                    and abs(len(token) - len(term)) <= 1
                    and token[0].casefold() == term[0].casefold()
                    and _edit_distance_at_most_one(token.casefold(), term.casefold())
                ):
                    return term
            return token

        translated = re.sub(
            r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9.-]*(?![A-Za-z0-9])",
            restore,
            translated,
        )
    for bit_term in re.findall(r"\b\d+-bit\b", source, flags=re.IGNORECASE):
        bit_number = re.escape(bit_term.split("-", 1)[0])
        translated = re.sub(
            rf"(?<!\d){bit_number}\s*位(?=[A-Za-z])",
            f"{bit_term} ",
            translated,
        )
        translated = re.sub(
            rf"(?<!\d){bit_number}\s*位",
            bit_term,
            translated,
        )
    return translated


def _edit_distance_at_most_one(left: str, right: str) -> bool:
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) <= 1
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    short_index = 0
    long_index = 0
    differences = 0
    while short_index < len(shorter) and long_index < len(longer):
        if shorter[short_index] == longer[long_index]:
            short_index += 1
            long_index += 1
            continue
        differences += 1
        long_index += 1
        if differences > 1:
            return False
    return True


def _trim_english_summary(text: str, limit: int = 90) -> str:
    paragraphs = _summary_paragraphs(text)
    if not paragraphs:
        return ""
    remaining = limit
    rendered: list[str] = []
    for index, paragraph in enumerate(paragraphs):
        words = paragraph.split()
        paragraphs_left = len(paragraphs) - index - 1
        reserved = min(20 * paragraphs_left, max(0, remaining - 1))
        allowance = max(1, remaining - reserved)
        if len(words) > allowance:
            paragraph = " ".join(words[:allowance]).rstrip(".,;:!? ") + "…"
            used = allowance
        else:
            paragraph = " ".join(words)
            used = len(words)
        rendered.append(paragraph)
        remaining -= used
        if remaining <= 0:
            break
    return "\n\n".join(rendered)


def _translation_prompt(candidate: TechCandidate) -> str:
    source_summary = candidate.summary or "The source page did not provide a description. Use only the title facts."
    return f"""You edit a bilingual technology news digest. Return one JSON object with exactly these string fields:
zh_title, en_title, zh_summary, en_summary.
Keep names, figures, qualifications, and uncertainty faithful to the source. Do not add facts. Each summary must contain two short paragraphs and three to four informative sentences, not a one-sentence description. Cover the main event, important figures or technical details, and any limitation or unresolved point present in the source. The Chinese summary should be at most 280 Chinese characters; the English summary at most 90 words. End every paragraph with a complete sentence; never cut off a contrast or qualification. Do not reproduce the full article.

Source: {candidate.source_name}
Original title: {candidate.title}
Source article digest material: {source_summary}
URL: {candidate.url}
"""


def _parse_json_content(content: object) -> dict[str, object]:
    if not isinstance(content, str):
        raise TypeError("message content is not a string")
    cleaned = content.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    value = json.loads(cleaned)
    if not isinstance(value, dict):
        raise TypeError("message content is not a JSON object")
    return value
