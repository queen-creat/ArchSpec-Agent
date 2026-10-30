"""固定书内文字查找；归一化只用于匹配，返回位置和预览仍取完整原字。"""


def folded(text):
    return ''.join(character.casefold() for character in text if not character.isspace())


def find_entry(text, needle):
    """忽略PDF换行/空格并保留Unicode原位置；不修OCR、不作同义词或规范解释。"""
    characters, owners = [], []
    for index, character in enumerate(text):
        if character.isspace():
            continue
        value = character.casefold()
        characters.extend(value)
        owners.extend([index] * len(value))
    location = ''.join(characters).find(needle)
    if location < 0:
        return None
    start, end = owners[location], owners[location + len(needle) - 1] + 1
    preview_start = max(0, start - 30)
    preview_end = min(len(text), preview_start + 200)
    return {'match_start': start, 'match_end': end, 'preview_start': preview_start,
            'preview_end': preview_end, 'preview': text[preview_start:preview_end]}
