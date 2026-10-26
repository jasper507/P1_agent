import re
from dataclasses import dataclass
from pathlib import Path

HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
CLAUSE_NUMBER = re.compile(r"^\d+(\.\d+)*")
SENTENCE_END = re.compile(r"(?<=[。！？；])")
MAX_CHARS = 300  # 一段最长多少字。政策条款一般都短，超过的再按句子拆


@dataclass(frozen=True)
class Chunk:
    """政策里的一段，通常就是一条条款。"""

    id: str  # 文件名加条款号，比如 returns-1.1。评测题用它标答案
    citation: str  # 给用户看的出处，比如「《退换货政策》1.1 适用条件」
    path: str  # 标题路径，比如「《退换货政策》 > 1. 七天无理由退货 > 1.1 适用条件」
    text: str  # 条款正文

    @property
    def indexed_text(self) -> str:
        # 标题路径拼在正文前面一起建索引：正文里没写「退货」两个字，也能凭标题搜到
        return f"{self.path}\n{self.text}"


def load_policy_chunks(policy_dir: Path) -> list[Chunk]:
    chunks = [c for f in sorted(policy_dir.glob("*.md")) for c in chunk_markdown(f.stem, f.read_text(encoding="utf-8"))]
    if not chunks:
        raise ValueError(f"{policy_dir} 里没有找到售后政策")
    return chunks


def chunk_markdown(doc_id: str, markdown: str, max_chars: int = MAX_CHARS) -> list[Chunk]:
    """按条款切段。# 是文档标题，它下面的说明文字不进索引；## 和 ### 是章节和条款，标题要以编号开头。"""
    title = ""
    headings: list[tuple[int, str]] = []  # 当前所在的各级标题，(级别, 标题)
    body: list[str] = []
    chunks: list[Chunk] = []

    def flush():
        text = "\n".join(body)
        body.clear()
        if not text or not headings:
            return
        heading = headings[-1][1]
        number = CLAUSE_NUMBER.match(heading)
        if number is None:
            # 条款号是出处的一部分，没有编号就没法引用，宁可启动时报错
            raise ValueError(f"{doc_id}：条款标题要以编号开头，现在是「{heading}」")
        path = " > ".join([f"《{title}》", *(h for _, h in headings)])
        for piece in split_long(text, max_chars):
            chunks.append(Chunk(id=f"{doc_id}-{number.group()}", citation=f"《{title}》{heading}", path=path, text=piece))

    for line in markdown.splitlines():
        if match := HEADING.match(line):
            flush()
            level, heading = len(match.group(1)), match.group(2).strip()
            if level == 1:
                title, headings = heading, []
            else:
                headings = [h for h in headings if h[0] < level] + [(level, heading)]
        elif line.strip():
            body.append(line.strip())
    flush()
    return chunks


def split_long(text: str, max_chars: int) -> list[str]:
    """太长的条款按句子拆成几段，每段不超过 max_chars 字。一句话本身就超长的，单独成段。"""
    if len(text) <= max_chars:
        return [text]
    pieces, current = [], ""
    for sentence in SENTENCE_END.split(text):
        if current and len(current) + len(sentence) > max_chars:
            pieces.append(current)
            current = ""
        current += sentence
    if current:
        pieces.append(current)
    return pieces
