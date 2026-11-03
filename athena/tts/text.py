"""Speech-only formatting cleanup; never applied to code or tool arguments."""
import re
import unicodedata

def speech_text(text: str) -> str:
    text = unicodedata.normalize('NFC', text)
    text = re.sub(r'```.*?```', ' Code omitted. ', text, flags=re.S)
    text = re.sub(r'\[([^]]+)\]\([^)]*\)', r'\1', text)
    # Preserve a small boundary between written list entries. Removing the
    # markers outright joined race positions into one breathless sentence.
    text = re.sub(r'(?m)^\s*(?:[-+*]|\d+[.)])\s+',
                  lambda match: '' if not text[:match.start()].strip() else '. ', text)
    text = re.sub(r'\s+\.', '.', text)
    text = re.sub(r'[-_=]{2,}', ' ', text)
    text = re.sub(r'\b\d{1,3}(?:,\d{3})+\b', lambda match: match.group().replace(',', ''), text)
    text = re.sub(r'[,，、`*_#>|]', ' ', text)
    text = text.replace('\u2014', '. ').replace('\u2013', ' to ')
    text = ''.join(char for char in text if char.isspace() or
                   (unicodedata.category(char) not in {'Cc', 'Cf', 'Cs', 'So'} or char == '°'))
    return ' '.join(text.split()).strip()
