"""Remove private analysis even when XML-like tags cross stream chunks."""
import re


class PublicTextStream:
    def __init__(self):
        self.buffer = ""
        self.hidden = None

    def feed(self, text: str) -> str:
        self.buffer += text
        output = []
        while self.buffer:
            if self.hidden:
                end = re.search(rf"</{self.hidden}\s*>", self.buffer, re.I)
                if not end:
                    self.buffer = self.buffer[-(len(self.hidden) + 5):]
                    break
                self.buffer = self.buffer[end.end():]
                self.hidden = None
                continue
            start = self.buffer.find("<")
            if start < 0:
                output.append(self.buffer)
                self.buffer = ""
                break
            output.append(self.buffer[:start])
            self.buffer = self.buffer[start:]
            end = self.buffer.find(">")
            if end < 0:
                break
            tag = self.buffer[:end + 1]
            match = re.fullmatch(r"<(think|analysis)\s*>", tag, re.I)
            if match:
                self.hidden = match[1]
            else:
                output.append(tag)
            self.buffer = self.buffer[end + 1:]
        return "".join(output)

    def finish(self) -> str:
        text = self.buffer if not self.hidden and not self.buffer.startswith("<") else ""
        self.buffer = ""
        return text
