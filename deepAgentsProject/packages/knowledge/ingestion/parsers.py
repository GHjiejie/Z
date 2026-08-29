from __future__ import annotations

import csv
import io
import json
import re
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import List

from packages.knowledge.errors import KnowledgeValidationError
from packages.knowledge.models import ParsedBlock


class _TextHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: List[str] = []
        self.section: str | None = None
        self.blocks: List[ParsedBlock] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "br"}:
            self._flush()

    def handle_endtag(self, tag):
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            text = " ".join(self.parts).strip()
            if text:
                self.section = text
            self._flush()
        elif tag in {"p", "li", "tr"}:
            self._flush()

    def handle_data(self, data):
        if data.strip():
            self.parts.append(data.strip())

    def close(self):
        super().close()
        self._flush()

    def _flush(self):
        text = " ".join(self.parts).strip()
        if text:
            locator = {"section": self.section} if self.section else {}
            self.blocks.append(ParsedBlock(text=text, locator=locator))
        self.parts = []


class DocumentParser:
    version = "structure-parser-1.0"

    def parse(self, content: bytes, content_type: str, filename: str) -> List[ParsedBlock]:
        suffix = PurePosixPath(filename).suffix.lower()
        normalized_type = content_type.split(";", 1)[0].strip().lower()
        if suffix == ".pdf" or normalized_type == "application/pdf":
            return self._parse_pdf(content)
        if suffix == ".docx" or normalized_type.endswith("wordprocessingml.document"):
            return self._parse_docx(content)
        if suffix in {".html", ".htm"} or normalized_type == "text/html":
            parser = _TextHTMLParser()
            parser.feed(self._decode(content))
            parser.close()
            return parser.blocks
        if suffix == ".json" or normalized_type == "application/json":
            try:
                value = json.loads(self._decode(content))
                text = json.dumps(value, ensure_ascii=False, indent=2)
            except json.JSONDecodeError as exc:
                raise KnowledgeValidationError(f"Invalid JSON document: {exc}") from exc
            return [ParsedBlock(text=text, locator={"section": "root"})]
        if suffix == ".csv" or normalized_type == "text/csv":
            return self._parse_csv(content)
        if suffix in {".md", ".markdown"} or normalized_type in {
            "text/markdown",
            "text/x-markdown",
        }:
            return self._parse_markdown(self._decode(content))
        if suffix in {".txt", ""} or normalized_type.startswith("text/"):
            return self._parse_plain_text(self._decode(content))
        raise KnowledgeValidationError(
            f"Unsupported document type: {content_type or suffix or 'unknown'}"
        )

    def _parse_pdf(self, content: bytes) -> List[ParsedBlock]:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise KnowledgeValidationError("PDF parsing requires the pypdf dependency") from exc
        try:
            reader = PdfReader(io.BytesIO(content))
            blocks = []
            for index, page in enumerate(reader.pages, start=1):
                text = (page.extract_text() or "").strip()
                if text:
                    blocks.append(ParsedBlock(text=text, locator={"page": index}))
            return self._require_content(blocks)
        except Exception as exc:
            raise KnowledgeValidationError(f"Unable to parse PDF: {exc}") from exc

    def _parse_docx(self, content: bytes) -> List[ParsedBlock]:
        try:
            from docx import Document
        except ImportError as exc:
            raise KnowledgeValidationError("DOCX parsing requires the python-docx dependency") from exc
        try:
            document = Document(io.BytesIO(content))
            blocks: List[ParsedBlock] = []
            section: str | None = None
            for paragraph in document.paragraphs:
                text = paragraph.text.strip()
                if not text:
                    continue
                if paragraph.style and paragraph.style.name.lower().startswith("heading"):
                    section = text
                blocks.append(
                    ParsedBlock(text=text, locator={"section": section} if section else {})
                )
            for table_index, table in enumerate(document.tables, start=1):
                rows = [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
                text = "\n".join(row for row in rows if row.strip(" |"))
                if text:
                    blocks.append(
                        ParsedBlock(text=text, locator={"table": table_index, "section": section})
                    )
            return self._require_content(blocks)
        except Exception as exc:
            raise KnowledgeValidationError(f"Unable to parse DOCX: {exc}") from exc

    def _parse_markdown(self, text: str) -> List[ParsedBlock]:
        blocks: List[ParsedBlock] = []
        section: str | None = None
        buffer: List[str] = []

        def flush():
            if buffer:
                value = "\n".join(buffer).strip()
                if value:
                    blocks.append(
                        ParsedBlock(text=value, locator={"section": section} if section else {})
                    )
                buffer.clear()

        for line in text.splitlines():
            heading = re.match(r"^#{1,6}\s+(.+)$", line.strip())
            if heading:
                flush()
                section = heading.group(1).strip()
                buffer.append(line.strip())
            elif line.strip():
                buffer.append(line.rstrip())
            else:
                flush()
        flush()
        return self._require_content(blocks)

    def _parse_plain_text(self, text: str) -> List[ParsedBlock]:
        blocks = [
            ParsedBlock(text=part.strip(), locator={"paragraph": index})
            for index, part in enumerate(re.split(r"\n\s*\n", text), start=1)
            if part.strip()
        ]
        return self._require_content(blocks)

    def _parse_csv(self, content: bytes) -> List[ParsedBlock]:
        reader = csv.reader(io.StringIO(self._decode(content)))
        blocks = []
        for index, row in enumerate(reader, start=1):
            text = " | ".join(cell.strip() for cell in row)
            if text.strip(" |"):
                blocks.append(ParsedBlock(text=text, locator={"row": index}))
        return self._require_content(blocks)

    @staticmethod
    def _decode(content: bytes) -> str:
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return content.decode(encoding)
            except UnicodeDecodeError:
                continue
        raise KnowledgeValidationError("Document text encoding is not supported")

    @staticmethod
    def _require_content(blocks: List[ParsedBlock]) -> List[ParsedBlock]:
        if not blocks:
            raise KnowledgeValidationError("Document contains no extractable text")
        return blocks
