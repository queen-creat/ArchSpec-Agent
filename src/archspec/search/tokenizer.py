"""把中英文问题转换成可供关键词检索统计的词。"""

import re

TOKEN_PATTERN = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z0-9]+")
CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]+")


class LexicalTokenizer:
    """使用固定规则生成中文单字、双字词以及英文数字词。"""

    def tokenize(self, text: str) -> list[str]:
        tokens: list[str] = []
        for run in TOKEN_PATTERN.findall(text):
            if CJK_PATTERN.fullmatch(run):
                tokens.extend(run)
                tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
            else:
                tokens.append(run.lower())
        return tokens
