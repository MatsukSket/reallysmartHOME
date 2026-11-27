"""Подготовка Markdown к индексации."""
import re
from pathlib import Path

import yaml

CHUNK_SIZE = 650
OVERLAP = 100
HEADING = re.compile(r"(?m)^(#{1,6})\s+(.+?)\s*$")
ERROR = re.compile(r"(?im)^(?:(?:ошибка|код)\s+)?(E\d{2})\s*[:.\-]")
NAME = re.compile(r"\w[\w .-]{0,99}\.md\Z", re.IGNORECASE)


def validate_name(name):
    reserved = re.fullmatch(r"con|prn|aux|nul|com[1-9]|lpt[1-9]", name.split(".", 1)[0], re.IGNORECASE)
    if not NAME.fullmatch(name) or reserved or Path(name).stem.endswith((" ", ".")):
        raise ValueError("Ожидается имя Markdown-файла без пути")


def windows(text):
    start = 0
    while start < len(text):
        end = min(start + CHUNK_SIZE, len(text))
        if end < len(text):
            boundary = text.rfind(" ", start + CHUNK_SIZE // 2, end)
            if boundary > start:
                end = boundary
        yield text[start:end].strip()
        if end == len(text):
            break
        start = end - OVERLAP


def parse_manual(name, raw):
    validate_name(name)
    text = raw.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip() or any(ord(char) < 32 and char not in "\n\t" for char in text):
        raise ValueError(f"{name}: пустой документ или повреждённый текст")
    meta = {}
    if text.startswith("---\n"):
        header, boundary, contents = text[4:].partition("\n---\n")
        if not boundary:
            raise ValueError(f"{name}: метаданные YAML не закрыты")
        try:
            meta = yaml.safe_load(header) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"{name}: некорректные метаданные YAML") from exc
        text = contents
    if not isinstance(meta, dict):
        raise ValueError(f"{name}: метаданные должны быть объектом")
    keys = ("device_type", "model", "category")
    if any(not isinstance(meta.get(key, ""), str) or len(meta.get(key, "")) > 120 for key in keys):
        raise ValueError(f"{name}: недопустимые метаданные")
    operations = meta.get("operations", [])
    if not isinstance(operations, list) or any(
        not isinstance(op, str) or not re.fullmatch(r"[a-z_]{1,50}", op) for op in operations
    ):
        raise ValueError(f"{name}: operations должен содержать имена операций")

    headings = list(HEADING.finditer(text))
    sections = [(Path(name).stem, text[:headings[0].start()] if headings else text)]
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        sections.append((heading[2], text[heading.end():end]))

    chunks = []
    for section, body in sections:
        errors = list(ERROR.finditer(body))
        heading_error = re.search(r"\bE\d{2}\b", section, re.IGNORECASE)
        section_code = heading_error[0].upper() if heading_error else ""
        blocks = [(section_code, body[:errors[0].start()] if errors else body)]
        for index, error in enumerate(errors):
            end = errors[index + 1].start() if index + 1 < len(errors) else len(body)
            blocks.append((error[1].upper(), body[error.start():end]))
        for code, block in blocks:
            cleaned = " ".join(block.split())
            for chunk in windows(cleaned):
                chunks.append({
                    "chunk_id": f"{Path(name).stem}-{len(chunks)}", "source": name, "section": section,
                    "text": chunk, "code": code, "operations": ",".join(operations),
                    **{key: meta.get(key, "").strip() for key in keys},
                })
    if not chunks:
        raise ValueError(f"{name}: отсутствует текст для индексации")
    return chunks
